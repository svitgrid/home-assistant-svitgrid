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
        "createdAtMs",
    }
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


async def test_retry_keeps_order_and_sends_after_reconnect(setup):
    rt, state, api, clock, sleeps = setup
    _chat(state, "phone-1", 11)
    api.results[11] = [SendResult.RETRY, SendResult.RETRY]
    api.retry_after_s = None
    await _outage(rt)
    clock.now += timedelta(minutes=10)
    await rt.on_reading("hh1", "inv1", {"gridPresent": True, "timestamp": clock.now.isoformat()})
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
    await rt.on_reading("hh1", "inv1", {"gridPresent": True})
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
    await rt.on_reading("hh1", "inv1", {"gridPresent": True})
    await _settle(rt)
    assert api.sent == []  # no lone restore
    await _outage(rt)
    clock.now += timedelta(minutes=1)
    await rt.on_reading("hh1", "inv1", {"gridPresent": True})
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
