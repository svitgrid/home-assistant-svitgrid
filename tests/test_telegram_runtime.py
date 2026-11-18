"""The island Telegram runtime: send queue, retries, and the link poller."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.telegram import views as tv
from custom_components.svitgrid.telegram.bot_api import (
    BotInUseElsewhere,
    InvalidToken,
    SendResult,
)
from custom_components.svitgrid.telegram.runtime import TelegramRuntime
from custom_components.svitgrid.telegram.runtime import TestOutcome as Outcome
from custom_components.svitgrid.telegram.state import Chat

TOKEN = "123456:" + "A" * 35
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
OPEN_URL = "https://svitgrid.app/open?route=/"


class FakeApi:
    """Stands in for `BotApi`. Scripted send results; get_updates batches."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, tuple[str, str] | None]] = []
        self.results: dict[int, list] = {}  # chat_id -> results, popped per send
        self.default = SendResult.OK
        self.updates: list = []  # batches (list) or exceptions, popped per call
        self.update_calls: list[int | None] = []
        self.retry_after_s: int | None = None
        self._idle = asyncio.Event()

    async def send(self, chat_id, html, button):
        script = self.results.get(chat_id)
        result = script.pop(0) if script else self.default
        if isinstance(result, Exception):
            raise result
        if result is SendResult.OK:
            self.sent.append((chat_id, html, button))
        return result

    async def get_updates(self, offset, timeout_s=25):
        self.update_calls.append(offset)
        if self.updates:
            item = self.updates.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await self._idle.wait()  # a long poll with nothing to say
        return []


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


async def _until(cond, steps: int = 200) -> None:
    for _ in range(steps):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _settle(rt: TelegramRuntime) -> None:
    for _ in range(50):
        await asyncio.sleep(0)
    if rt._sender is not None:
        await rt._sender


def _chat(state, device_id, chat_id, *, enabled=True, language="uk"):
    state._chats[device_id] = Chat(
        device_id, chat_id, None, enabled, language, "2026-10-30T00:00:00+00:00"
    )


@pytest.fixture
async def setup(hass):
    # A full island: the cloud copy is off, so the station sends.
    MockConfigEntry(
        domain=DOMAIN, title="Дача", data={"household_id": "hh1", "cloud_ingest_enabled": False}
    ).add_to_hass(hass)
    state = await tv.telegram_state(hass)
    await state.set_bot(TOKEN, "my_bot")
    api = FakeApi()
    clock = Clock()
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    rt = TelegramRuntime(
        hass,
        state,
        lambda: None,
        clock,
        sleep=sleep,
        api_factory=lambda session, token: api,
    )
    yield rt, state, api, clock, sleeps
    await rt.stop()


async def _outage(rt, household="hh1"):
    """Two readings five minutes apart: the household goes out."""
    clock = rt._now
    await rt.on_reading(household, "inv1", {"gridPresent": False, "batterySoc": 87})
    clock.now = clock.now + timedelta(minutes=5)
    await rt.on_reading(household, "inv1", {"gridPresent": False, "batterySoc": 86})


async def _restore(rt, household="hh1", **extra):
    """Two grid-up readings five minutes apart: the restore waits for the grid to hold."""
    clock = rt._now
    await rt.on_reading(household, "inv1", {"gridPresent": True, **extra})
    clock.now = clock.now + timedelta(minutes=5)
    await rt.on_reading(household, "inv1", {"gridPresent": True})


async def test_outage_reaches_every_enabled_chat_once(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22, language="en")
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert sorted(c for c, _, _ in api.sent) == [11, 22]
    by_chat = {c: (html, button) for c, html, button in api.sent}
    assert "Дача" in by_chat[11][0] and "(86%)" in by_chat[11][0]
    assert by_chat[11][1] == ("Відкрити в Svitgrid", OPEN_URL)
    assert by_chat[22][1][1] == OPEN_URL
    assert state.queue() == []
    # More readings while out send nothing new.
    clock.now += timedelta(minutes=1)
    await rt.on_reading("hh1", "inv1", {"gridPresent": False, "batterySoc": 85})
    await _settle(rt)
    assert len(api.sent) == 2


