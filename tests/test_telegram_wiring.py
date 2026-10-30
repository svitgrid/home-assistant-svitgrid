"""Island Telegram alerts wired into the Home Assistant station.

Every reading the store accepts reaches the Telegram runtime, but only for a
household on a full island: with the cloud copy on, the cloud sends the alert
(spec decision 8). Readings come from the entry's own harvest loops and from an
adopted edge box through the edge ingest views. The entry is set up for real,
with its reading store in `tmp_path`; Telegram is `aioclient_mock`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from multidict import CIMultiDict
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.svitgrid as svitgrid
from custom_components.svitgrid import edge_api
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import (
    SvitgridIslandCloudCopyView,
    SvitgridIslandLeaveView,
)
from custom_components.svitgrid.keystore import EdgeBoxEntry, SvitgridKeystore
from custom_components.svitgrid.telegram import views as tv
from custom_components.svitgrid.telegram.runtime import TelegramRuntime, household_key
from custom_components.svitgrid.telegram.state import Chat
from tests.test_edge_api import _CASE_0_PAYLOAD, _INV_1
from tests.test_island_lan import NOW_MS, _Admin, _Req

TOKEN = "123456:" + "A" * 35
BASE = f"https://api.telegram.org/bot{TOKEN}"
BOX_KEY = "box-api-key-1"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def iso(self, seconds: int = 0) -> str:
        at = self.now + timedelta(seconds=seconds)
        return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _entry(*, cloud_copy: bool = False, inverters=("ha-aaa",), **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Дача",
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "edge1",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "cloud_ingest_enabled": cloud_copy,
            "inverters": [
                {"inverter_id": i, "entity_map": {}, "preset_id": None} for i in inverters
            ],
            **data,
        },
    )


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(svitgrid, "_telegram_now", clock)
    monkeypatch.setattr(edge_api, "_now", clock)
    return clock


@pytest.fixture
def station(hass, enable_custom_integrations, tmp_path, monkeypatch, aioclient_mock, clock):
    """Sets entries up for real, with the loops that call out stubbed."""
    monkeypatch.setattr(svitgrid, "READINGS_DB_SUBDIR", str(tmp_path))
    aioclient_mock.post(f"{BASE}/sendMessage", json={"ok": True, "result": {}})
    with (
        patch("custom_components.svitgrid.run_readings_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.run_command_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.run_sender_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.run_settings_sync_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.run_event_scheduler_loop", AsyncMock(return_value=None)),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch(
            "custom_components.svitgrid.SvitgridUpdateCoordinator.async_refresh",
            AsyncMock(return_value=None),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        yield


async def _bot(hass) -> None:
    state = await tv.telegram_state(hass)
    await state.set_bot(TOKEN, "my_bot")
    state._chats["phone-1"] = Chat("phone-1", 77, "ann", True, "uk", "2026-10-30T00:00:00+00:00")


async def _set_up(hass, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _feed(hass, entry, clock, *, grid: bool, soc: int = 87) -> None:
    store = hass.data[DOMAIN][entry.entry_id]["store"]
    await store.append(
        {"inverterId": "ha-aaa", "timestamp": clock.iso(), "gridPresent": grid, "batterySoc": soc}
    )
    await hass.async_block_till_done(wait_background_tasks=True)


def _sends(aioclient_mock) -> list[str]:
    return [str(c[2]["text"]) for c in aioclient_mock.mock_calls if "sendMessage" in str(c[1])]


async def _outage(hass, entry, clock) -> None:
    await _feed(hass, entry, clock, grid=False)
    clock.now += timedelta(minutes=5)
    await _feed(hass, entry, clock, grid=False, soc=86)


def test_household_key_is_the_household_id_else_the_entry_id():
    assert household_key(_entry()) == "hh1"
    local = MockConfigEntry(domain=DOMAIN, data={"local_only": True})
    assert household_key(local) == local.entry_id


async def test_cloud_copy_on_sends_nothing(hass, station, aioclient_mock, clock):
    """Decision 8: the cloud already sends this household's alerts."""
    await _bot(hass)
    entry = _entry(cloud_copy=True)
    await _set_up(hass, entry)

    await _outage(hass, entry, clock)

    state = await tv.telegram_state(hass)
    assert _sends(aioclient_mock) == []
    assert state.queue() == []
    assert state.detector() == {}


async def test_cloud_copy_off_outage_reaches_queue(hass, station, aioclient_mock, clock):
    await _bot(hass)
    entry = _entry()
    await _set_up(hass, entry)

    await _outage(hass, entry, clock)

    sends = _sends(aioclient_mock)
    assert len(sends) == 1
    assert "Дача" in sends[0]
    assert "86%" in sends[0] or "87%" in sends[0]
    state = await tv.telegram_state(hass)
    assert list(state.detector()["households"]) == ["hh1"]
    assert state.queue() == []