async def test_queue_entry_shape(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await _outage(rt)  # not started: the sender never runs
    (entry,) = state.queue()
    assert set(entry) == {
        "id",
        "chatDeviceId",
        "kind",
        "householdId",
        "eventAtMs",
        "soc",
        "inverterId",
        "createdAtMs",
    }
    assert entry["inverterId"] is None  # an outage is household-level
    assert entry["chatDeviceId"] == "phone-1"
    assert entry["kind"] == "outage"
    assert entry["householdId"] == "hh1"
    assert entry["eventAtMs"] == NOW_MS
    assert entry["soc"] == 86
    assert entry["createdAtMs"] == NOW_MS + 5 * 60 * 1000
    assert len(entry["id"]) == 32


async def test_disabled_chat_gets_nothing(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22, enabled=False)
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [11]


async def test_no_token_queues_nothing(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    await state.clear()
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert state.queue() == [] and api.sent == []


async def test_battery_alerts_reach_every_enabled_chat(setup):
    """Low, critical and full go out with the cloud's copy (svitgrid D-2147)."""
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22, language="en")
    await rt.start()
    for soc in (25, 19, 7):
        await rt.on_reading("hh1", "inv1", {"gridPresent": True, "batterySoc": soc})
        clock.now += timedelta(minutes=10)
    await _settle(rt)
    uk = [h for c, h, _ in api.sent if c == 11]
    en = [h for c, h, _ in api.sent if c == 22]
    assert uk[0].startswith("<b>Низький заряд</b>\nБатарея Дача на рівні 19%")
    assert uk[1].startswith("<b>Критичний заряд</b>\nБатарея Дача критично розряджена — 7%")
    assert en[0].startswith("<b>Low Battery</b>\nДача battery is at 19%")
    for soc, power in ((100, 200), (100, 100)):
        await rt.on_reading(
            "hh1", "inv1", {"gridPresent": True, "batterySoc": soc, "batteryPower": power}
        )
        clock.now += timedelta(minutes=10)
    await _settle(rt)
    assert api.sent[-1][1].startswith("<b>Battery Fully Charged</b>") or api.sent[-1][1].startswith(
        "<b>Батарея заряджена</b>"
    )
    assert len(api.sent) == 6
    assert state.queue() == []


async def test_battery_alert_is_saved_at_once(setup):
    """A battery alert flushes the state like an outage, so a power cut does not
    lose the latch and tell it again."""
    rt, state, _, clock, _ = setup
    _chat(state, "phone-1", 11)
    flushes = []
    real = state.flush

    async def flush():
        flushes.append(1)
        await real()

    state.flush = flush
    await rt.on_reading("hh1", "inv1", {"gridPresent": True, "batterySoc": 25})
    assert flushes == []
    clock.now += timedelta(minutes=1)
    await rt.on_reading("hh1", "inv1", {"gridPresent": True, "batterySoc": 19})
    assert flushes == [1]
    assert [e["kind"] for e in state.queue()] == ["low_battery"]
    assert state.queue()[0]["inverterId"] == "inv1"


async def test_liveness_tick_sends_and_persists_offline(setup):
    """A silent board is told once per episode (svitgrid D-2149)."""
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    contact = NOW_MS - 20 * 60 * 1000
    snapshot = [
        {
            "householdId": "hh1",
            "gridOut": False,
            "inverters": [],
            "boards": [
                {"deviceId": "EC-1", "lastContactMs": contact, "paused": False, "inverterIds": []}
            ],
        }
    ]

    async def source():
        return snapshot

    rt._liveness_source = source
    rt._started_ms = NOW_MS - 60 * 60 * 1000
    await rt.start()
    await rt.check_liveness()
    await rt.check_liveness()
    await _settle(rt)
    assert len(api.sent) == 1
    assert api.sent[0][1].startswith(
        "<b>Модуль Svitgrid не на зв'язку</b>\nВід Дача немає даних 20 хв"
    )
    assert state.liveness()["households"]["hh1"]["boards"]["EC-1"]["sentAtMs"] == NOW_MS
    snapshot[0]["boards"][0]["lastContactMs"] = NOW_MS
    await rt.check_liveness()
    await _settle(rt)
    assert api.sent[-1][1].startswith("<b>Модуль Svitgrid знову на зв'язку</b>")


async def test_liveness_needs_a_bot_and_a_chat(setup):
    rt, state, api, _, _ = setup

    async def source():
        raise AssertionError("not read without an enabled chat")

    rt._liveness_source = source
    await rt.check_liveness()
    assert state.liveness() == {}


async def test_rule_notice_reaches_every_enabled_chat(setup):
    """A rule with notifyOnStartStop tells every chat (svitgrid D-2151)."""
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22, language="en")
    await rt.start()
    event = {
        "id": "evt-1",
        "name": "Бойлер",
        "mode": "custom",
        "schedule": {"recurrence": "always"},
        "config": {"notifyOnStartStop": True},
    }
    await rt.on_rule_notice("hh1", event, "started")
    await _settle(rt)
    by_chat = {c: h for c, h, _ in api.sent}
    assert by_chat[11].startswith("<b>▶️ Подія «Бойлер» почалася</b>")
    assert by_chat[22].startswith("<b>▶️ Event “Бойлер” started</b>")
    assert state.queue() == []


async def test_rule_notice_needs_a_chat(setup):
    rt, state, _, _, _ = setup
    await rt.on_rule_notice("hh1", {"id": "e", "name": "x"}, "ended")
    assert state.queue() == []


async def test_a_chat_gets_only_the_kinds_it_wants(setup):
    """A muted kind skips that chat, and quiet hours hold back what is not
    urgent, as the cloud's sender does (svitgrid D-2150)."""
    rt, state, api, clock, _ = setup
    state._chats["phone-1"] = Chat(
        "phone-1",
        11,
        None,
        True,
        "uk",
        "2026-10-30T00:00:00+00:00",
        {"mutedAlerts": ["low_battery"]},
    )
    _chat(state, "phone-2", 22)
    await rt.on_reading("hh1", "inv1", {"gridPresent": True, "batterySoc": 25})
    clock.now += timedelta(minutes=10)
    await rt.on_reading("hh1", "inv1", {"gridPresent": True, "batterySoc": 19})
    assert [(e["chatDeviceId"], e["kind"]) for e in state.queue()] == [("phone-2", "low_battery")]


async def test_quiet_hours_hold_a_notice_but_not_an_outage(setup, hass):
    rt, state, _, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    quiet = {"quietHoursEnabled": True, "quietHoursStart": "13:00", "quietHoursEnd": "15:00"}
    state._chats["phone-1"] = Chat(
        "phone-1", 11, None, True, "uk", "2026-10-30T00:00:00+00:00", quiet
    )
    # NOW is 12:00 UTC, 14:00 in Kyiv: inside the window.
    await rt.on_rule_notice("hh1", {"id": "e", "name": "x"}, "started")
    assert state.queue() == []
    await _outage(rt)
    assert [e["kind"] for e in state.queue()] == ["outage"]


async def test_daily_report_goes_out_once_per_day(setup, hass):
    """At the chat's report time, once per day even across a restart (svitgrid D-2152)."""
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    state._chats["phone-1"] = Chat(
        "phone-1", 11, None, True, "uk", "2026-10-30T00:00:00+00:00", {"dailyReport": True}
    )
    _chat(state, "phone-2", 22)  # reports off
    asked = []

    async def totals(household_id, from_day, to_day):
        asked.append((household_id, from_day, to_day))
        return {"pv": 12.0, "load": 9.0, "import": 1.0, "export": 2.0}

    rt._summary_totals = totals
    await rt.start()
    # NOW is 2026-10-30 12:00 UTC, 14:00 in Kyiv: yesterday's report is due.
    await rt.check_summaries(["hh1"])
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [11]
    assert api.sent[0][1].startswith("<b>Щоденний звіт</b>\nЗа вчора: 12.0 кВт·год згенеровано")
    assert asked == [("hh1", "2026-10-29", "2026-10-29")]
    await state.flush()
    again = tv.TelegramState(hass)
    await again.load()
    rt._state = again
    await rt.check_summaries(["hh1"])
    await _settle(rt)
    assert len(api.sent) == 1