async def test_edge_reading_with_edge_confirmed_fires_at_once(hass, station, aioclient_mock, clock):
    """The box's confirmed `lost` makes the next reading an outage at once,
    without the 5-minute wait."""
    await _bot(hass)
    keystore = SvitgridKeystore(hass)
    await keystore.save(
        api_key="k",
        public_key_hex="pub",
        private_key_pem="pem",
        signing_key_id="sk",
        trusted_key_ids=[],
        trusted_public_keys_hex={},
    )
    await keystore.async_add_edge_box(
        EdgeBoxEntry(
            device_id="EC-1",
            api_key=BOX_KEY,
            inverter_ids=["inv-1"],
            inverters={"inv-1": _INV_1},
            added_at="2026-10-30T10:00:00+00:00",
            pair_code_hash=None,
            pair_code_expires_at=None,
        )
    )
    entry = _entry(inverters=(), edge_move=True)
    await _set_up(hass, entry)

    async def post(view_cls, body):
        request = _EdgeReq(hass, body)
        resp = await view_cls().post(request)
        assert resp.status == 200, resp.body
        await hass.async_block_till_done(wait_background_tasks=True)

    def reading(seconds, **drop):
        r = {**_CASE_0_PAYLOAD, "inverterId": "inv-1", "timestamp": clock.iso(seconds)}
        return {k: v for k, v in r.items() if k not in drop}

    await post(edge_api.EdgeIngestBatchView, {"readings": [reading(-120)]})
    await post(
        edge_api.EdgeGridEventView,
        {
            "inverterId": "inv-1",
            "state": "lost",
            "confirmedAt": clock.iso(-60),
            "signals": {"phaseVoltages": [0, 0, 0], "gridRelayClosed": 0},
        },
    )
    assert _sends(aioclient_mock) == []
    no_grid = dict.fromkeys(("gridVoltage", "phaseVoltages", "gridFrequency", "gridRelayClosed"))
    await post(edge_api.EdgeIngestBatchView, {"readings": [reading(-30, **no_grid)]})

    assert len(_sends(aioclient_mock)) == 1
    state = await tv.telegram_state(hass)
    assert list(state.detector()["households"]) == ["hh1"]


class _EdgeReq:
    def __init__(self, hass, body) -> None:
        self.app = {"hass": hass}
        self.headers = CIMultiDict({"X-API-Key": BOX_KEY})
        self.match_info: dict = {}
        self._body = body

    async def json(self):
        return self._body


async def test_restart_mid_outage_sends_no_second_outage(hass, station, aioclient_mock, clock):
    """The detector state survives a restart, so the outage is not told twice
    and its restore still goes out."""
    await _bot(hass)
    entry = _entry()
    await _set_up(hass, entry)
    await _outage(hass, entry, clock)
    assert len(_sends(aioclient_mock)) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert DOMAIN not in hass.data or "telegram" not in hass.data[DOMAIN]
    hass.data[DOMAIN].pop("telegram_state")  # a restart loads it from disk
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    clock.now += timedelta(minutes=10)
    await _feed(hass, entry, clock, grid=False, soc=80)
    assert len(_sends(aioclient_mock)) == 1
    clock.now += timedelta(minutes=1)
    await _feed(hass, entry, clock, grid=True, soc=80)
    assert len(_sends(aioclient_mock)) == 2


async def test_reload_keeps_one_runtime(hass, station, aioclient_mock, clock):
    await _bot(hass)
    entry = _entry()
    await _set_up(hass, entry)
    first = hass.data[DOMAIN]["telegram"]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[DOMAIN]["telegram"]
    assert runtime is not first and runtime._running and not first._running
    await _outage(hass, entry, clock)
    assert len(_sends(aioclient_mock)) == 1