async def test_a_period_with_no_energy_is_tried_again(setup, hass):
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    state._chats["phone-1"] = Chat(
        "phone-1", 11, None, True, "uk", "2026-10-30T00:00:00+00:00", {"dailyReport": True}
    )
    answer = {"value": None}

    async def totals(household_id, from_day, to_day):
        return answer["value"]

    rt._summary_totals = totals
    await rt.start()
    await rt.check_summaries(["hh1"])
    await _settle(rt)
    assert api.sent == []
    answer["value"] = {"pv": 1.0, "load": 1.0, "import": 0.0, "export": 0.0}
    await rt.check_summaries(["hh1"])
    await _settle(rt)
    assert len(api.sent) == 1


def _strings_source(readings, *, name="Deye", strings=None, asked=None):
    """One island inverter with two strings and `readings` over the window."""

    async def source(since_ms):
        if asked is not None:
            asked.append(since_ms)
        return [
            {
                "householdId": "hh1",
                "inverterId": "inv1",
                "inverterName": name,
                "strings": strings
                if strings is not None
                else [{"id": "PV1", "peakPowerWp": 3000}, {"id": "PV2", "peakPowerWp": 3000}],
                "readings": readings,
            }
        ]

    return source


DEAD_PV2 = [{"pvPower": 2500, "pvPower1": 2500, "pvPower2": 0}] * 60


async def test_a_dead_string_is_told_once_a_day_at_most(setup, hass):
    """The cloud's string-outage rule, once a day from 09:00 (svitgrid D-2155)."""
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    _chat(state, "phone-1", 11)
    asked: list[int] = []
    rt._strings_source = _strings_source(DEAD_PV2, asked=asked)
    await rt.start()
    # NOW is 14:00 in Kyiv.
    await rt.check_strings()
    await rt.check_strings()
    await _settle(rt)
    assert len(api.sent) == 1
    assert api.sent[0][1] == (
        "<b>Рядок панелей не працює</b>\nDeye: рядок PV2 майже не виробляє, тоді як інші "
        "працюють — перевірте підключення/запобіжники.\n🕑 14:00"
    )
    assert asked == [NOW_MS - 7 * 24 * 60 * 60 * 1000]
    # The next day the string is still dead: the transition gate holds, even
    # across a restart.
    await state.flush()
    again = tv.TelegramState(hass)
    await again.load()
    rt._state = again
    clock.now = NOW + timedelta(days=1)
    await rt.check_strings()
    await _settle(rt)
    assert len(api.sent) == 1
    assert len(asked) == 2


async def test_strings_wait_for_nine_in_the_morning(setup, hass):
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    _chat(state, "phone-1", 11)
    asked: list[int] = []
    rt._strings_source = _strings_source(DEAD_PV2, asked=asked)
    await rt.start()
    clock.now = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)  # 08:00 in Kyiv
    await rt.check_strings()
    assert asked == []
    clock.now = datetime(2026, 10, 30, 7, 0, tzinfo=UTC)  # 09:00
    await rt.check_strings()
    await _settle(rt)
    assert len(api.sent) == 1


async def test_a_muted_string_alert_is_not_sent(setup, hass):
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")
    state._chats["phone-1"] = Chat(
        "phone-1",
        11,
        None,
        True,
        "en",
        "2026-10-30T00:00:00+00:00",
        {"mutedAlerts": ["string_offline"]},
    )
    _chat(state, "phone-2", 22, language="en")
    rt._strings_source = _strings_source(DEAD_PV2, name=None)
    await rt.start()
    await rt.check_strings()
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [22]
    # With no inverter name the station's name stands in.
    assert api.sent[0][1].startswith("<b>Solar string offline</b>\nДача: string PV2 is")


async def test_strings_need_a_bot_a_chat_and_a_layout(setup, hass):
    rt, state, api, clock, _ = setup
    await hass.config.async_update(time_zone="Europe/Kyiv")

    async def source(since_ms):
        raise AssertionError("not read without an enabled chat")

    rt._strings_source = source
    await rt.check_strings()
    _chat(state, "phone-1", 11)
    # An older app never sent a layout: the source has nothing, nothing is sent.
    rt._strings_source = _strings_source(DEAD_PV2, strings=[])
    await rt.check_strings()
    await _settle(rt)
    assert api.sent == []


async def test_a_failing_string_check_does_not_stop_the_tick(setup, hass):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)

    async def source(since_ms):
        raise RuntimeError("store gone")

    rt._strings_source = source
    await rt._tick_once()  # logs, never raises


async def test_retry_keeps_order_and_sends_after_reconnect(setup):
    rt, state, api, clock, sleeps = setup
    _chat(state, "phone-1", 11)
    api.results[11] = [SendResult.RETRY, SendResult.RETRY]
    api.retry_after_s = None
    await _outage(rt)
    clock.now += timedelta(minutes=10)
    await _restore(rt, timestamp=clock.now.isoformat())
    assert [e["kind"] for e in state.queue()] == ["outage", "grid_restored"]
    await rt.start()
    await _settle(rt)
    assert sleeps == [60, 60]
    assert len(api.sent) == 2
    assert "<b>Відключення" in api.sent[0][1]
    assert "<b>Мережу відновлено" in api.sent[1][1]
    assert state.queue() == []


async def test_retry_after_hint_sets_the_sleep(setup):
    rt, state, api, _, sleeps = setup
    _chat(state, "phone-1", 11)
    api.results[11] = [SendResult.RETRY]
    api.retry_after_s = 7
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert sleeps == [7]
    assert len(api.sent) == 1


async def test_blocked_chat_unlinks_without_stalling_others(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22)
    api.results[11] = [SendResult.BLOCKED]
    await _outage(rt)
    clock.now += timedelta(minutes=10)
    await _restore(rt)
    assert len(state.queue()) == 4
    await rt.start()
    await _settle(rt)
    assert state.chat_for("phone-1") is None
    assert state.chat_for("phone-2") is not None
    assert [c for c, _, _ in api.sent] == [22, 22]
    assert state.queue() == []


async def test_rejected_entry_is_dropped(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22)
    api.results[11] = [SendResult.REJECTED]
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [22]
    assert state.chat_for("phone-1") is not None
    assert state.queue() == []