async def test_leave_clears_telegram_state(hass, station, aioclient_mock, clock):
    """Leaving the island hands the alerts back to the cloud: the station's
    bot, chats and queue go, and its readings feed nothing from then on."""
    await _bot(hass)
    admin = _Admin()
    keystore = SvitgridKeystore(hass)
    await keystore.save(
        api_key="k",
        public_key_hex="pub",
        private_key_pem="pem",
        signing_key_id="sk",
        trusted_key_ids=[admin.key_id],
        trusted_public_keys_hex={admin.key_id: admin.pub},
    )
    await keystore.async_add_island_key("phone-1", "p" * 43, label="Pixel 8")
    entry = _entry(trusted_keys=[{"keyId": admin.key_id, "publicKeyHex": admin.pub}])
    await _set_up(hass, entry)
    await _feed(hass, entry, clock, grid=False)

    with (
        patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        resp = await SvitgridIslandLeaveView().post(_Req(hass, admin.request("leave_island")))
    assert resp.status == 200

    state = await tv.telegram_state(hass)
    assert state.token() is None
    assert state.chats() == []
    assert state.queue() == []
    await _bot(hass)  # even with a bot again, the entry is no longer island
    clock.now += timedelta(minutes=5)
    await _feed(hass, entry, clock, grid=False)
    assert _sends(aioclient_mock) == []


async def test_leave_keeps_telegram_while_a_local_only_entry_stays_island(
    hass, station, aioclient_mock, clock
):
    await _bot(hass)
    admin = _Admin()
    keystore = SvitgridKeystore(hass)
    await keystore.save(
        api_key="k",
        public_key_hex="pub",
        private_key_pem="pem",
        signing_key_id="sk",
        trusted_key_ids=[admin.key_id],
        trusted_public_keys_hex={admin.key_id: admin.pub},
    )
    await keystore.async_add_island_key("phone-1", "p" * 43, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = keystore
    cloud = _entry(trusted_keys=[{"keyId": admin.key_id, "publicKeyHex": admin.pub}])
    cloud.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={"local_only": True}).add_to_hass(hass)
    state = await tv.telegram_state(hass)
    await state.set_detector({"households": {"hh1": {"out": True}, "other": {"out": True}}})
    await state.set_queue(
        [
            {"id": "a", "householdId": "hh1", "kind": "outage"},
            {"id": "b", "householdId": "other", "kind": "outage"},
        ]
    )

    with (
        patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        resp = await SvitgridIslandLeaveView().post(_Req(hass, admin.request("leave_island")))
    assert resp.status == 200

    assert state.token() == TOKEN
    assert [c.device_id for c in state.chats()] == ["phone-1"]
    assert [e["id"] for e in state.queue()] == ["b"]
    assert list(state.detector()["households"]) == ["other"]


async def test_a_failed_start_is_retried_at_the_next_setup(hass, station, aioclient_mock, clock):
    await _bot(hass)
    entry = _entry()
    with patch.object(TelegramRuntime, "start", AsyncMock(side_effect=RuntimeError("boom"))):
        await _set_up(hass, entry)
    assert "telegram" not in hass.data[DOMAIN]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN]["telegram"]._running
    await _outage(hass, entry, clock)
    assert len(_sends(aioclient_mock)) == 1


async def _admin_keystore(hass) -> _Admin:
    admin = _Admin()
    keystore = SvitgridKeystore(hass)
    await keystore.save(
        api_key="k",
        public_key_hex="pub",
        private_key_pem="pem",
        signing_key_id="sk",
        trusted_key_ids=[admin.key_id],
        trusted_public_keys_hex={admin.key_id: admin.pub},
    )
    await keystore.async_add_island_key("phone-1", "p" * 43, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = keystore
    return admin


async def test_cloud_copy_on_forgets_the_households_it_covers(hass, station, aioclient_mock, clock):
    """A household the cloud copy covers starts fresh when it returns to island,
    so it never gets a second restore of an outage the cloud already closed."""
    await _bot(hass)
    admin = await _admin_keystore(hass)
    entry = _entry(trusted_keys=[{"keyId": admin.key_id, "publicKeyHex": admin.pub}])
    entry.add_to_hass(hass)
    state = await tv.telegram_state(hass)
    await state.set_detector({"households": {"hh1": {"out": True}, "other": {"out": True}}})
    await state.set_queue(
        [
            {"id": "a", "householdId": "hh1", "kind": "outage"},
            {"id": "b", "householdId": "other", "kind": "outage"},
        ]
    )

    with (
        patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        resp = await SvitgridIslandCloudCopyView().post(
            _Req(hass, admin.request("set_cloud_copy", enabled=True))
        )
    assert resp.status == 200
    assert list(state.detector()["households"]) == ["other"]
    assert [e["id"] for e in state.queue()] == ["b"]
    assert state.token() == TOKEN  # the cloud copy is not leaving the island


async def test_cloud_copy_off_keeps_the_detector(hass, station, aioclient_mock, clock):
    await _bot(hass)
    admin = await _admin_keystore(hass)
    entry = _entry(
        cloud_copy=True, trusted_keys=[{"keyId": admin.key_id, "publicKeyHex": admin.pub}]
    )
    entry.add_to_hass(hass)
    state = await tv.telegram_state(hass)
    await state.set_detector({"households": {"hh1": {"out": True}}})

    with (
        patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS),
        patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)),
    ):
        resp = await SvitgridIslandCloudCopyView().post(
            _Req(hass, admin.request("set_cloud_copy", enabled=False))
        )
    assert resp.status == 200
    assert list(state.detector()["households"]) == ["hh1"]


async def test_removing_the_last_entry_clears_telegram_state(hass, station, aioclient_mock, clock):
    """The station changes hands: its bot, chats, codes, queue and detector go."""
    await _bot(hass)
    state = await tv.telegram_state(hass)
    await state.issue_code("phone-2", "uk", clock.now)
    await state.set_queue([{"id": "a", "householdId": "hh1", "kind": "outage"}])
    await state.set_detector({"households": {"hh1": {"out": True}}})
    entry = _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert state.token() is None
    assert state.chats() == []
    assert not state.has_pending_codes(clock.now)
    assert state.queue() == [] and state.detector() == {}


async def test_removing_one_of_two_entries_keeps_telegram_state(
    hass, station, aioclient_mock, clock
):
    await _bot(hass)
    state = await tv.telegram_state(hass)
    MockConfigEntry(domain=DOMAIN, data={"local_only": True}).add_to_hass(hass)
    entry = _entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert state.token() == TOKEN
    assert [c.device_id for c in state.chats()] == ["phone-1"]