async def test_invalid_token_keeps_queue_and_stops(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    api.results[11] = [InvalidToken("x")]
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert state.status == "invalid_token"
    assert len(state.queue()) == 1
    # A kick while the token is invalid sends nothing.
    rt._kick_sender()
    await _settle(rt)
    assert api.sent == [] and len(state.queue()) == 1


def _entry(i, created_ms, device="phone-1"):
    return {
        "id": f"{i:032x}",
        "chatDeviceId": device,
        "kind": "outage",
        "householdId": "hh1",
        "eventAtMs": created_ms,
        "soc": 50,
        "createdAtMs": created_ms,
    }


async def test_queue_drops_entries_older_than_7_days(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    week_ms = 7 * 24 * 3600 * 1000
    await state.set_queue(
        [_entry(1, NOW_MS - week_ms - 1), _entry(2, NOW_MS - week_ms + 1000), _entry(3, NOW_MS)]
    )
    await rt.start()
    await _settle(rt)
    assert len(api.sent) == 2  # the 7-day-old entry never went out
    assert state.queue() == []


async def test_queue_caps_at_50(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await state.set_queue([_entry(i, NOW_MS - 1000 + i) for i in range(50)])
    await _outage(rt)  # not started: one more entry joins the queue
    queue = state.queue()
    assert len(queue) == 50
    assert queue[0]["id"] == f"{1:032x}"  # the oldest one went
    assert queue[-1]["kind"] == "outage" and queue[-1]["createdAtMs"] == NOW_MS + 300_000


async def test_sends_in_event_order(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22)
    later = {**_entry(1, NOW_MS - 5000, "phone-1"), "eventAtMs": NOW_MS - 1000}
    earlier = {**_entry(2, NOW_MS, "phone-2"), "eventAtMs": NOW_MS - 9000}
    await state.set_queue([later, earlier])
    await rt.start()
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [22, 11]


async def test_malformed_entry_is_dropped_not_retried(setup):
    rt, state, api, _, sleeps = setup
    _chat(state, "phone-1", 11)
    await state.set_queue(
        [
            {**_entry(1, NOW_MS - 3000), "kind": None},
            {**_entry(2, NOW_MS - 2000), "kind": "bogus"},
            _entry(3, NOW_MS - 1000),
        ]
    )
    await rt.start()
    await _settle(rt)
    assert len(api.sent) == 1 and sleeps == []
    assert state.queue() == []


async def test_unexpected_send_error_drops_the_entry(setup):
    rt, state, api, _, sleeps = setup
    _chat(state, "phone-1", 11)
    _chat(state, "phone-2", 22)
    api.results[11] = [RuntimeError("boom")]
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [22]
    assert sleeps == [] and state.queue() == []


async def test_sender_survives_a_storage_error(setup):
    rt, state, api, _, sleeps = setup
    _chat(state, "phone-1", 11)
    real = rt._next_entry
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk")
        return await real()

    rt._next_entry = flaky
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert sleeps == [60]
    assert len(api.sent) == 1


async def test_poller_survives_an_unexpected_error(setup):
    rt, state, api, clock, sleeps = setup
    await rt.start()
    await state.issue_code("phone-1", "uk", clock.now)
    api.updates = [RuntimeError("boom"), []]
    rt.wake()
    await _until(lambda: len(api.update_calls) >= 3)
    assert sleeps == [5]
    assert not rt._poller.done()


async def test_setup_mid_outage_sends_outage_not_lone_restore(setup):
    rt, state, api, clock, _ = setup
    await rt.start()
    await _outage(rt)  # no chat linked yet
    assert state.detector() == {}
    _chat(state, "phone-1", 11)
    clock.now += timedelta(minutes=1)
    await _restore(rt)
    await _settle(rt)
    assert api.sent == []  # no lone restore
    await _outage(rt)
    clock.now += timedelta(minutes=1)
    await _restore(rt)
    await _settle(rt)
    assert ["<b>Відключення" in h for _, h, _ in api.sent] == [True, False]


async def test_send_test(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    assert await rt.send_test("phone-1") is Outcome.SENT
    assert api.sent[0][0] == 11 and api.sent[0][2] == ("Відкрити в Svitgrid", OPEN_URL)
    assert await rt.send_test("phone-9") is Outcome.NOT_LINKED
    api.results[11] = [SendResult.RETRY]
    assert await rt.send_test("phone-1") is Outcome.UNREACHABLE
    api.results[11] = [InvalidToken("x")]
    assert await rt.send_test("phone-1") is Outcome.INVALID_TOKEN
    assert state.status == "invalid_token"
    api.results[11] = [SendResult.BLOCKED]
    assert await rt.send_test("phone-1") is Outcome.NOT_LINKED
    assert state.chat_for("phone-1") is None
    await state.clear()
    assert await rt.send_test("phone-1") is Outcome.NOT_LINKED


async def test_poller_idle_without_codes(setup):
    rt, state, api, _, _ = setup
    await rt.start()
    rt.wake()
    await _settle(rt)
    assert api.update_calls == []
    assert rt._poller is None or rt._poller.done()


def _update(update_id, text, chat_id=555, username="ann", chat_type="private"):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"username": username},
        },
    }


async def test_start_with_code_links_and_welcomes(setup, hass):
    rt, state, api, clock, _ = setup
    await rt.start()
    code = await state.issue_code("phone-1", "uk", clock.now)
    await state.issue_code("phone-2", "uk", clock.now)  # keeps the poller going
    api.updates = [[_update(10, f"/start {code}")]]
    hass.data[DOMAIN]["telegram_wake"]()
    await _until(lambda: len(api.update_calls) >= 2)
    await _until(lambda: api.sent)
    chat = state.chat_for("phone-1")
    assert chat is not None and chat.chat_id == 555 and chat.username == "ann"
    assert api.sent == [(555, "Готово: сповіщення станції приходитимуть сюди.", None)]
    assert state.offset == 11
    assert api.update_calls[1] == 11


async def test_start_with_bot_name_links(setup):
    rt, state, api, clock, _ = setup
    await rt.start()
    code = await state.issue_code("phone-1", "uk", clock.now)
    api.updates = [[_update(3, f"/start@my_bot {code}")]]
    rt.wake()
    await _until(lambda: state.chat_for("phone-1") is not None)
    # The last pending code was claimed, so the poller stops.
    await _until(lambda: rt._poller.done())
    assert len(api.update_calls) == 1


async def test_start_with_wrong_code_links_nothing(setup):
    rt, state, api, clock, _ = setup
    await rt.start()
    code = await state.issue_code("phone-1", "uk", clock.now)
    api.updates = [
        [
            _update(5, "/start wrongcode"),
            _update(6, f"hello {code}"),
            {"update_id": 7, "edited_message": {"text": f"/start {code}"}},
        ]
    ]
    rt.wake()
    await _until(lambda: len(api.update_calls) >= 2)
    assert state.chats() == []
    assert api.sent == []
    assert state.offset == 8
    assert state.has_pending_codes(clock.now)


async def test_poller_stops_when_codes_expire(setup):
    rt, state, api, clock, _ = setup
    await rt.start()
    await state.issue_code("phone-1", "uk", clock.now)
    clock.now += timedelta(minutes=11)
    api.updates = [[]]
    rt.wake()
    await _settle(rt)
    assert api.update_calls == []


async def test_409_sets_status(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await rt.start()
    await state.issue_code("phone-2", "uk", clock.now)
    api.updates = [BotInUseElsewhere("x")]
    rt.wake()
    await _until(lambda: state.status == "bot_in_use_elsewhere")
    await _until(lambda: rt._poller.done())
    assert len(api.update_calls) == 1
    # Sending continues.
    await _outage(rt)
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [11]
    # The next wake polls again and a good poll clears the status.
    api.updates = [[]]
    rt.wake()
    await _until(lambda: len(api.update_calls) >= 2)
    await _until(lambda: state.status is None)


async def test_wake_twice_starts_one_poller(setup):
    rt, state, api, clock, _ = setup
    await rt.start()
    await state.issue_code("phone-1", "uk", clock.now)
    rt.wake()
    first = rt._poller
    rt.wake()
    assert rt._poller is first
    await _until(lambda: len(api.update_calls) == 1)
    await _settle(rt)
    assert len(api.update_calls) == 1


async def test_stop_restores_wake_and_cancels(setup, hass):
    rt, state, api, clock, _ = setup
    await rt.start()
    assert hass.data[DOMAIN]["telegram_wake"] == rt.wake
    await state.issue_code("phone-1", "uk", clock.now)
    rt.wake()
    await _until(lambda: len(api.update_calls) == 1)
    poller = rt._poller
    await rt.stop()
    assert poller.done()
    assert hass.data[DOMAIN]["telegram_wake"] != rt.wake
    hass.data[DOMAIN]["telegram_wake"]()  # the no-op does not raise


async def test_logs_carry_no_token_text_or_chat_id(setup, caplog):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 987654)
    _chat(state, "phone-2", 876543)
    api.results[987654] = [SendResult.REJECTED]
    api.results[876543] = [SendResult.BLOCKED]
    caplog.set_level("DEBUG")
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    code = await state.issue_code("phone-3", "uk", clock.now)
    api.updates = [[_update(1, f"/start {code}", chat_id=765432)]]
    rt.wake()
    await _until(lambda: state.chat_for("phone-3") is not None)
    # The test harness's mock storage logs every write in full; Home
    # Assistant's real Store logs only the key and the path.
    text = "\n".join(
        r.getMessage()
        for r in caplog.records
        if not r.name.startswith("pytest_homeassistant_custom_component")
    )
    for secret in (TOKEN, "987654", "876543", "765432", code, "Дача"):
        assert secret not in text


async def test_queued_alert_is_dropped_when_the_cloud_copy_goes_on(setup, hass):
    """Decision 8: once the cloud copy is on, the cloud sends this household's
    alerts, so one still queued here would arrive twice."""
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await _outage(rt)  # not started: queued, not sent
    assert len(state.queue()) == 1
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    hass.config_entries.async_update_entry(entry, data={**entry.data, "cloud_ingest_enabled": True})
    await rt.start()
    await _settle(rt)
    assert api.sent == []
    assert state.queue() == []
    assert "hh1" not in state.detector().get("households", {})


async def test_drop_households_removes_their_queue_and_detector_state(setup):
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await _outage(rt, "hh1")
    await _outage(rt, "hh2")
    await rt.drop_households({"hh1"})
    assert [e["householdId"] for e in state.queue()] == ["hh2"]
    assert list(state.detector()["households"]) == ["hh2"]


async def test_a_queued_outage_is_written_at_once(setup, hass_storage):
    """HA does not unload entries on shutdown, so a delayed save can be lost
    to a power cut and the outage told again after the restart."""
    rt, state, api, clock, _ = setup
    _chat(state, "phone-1", 11)
    await _outage(rt)  # the clock never moves, so no delayed save has run
    saved = hass_storage["svitgrid_telegram"]["data"]
    assert [e["kind"] for e in saved["queue"]] == ["outage"]
    assert saved["detector"]["households"]["hh1"]["out"] is True


async def test_start_from_a_group_chat_links_nothing(setup):
    """Only a private chat links: a code posted in a group stays unclaimed."""
    rt, state, api, clock, _ = setup
    await rt.start()
    code = await state.issue_code("phone-1", "uk", clock.now)
    api.updates = [
        [
            _update(5, f"/start {code}", chat_id=-100, chat_type="group"),
            _update(6, f"/start {code}", chat_id=-101, chat_type="supergroup"),
            _update(7, f"/start {code}", chat_id=-102, chat_type="channel"),
        ]
    ]
    rt.wake()
    await _until(lambda: len(api.update_calls) >= 2)
    assert state.chats() == []
    assert api.sent == []
    assert state.has_pending_codes(clock.now)


async def test_a_sent_alert_clears_bot_in_use_elsewhere(setup):
    """A good send proves the bot works, so the status does not stick."""
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    await state.set_status("bot_in_use_elsewhere")
    await rt.start()
    await _outage(rt)
    await _settle(rt)
    assert [c for c, _, _ in api.sent] == [11]
    assert state.status is None


async def test_a_sent_test_clears_bot_in_use_elsewhere(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    await state.set_status("bot_in_use_elsewhere")
    assert await rt.send_test("phone-1") is Outcome.SENT
    assert state.status is None


async def test_send_test_maps_an_unexpected_error_to_unreachable(setup):
    rt, state, api, _, _ = setup
    _chat(state, "phone-1", 11)
    api.results[11] = [RuntimeError("boom")]
    assert await rt.send_test("phone-1") is Outcome.UNREACHABLE
    assert state.chat_for("phone-1") is not None
