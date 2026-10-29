"""Inverter settings served over the LAN, read fast after setup.

The app's onboarding lands the owner on a filled settings page, in cloud mode
and in island mode. That needs four things from the settings-sync loop:

- One connection at a time per logger. A settings read and a harvest read
  racing on the same Solarman logger made the first settings read fail.
- A fast retry until the first successful read, instead of waiting 300 s.
- A cache of the last successful read, which the island view serves.
- No settings POST while cloud ingest is off: nothing leaves the house.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace

from custom_components.svitgrid import settings_sync
from custom_components.svitgrid.api_client import SettingsSyncRejected
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.harvest import transport
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.http_views import SvitgridSettingsView, register_views
from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid.settings_sync import (
    read_config_registers,
    settings_sync_loop,
    settings_sync_tick,
)

_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class _FakeHass:
    is_stopping = False

    def __init__(self) -> None:
        self.data: dict = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _OverlapHass(_FakeHass):
    """Runs executor jobs after a real await, recording how many overlap."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.peak = 0

    async def async_add_executor_job(self, func, *args):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            return func(*args)
        finally:
            self.active -= 1


class _FakeApiClient:
    def __init__(self, ok: bool = True, rejected_status: int | None = None) -> None:
        self.ok = ok
        self.rejected_status = rejected_status
        self.calls: list[dict] = []

    async def sync_settings(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        if self.rejected_status is not None:
            raise SettingsSyncRejected(self.rejected_status, "rejected")
        return self.ok


def _harvest_inv(inverter_id: str = "inv-1", ip: str = "192.168.1.50") -> dict:
    return {
        "inverter_id": inverter_id,
        "harvest_config": {
            "protocol": "solarman_v5",
            "ip": ip,
            "port": 8899,
            "slave_id": 1,
            "model_id": "deye_sg04lp3",
            "logger_serial": "12345",
        },
    }


def _stub_read(monkeypatch, fail_ips=lambda ip, n: False) -> list[str]:
    """Stub the Solarman read. ``fail_ips(ip, call_index)`` decides failures.

    Returns the list of ips read, one entry per transport call.
    """
    calls: list[str] = []

    def fake_read_solarman(cfg, ranges):
        calls.append(cfg["ip"])
        if fail_ips(cfg["ip"], len(calls) - 1):
            raise OSError("connection refused")
        out: dict = {}
        for unit_id, start, count, _fc in ranges:
            slot = out.setdefault(unit_id, {})
            for addr in range(start, start + count):
                slot[addr] = addr
        return out

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)
    return calls


def _entry(inverters: list[dict], cloud_ingest_enabled: bool = True):
    return SimpleNamespace(
        entry_id="entry-1",
        data={
            "api_base": "https://api.example.com",
            "api_key": "key",
            "inverters": inverters,
            "cloud_ingest_enabled": cloud_ingest_enabled,
        },
        options={},
    )


def _stop_after(n: int, lifecycle, sleeps: list[float], on_sleep=None):
    async def sleep_fn(seconds):
        sleeps.append(seconds)
        if on_sleep is not None:
            on_sleep(len(sleeps))
        if len(sleeps) >= n:
            lifecycle.active = False

    return sleep_fn


# ── 1. One connection at a time per logger ─────────────────────────────


_SPEC = RegisterSpec.from_dict(
    {
        "modelId": "deye_sg04lp3",
        "version": 1,
        "protocol": "solarman_v5",
        "port": 8899,
        "defaultSlaveId": 1,
        "reads": [{"field": "batterySoc", "address": 588}],
        "derivations": [],
        "writes": [],
    }
)


async def test_settings_read_and_harvest_read_never_overlap_on_one_logger(monkeypatch):
    _stub_read(monkeypatch)
    hass = _OverlapHass()
    cfg = _harvest_inv()["harvest_config"]

    raw, registers = await asyncio.gather(
        transport.read_raw(hass, _SPEC, cfg),
        read_config_registers(hass, cfg, "deye_sg04lp3"),
    )

    assert raw[1][588] == 588
    assert registers == list(range(115, 115 + 77))
    assert hass.peak == 1


async def test_settings_read_waits_for_a_write_on_the_same_logger(monkeypatch):
    _stub_read(monkeypatch)
    monkeypatch.setattr(transport, "_write_solarman", lambda cfg, writes: None)
    hass = _OverlapHass()
    cfg = _harvest_inv()["harvest_config"]

    await asyncio.gather(
        transport.write_registers(hass, _SPEC, cfg, [(1, 142, 1)]),
        transport.read_word(hass, _SPEC, cfg, 1, 588),
        read_config_registers(hass, cfg, "deye_sg04lp3"),
    )

    assert hass.peak == 1


async def test_reads_of_different_loggers_still_run_side_by_side(monkeypatch):
    _stub_read(monkeypatch)
    hass = _OverlapHass()

    await asyncio.gather(
        transport.read_raw(hass, _SPEC, _harvest_inv(ip="10.0.0.1")["harvest_config"]),
        read_config_registers(hass, _harvest_inv(ip="10.0.0.2")["harvest_config"], "deye_sg04lp3"),
    )

    assert hass.peak == 2


# ── 2. Fast first read ─────────────────────────────────────────────────


async def test_first_read_retries_fast_and_stops_after_the_first_success(monkeypatch):
    # Three failed reads, then success from the fourth onwards.
    _stub_read(monkeypatch, fail_ips=lambda ip, n: n < 3)
    lifecycle = SimpleNamespace(active=True)
    sleeps: list[float] = []

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()]),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(6, lifecycle, sleeps),
        now_monotonic_fn=lambda: 1000.0,
    )

    assert sleeps == [5, 10, 20, 300, 300, 300]


async def test_first_read_falls_back_to_the_interval_after_four_retries(monkeypatch):
    _stub_read(monkeypatch, fail_ips=lambda ip, n: True)
    lifecycle = SimpleNamespace(active=True)
    sleeps: list[float] = []

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()]),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(7, lifecycle, sleeps),
    )

    assert sleeps == [5, 10, 20, 30, 300, 300, 300]


async def test_fast_retry_reads_only_the_inverters_not_read_yet(monkeypatch):
    # inv-b's logger fails its first read only.
    calls = _stub_read(monkeypatch, fail_ips=lambda ip, n: ip == "10.0.0.2" and n < 2)
    lifecycle = SimpleNamespace(active=True)
    sleeps: list[float] = []

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv("inv-a", "10.0.0.1"), _harvest_inv("inv-b", "10.0.0.2")]),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(2, lifecycle, sleeps),
        now_monotonic_fn=lambda: 1000.0,
    )

    assert sleeps == [5, 300]
    assert calls == ["10.0.0.1", "10.0.0.2", "10.0.0.2"]


# ── 3. A failed read is a WARNING, once per failure streak ─────────────


async def test_failed_read_warns_once_per_failure_streak(monkeypatch, caplog):
    outcomes = iter([True, True, True, False, True])
    _stub_read(monkeypatch, fail_ips=lambda ip, n: next(outcomes))
    hass = _FakeHass()
    failing: set[str] = set()

    def warnings() -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == settings_sync.__name__
        ]

    with caplog.at_level(logging.DEBUG, logger=settings_sync.__name__):
        for _ in range(3):
            await settings_sync_tick(
                hass, _FakeApiClient(), "key", [_harvest_inv()], {}, failing=failing
            )
        assert len(warnings()) == 1
        assert "inv-1" in warnings()[0].getMessage()
        assert "connection refused" in warnings()[0].getMessage()

        # A success ends the streak; the next failure warns again.
        await settings_sync_tick(
            hass, _FakeApiClient(), "key", [_harvest_inv()], {}, failing=failing
        )
        await settings_sync_tick(
            hass, _FakeApiClient(), "key", [_harvest_inv()], {}, failing=failing
        )
        assert len(warnings()) == 2


# ── 4 + 5. Cache every read; POST only while cloud ingest is on ────────


async def test_cloud_ingest_off_reads_and_caches_but_never_posts(monkeypatch):
    _stub_read(monkeypatch)
    api = _FakeApiClient()
    snapshots: dict = {}

    await settings_sync_tick(
        _FakeHass(),
        api,
        "key",
        [_harvest_inv()],
        {},
        cloud_ingest_enabled=False,
        snapshots=snapshots,
    )

    assert api.calls == []
    snap = snapshots["inv-1"]
    assert snap["inverter_id"] == "inv-1"
    assert snap["model_id"] == "deye_sg04lp3"
    assert snap["start_register"] == 115
    assert snap["registers"] == list(range(115, 115 + 77))
    assert _ISO_Z.match(snap["read_at"])


async def test_settings_read_runs_on_local_only_entry_without_api_key(monkeypatch):
    """A local-only entry has no account: no api_key, no api_base. The loop
    must still read and cache, and never build a client or post."""
    _stub_read(monkeypatch)
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"inverters": [_harvest_inv()], "local_only": True},
        options={},
    )
    lifecycle = SimpleNamespace(active=True)
    snapshots: dict = {}
    sleeps: list[float] = []

    await settings_sync_loop(
        _FakeHass(),
        entry,
        lifecycle=lifecycle,
        cloud_upload=False,
        sleep_fn=_stop_after(1, lifecycle, sleeps),
        snapshots=snapshots,
    )

    assert snapshots["inv-1"]["registers"] == list(range(115, 115 + 77))


async def test_cloud_upload_off_never_posts_even_with_the_copy_on(monkeypatch):
    """A true island keeps the cloud out even if its entry still says the copy
    is on: cloud_upload=False wins over the flag."""
    _stub_read(monkeypatch)
    api = _FakeApiClient()
    lifecycle = SimpleNamespace(active=True)
    snapshots: dict = {}

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()], cloud_ingest_enabled=True),
        lifecycle=lifecycle,
        api_client=api,
        cloud_upload=False,
        sleep_fn=_stop_after(1, lifecycle, []),
        snapshots=snapshots,
    )

    assert api.calls == []
    assert "inv-1" in snapshots


async def test_cloud_ingest_on_caches_every_read_even_when_the_hash_gate_skips(monkeypatch):
    _stub_read(monkeypatch)
    api = _FakeApiClient()
    cache: dict = {}
    snapshots: dict = {}
    clock = [1000.0]

    await settings_sync_tick(
        _FakeHass(),
        api,
        "key",
        [_harvest_inv()],
        cache,
        snapshots=snapshots,
        now_monotonic_fn=lambda: clock[0],
    )
    first = snapshots["inv-1"]
    clock[0] += 60
    await settings_sync_tick(
        _FakeHass(),
        api,
        "key",
        [_harvest_inv()],
        cache,
        snapshots=snapshots,
        now_monotonic_fn=lambda: clock[0],
    )

    assert len(api.calls) == 1  # hash gate unchanged
    assert snapshots["inv-1"] is not first  # but the cache took the new read


async def test_cloud_ingest_off_ignores_the_4xx_post_backoff(monkeypatch):
    calls = _stub_read(monkeypatch)
    snapshots: dict = {}

    await settings_sync_tick(
        _FakeHass(),
        _FakeApiClient(),
        "key",
        [_harvest_inv()],
        {},
        now_monotonic_fn=lambda: 0.0,
        backoff_until={"inv-1": 10_000.0},
        cloud_ingest_enabled=False,
        snapshots=snapshots,
    )

    assert calls == ["192.168.1.50"]
    assert "inv-1" in snapshots


async def test_loop_follows_a_live_change_of_the_cloud_ingest_flag(monkeypatch):
    _stub_read(monkeypatch)
    lifecycle = SimpleNamespace(active=True)
    sleeps: list[float] = []
    api = _FakeApiClient()
    entry = _entry([_harvest_inv()], cloud_ingest_enabled=False)
    posts_while_off: list[int] = []

    def flip(n):
        posts_while_off.append(len(api.calls))
        entry.data = {**entry.data, "cloud_ingest_enabled": True}

    await settings_sync_loop(
        _FakeHass(),
        entry,
        lifecycle=lifecycle,
        api_client=api,
        sleep_fn=_stop_after(2, lifecycle, sleeps, on_sleep=flip),
    )

    assert posts_while_off[0] == 0
    assert len(api.calls) == 1  # the tick after the flag turned on


async def test_cloud_ingest_absent_defaults_to_posting(monkeypatch):
    _stub_read(monkeypatch)
    lifecycle = SimpleNamespace(active=True)
    api = _FakeApiClient()
    entry = _entry([_harvest_inv()])
    del entry.data["cloud_ingest_enabled"]

    await settings_sync_loop(
        _FakeHass(),
        entry,
        lifecycle=lifecycle,
        api_client=api,
        sleep_fn=_stop_after(1, lifecycle, []),
    )

    assert len(api.calls) == 1


# ── 4. Persisted across a restart ──────────────────────────────────────


class _MemoryStore:
    def __init__(self, saved: dict | None = None) -> None:
        self.saved = dict(saved or {})

    async def get_settings_snapshot(self, inverter_id):
        return self.saved.get(inverter_id)

    async def set_settings_snapshot(self, snapshot):
        self.saved[snapshot["inverter_id"]] = snapshot


async def test_loop_persists_a_successful_read(monkeypatch):
    _stub_read(monkeypatch)
    lifecycle = SimpleNamespace(active=True)
    store = _MemoryStore()

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()], cloud_ingest_enabled=False),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(1, lifecycle, []),
        store=store,
    )

    assert store.saved["inv-1"]["registers"] == list(range(115, 115 + 77))


async def test_restart_serves_the_persisted_read_and_still_retries_fast(monkeypatch):
    _stub_read(monkeypatch, fail_ips=lambda ip, n: True)
    saved = {
        "inverter_id": "inv-1",
        "model_id": "deye_sg04lp3",
        "start_register": 115,
        "registers": [7] * 77,
        "read_at": "2026-09-14T10:00:00Z",
    }
    lifecycle = SimpleNamespace(active=True)
    sleeps: list[float] = []
    snapshots: dict = {}

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()], cloud_ingest_enabled=False),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(2, lifecycle, sleeps),
        store=_MemoryStore({"inv-1": saved}),
        snapshots=snapshots,
    )

    assert snapshots["inv-1"] == saved
    assert sleeps == [5, 10]  # a persisted read is not a read of this run


async def test_persisted_read_of_another_model_is_not_served(monkeypatch):
    _stub_read(monkeypatch, fail_ips=lambda ip, n: True)
    lifecycle = SimpleNamespace(active=True)
    snapshots: dict = {}

    await settings_sync_loop(
        _FakeHass(),
        _entry([_harvest_inv()], cloud_ingest_enabled=False),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(1, lifecycle, []),
        store=_MemoryStore({"inv-1": {"inverter_id": "inv-1", "model_id": "deye_sg02lp1"}}),
        snapshots=snapshots,
    )

    assert snapshots == {}


def test_reading_store_round_trips_a_settings_snapshot(tmp_path):
    db = str(tmp_path / "readings.db")
    snap = {
        "inverter_id": "inv-1",
        "model_id": "deye_sg04lp3",
        "start_register": 115,
        "registers": [1, 2, 3],
        "read_at": "2026-09-15T08:00:00Z",
    }
    ReadingStore(None, db)._set_settings_snapshot_sync(snap)

    fresh = ReadingStore(None, db)
    assert fresh._get_settings_snapshot_sync("inv-1") == snap
    assert fresh._get_settings_snapshot_sync("inv-unknown") is None


# ── 6. GET /api/svitgrid/settings ──────────────────────────────────────


class _Req:
    def __init__(self, hass, query=None, authenticated=True) -> None:
        self.app = {"hass": hass}
        self.query = query or {}
        self.headers = {}
        self._data = {"ha_authenticated": authenticated}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]


def _hass_with_snapshots(snapshots: dict, inverters=("inv-1", "inv-2")):
    entry_data = {"inverters": [{"inverter_id": i, "model_id": "deye_sg04lp3"} for i in inverters]}
    return SimpleNamespace(
        data={DOMAIN: {"entry-1": {"settings_snapshots": snapshots, "entry_data": entry_data}}}
    )


_SNAPSHOT = {
    "inverter_id": "inv-1",
    "model_id": "deye_sg04lp3",
    "start_register": 115,
    "registers": [10, 20, 30],
    "read_at": "2026-09-15T08:00:00Z",
}


async def test_settings_view_requires_auth():
    view = SvitgridSettingsView(store=None)
    resp = await view.get(
        _Req(_hass_with_snapshots({"inv-1": _SNAPSHOT}), {"inverter_id": "inv-1"}, False)
    )
    assert resp.status == 401


async def test_settings_view_without_inverter_id_is_a_bad_request():
    view = SvitgridSettingsView(store=None)
    resp = await view.get(_Req(_hass_with_snapshots({"inv-1": _SNAPSHOT})))
    assert resp.status == 400
    assert json.loads(resp.body) == {"error": "missing_inverter_id"}


async def test_settings_view_before_a_read_is_not_found():
    view = SvitgridSettingsView(store=None)
    resp = await view.get(
        _Req(_hass_with_snapshots({"inv-1": _SNAPSHOT}), {"inverter_id": "inv-2"})
    )
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "not_read_yet"}


async def test_settings_unknown_inverter_is_404_unknown_inverter():
    view = SvitgridSettingsView(store=None)
    resp = await view.get(
        _Req(_hass_with_snapshots({"inv-1": _SNAPSHOT}), {"inverter_id": "unknown"})
    )
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "unknown_inverter"}


async def test_settings_view_returns_the_last_read():
    view = SvitgridSettingsView(store=None)
    resp = await view.get(
        _Req(_hass_with_snapshots({"inv-1": _SNAPSHOT}), {"inverter_id": "inv-1"})
    )
    assert resp.status == 200
    assert json.loads(resp.body) == {
        "inverterId": "inv-1",
        "modelId": "deye_sg04lp3",
        "startRegister": 115,
        "registers": [10, 20, 30],
        "readAt": "2026-09-15T08:00:00Z",
    }


def test_settings_view_is_registered():
    registered = []
    hass = SimpleNamespace(http=SimpleNamespace(register_view=registered.append))
    register_views(hass, store=None)
    assert any(isinstance(v, SvitgridSettingsView) for v in registered)
    assert SvitgridSettingsView.url == "/api/svitgrid/settings"


# ── 7. Every brand with settings, and nothing to configure for the rest ─


_CBAA = {7: ord("c") | ord("B") << 8, 8: ord("a") | ord("a") << 8}


def _stub_modbus(monkeypatch, overrides: dict | None = None) -> None:
    def fake_read_modbus(cfg, ranges):
        out: dict = {}
        for unit_id, start, count, _fc in ranges:
            slot = out.setdefault(unit_id, {})
            for addr in range(start, start + count):
                slot[addr] = (overrides or {}).get(addr, addr & 0xFFFF)
        return out

    monkeypatch.setattr(transport, "_read_modbus", fake_read_modbus)


def _tcp_inv(inverter_id: str, model_id: str, slave_id: int = 1, port: int = 502) -> dict:
    return {
        "inverter_id": inverter_id,
        "harvest_config": {
            "protocol": "modbus_tcp",
            "ip": "192.168.1.60",
            "port": port,
            "slave_id": slave_id,
            "model_id": model_id,
        },
    }


class _Session:
    def __init__(self, words: dict[int, int]) -> None:
        self.words = words

    async def read_registers(self, address, count, timeout_s=5.0):
        return [self.words.get(a, a & 0xFFFF) for a in range(address, address + count)]


class _Hub:
    def __init__(self, session) -> None:
        self.session = session

    def session_for(self, serial):
        return self.session if serial == "SN1" else None


async def _read_and_serve(inv: dict, *, eybond_hub=None) -> tuple[_FakeApiClient, dict]:
    """One tick with the cloud copy ON, then GET /settings for the inverter."""
    api = _FakeApiClient()
    snapshots: dict = {}
    await settings_sync_tick(
        _FakeHass(),
        api,
        "key",
        [inv],
        {},
        snapshots=snapshots,
        eybond_hub=eybond_hub,
    )
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "entry-1": {"settings_snapshots": snapshots, "entry_data": {"inverters": [inv]}}
            }
        }
    )
    resp = await SvitgridSettingsView(store=None).get(
        _Req(hass, {"inverter_id": inv["inverter_id"]})
    )
    assert resp.status == 200, resp.body
    return api, json.loads(resp.body)


def _block_shape(body: dict) -> list[tuple[int, int]]:
    return [(b["startRegister"], len(b["registers"])) for b in body["blocks"]]


async def test_settings_serves_luxpower_snapshot_shape(monkeypatch):
    """The body `luxpower_build_sync_body` posts: five blocks of 40 from
    holding 0, as the app's `luxpowerCurrentState` parses it."""
    _stub_modbus(monkeypatch, _CBAA)
    api, body = await _read_and_serve(_tcp_inv("inv-lux", "luxpower_sna_5000_wpv", port=8899))

    assert api.calls == []  # the cloud upload stays Deye-only
    assert set(body) == {"inverterId", "modelId", "readAt", "protocol", "familyCode", "blocks"}
    assert body["inverterId"] == "inv-lux"
    assert body["modelId"] == "luxpower_sna_5000_wpv"
    assert body["protocol"] == "luxpower"
    assert body["familyCode"] == "cBaa"
    assert body["blocks"][0]["startRegister"] == 0
    assert _block_shape(body) == [(0, 40), (40, 40), (80, 40), (120, 40), (160, 40)]
    assert _ISO_Z.match(body["readAt"])


async def test_settings_serves_must_snapshot_shape(monkeypatch):
    _stub_modbus(monkeypatch, {20006: 10415, 25275: 24})
    api, body = await _read_and_serve(_tcp_inv("inv-must", "must_pv18_5248", slave_id=4))

    assert api.calls == []
    assert set(body) == {
        "inverterId",
        "modelId",
        "readAt",
        "protocol",
        "edition",
        "packVoltage",
        "blocks",
    }
    assert body["protocol"] == "must"
    assert body["edition"] == "1.04.15"
    assert body["packVoltage"] == 24
    assert _block_shape(body) == [(10101, 12), (20101, 43)]


async def test_settings_serves_smg_snapshot_shape():
    inv = {
        "inverter_id": "inv-smg",
        "harvest_config": {
            "protocol": "eybond_at",
            "model_id": "anenji_anj_6200",
            "inverter_serial": "SN1",
        },
    }
    api, body = await _read_and_serve(inv, eybond_hub=_Hub(_Session({184: 11})))

    assert api.calls == []
    assert set(body) == {"inverterId", "modelId", "readAt", "protocolNumber", "blocks"}
    assert body["protocolNumber"] == 11
    assert _block_shape(body) == [(303, 1), (313, 31)]


async def test_settings_serves_solis_snapshot_shape(monkeypatch):
    _stub_modbus(monkeypatch)
    api, body = await _read_and_serve(_tcp_inv("inv-solis", "solis_s6_eh3p_h"))

    assert api.calls == []
    assert set(body) == {"inverterId", "modelId", "readAt", "protocol", "registers"}
    assert body["protocol"] == "solis"
    assert len(body["registers"]) == 23
    assert body["registers"]["43011"] == 43011
    assert body["registers"]["43118"] == 43118


def _hass_with_inverter(inv: dict, snapshots: dict | None = None):
    return SimpleNamespace(
        data={
            DOMAIN: {
                "entry-1": {
                    "settings_snapshots": snapshots or {},
                    "entry_data": {"inverters": [inv]},
                }
            }
        }
    )


async def test_settings_answers_nothing_to_configure_for_huawei():
    hass = _hass_with_inverter(_tcp_inv("inv-hw", "huawei_sun2000_30ktl_m3"))
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-hw"}))
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "nothing_to_configure"}


async def test_settings_answers_nothing_to_configure_for_solax_and_solis_string():
    for model_id in ("solax_x3_hybrid_g4_15k", "solis_30k_5g", "luxpower_lxp_lb_eu_7k"):
        hass = _hass_with_inverter(_tcp_inv("inv-x", model_id))
        resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-x"}))
        assert json.loads(resp.body) == {"error": "nothing_to_configure"}, model_id


async def test_settings_answers_not_read_yet_for_unread_deye():
    hass = _hass_with_inverter(_harvest_inv("inv-deye"))
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-deye"}))
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "not_read_yet"}


async def test_settings_answers_not_read_yet_for_pi30_it_cannot_read():
    """Edecoa has settings; this add-on cannot harvest PI30, so the truth is
    "not read yet", never "nothing to configure"."""
    hass = _hass_with_inverter(_tcp_inv("inv-pi30", "edecoa_em_624a"))
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-pi30"}))
    assert json.loads(resp.body) == {"error": "not_read_yet"}


async def test_loop_reads_smg_through_the_entry_eybond_hub():
    """Setup stores the EyBond hub in hass.data; the loop finds it there."""
    inv = {
        "inverter_id": "inv-smg",
        "harvest_config": {
            "protocol": "eybond_at",
            "model_id": "anenji_anj_11kw",
            "inverter_serial": "SN1",
        },
    }
    hass = _FakeHass()
    hass.data = {DOMAIN: {"entry-1": {"eybond_hub": _Hub(_Session({184: 4}))}}}
    lifecycle = SimpleNamespace(active=True)
    snapshots: dict = {}
    api = _FakeApiClient()

    await settings_sync_loop(
        hass,
        _entry([inv]),
        lifecycle=lifecycle,
        api_client=api,
        sleep_fn=_stop_after(1, lifecycle, []),
        snapshots=snapshots,
    )

    assert api.calls == []
    assert snapshots["inv-smg"]["kind"] == "smg"
    assert snapshots["inv-smg"]["body"]["protocolNumber"] == 4


def test_reading_store_round_trips_a_non_deye_snapshot(tmp_path):
    db = str(tmp_path / "readings.db")
    snap = {
        "inverter_id": "inv-must",
        "model_id": "must_pv18_5248",
        "kind": "must",
        "body": {
            "protocol": "must",
            "edition": "1.04.15",
            "packVoltage": 24,
            "blocks": [{"startRegister": 10101, "registers": [1] * 12}],
        },
        "read_at": "2026-10-28T08:00:00Z",
    }
    ReadingStore(None, db)._set_settings_snapshot_sync(snap)

    assert ReadingStore(None, db)._get_settings_snapshot_sync("inv-must") == snap


async def test_restart_serves_a_persisted_non_deye_snapshot(monkeypatch):
    def failing_read(cfg, ranges):
        raise OSError("connection refused")

    monkeypatch.setattr(transport, "_read_modbus", failing_read)
    inv = _tcp_inv("inv-solis", "solis_s6_eh3p_h")
    saved = {
        "inverter_id": "inv-solis",
        "model_id": "solis_s6_eh3p_h",
        "kind": "solis",
        "body": {"protocol": "solis", "registers": {"43011": 20}},
        "read_at": "2026-10-28T08:00:00Z",
    }
    lifecycle = SimpleNamespace(active=True)
    snapshots: dict = {}

    await settings_sync_loop(
        _FakeHass(),
        _entry([inv], cloud_ingest_enabled=False),
        lifecycle=lifecycle,
        api_client=_FakeApiClient(),
        sleep_fn=_stop_after(1, lifecycle, []),
        store=_MemoryStore({"inv-solis": saved}),
        snapshots=snapshots,
    )

    hass = _hass_with_inverter(inv, snapshots)
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-solis"}))
    assert json.loads(resp.body) == {
        "inverterId": "inv-solis",
        "modelId": "solis_s6_eh3p_h",
        "protocol": "solis",
        "registers": {"43011": 20},
        "readAt": "2026-10-28T08:00:00Z",
    }


# ── The settings view for an edge box's inverter (svitgrid#1204) ────────


class _FakeKeystore:
    def __init__(self, boxes):
        edge_boxes = {b.device_id: b for b in boxes}
        self._state = SimpleNamespace(
            edge_boxes=edge_boxes,
            edge_box_for_api_key=lambda key: next(
                (b for b in edge_boxes.values() if key and b.api_key == key), None
            ),
        )

    async def load(self):
        return self._state

    async def async_get_island_keys(self):
        return []


class _FakeSnapshotStore:
    def __init__(self, snapshots=None):
        self.snapshots = dict(snapshots or {})

    async def get_settings_snapshot(self, inverter_id):
        return self.snapshots.get(inverter_id)

    async def set_settings_snapshot(self, snapshot):
        self.snapshots[snapshot["inverter_id"]] = snapshot


def _edge_box(inverters: dict):
    from custom_components.svitgrid.keystore import EdgeBoxEntry

    return EdgeBoxEntry(
        device_id="EC-1",
        api_key="box-key",
        inverter_ids=list(inverters),
        inverters=inverters,
        added_at="2026-10-28T10:00:00+00:00",
        pair_code_hash=None,
        pair_code_expires_at=None,
    )


def _edge_hass(store, inverters=None):
    """An edge island: the entry harvests nothing, the box holds the inverter."""
    inverters = inverters if inverters is not None else {"inv-e": {"modelId": "deye_sg04lp3"}}
    return SimpleNamespace(
        data={
            DOMAIN: {
                "keystore": _FakeKeystore([_edge_box(inverters)]),
                "entry-1": {
                    "settings_snapshots": {},
                    "entry_data": {"inverters": []},
                    "store": store,
                },
            }
        }
    )


_EDGE_SNAPSHOT = {
    "inverter_id": "inv-e",
    "model_id": "deye_sg04lp3",
    "start_register": 115,
    "registers": [4, 5, 6],
    "read_at": "2026-10-29T00:00:00Z",
}


async def test_settings_view_serves_edge_snapshot():
    """What the box posts to sync-settings is what the app reads back."""
    from custom_components.svitgrid.edge_api import EdgeSyncSettingsView

    store = _FakeSnapshotStore()
    hass = _edge_hass(store)
    post = SimpleNamespace(app={"hass": hass}, headers={"X-API-Key": "box-key"}, match_info={})

    async def _json_body():
        return {
            "inverterId": "inv-e",
            "modelId": "deye_sg04lp3",
            "startRegister": 115,
            "registers": [4, 5, 6],
        }

    post.json = _json_body
    resp = await EdgeSyncSettingsView().post(post)
    assert json.loads(resp.body) == {"ok": True}

    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-e"}))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["inverterId"] == "inv-e"
    assert body["modelId"] == "deye_sg04lp3"
    assert body["startRegister"] == 115
    assert body["registers"] == [4, 5, 6]
    assert _ISO_Z.match(body["readAt"])


async def test_settings_view_serves_a_persisted_edge_snapshot_after_restart():
    """The box posts settings only when they change, so after a restart the
    view serves what the store kept."""
    hass = _edge_hass(_FakeSnapshotStore({"inv-e": _EDGE_SNAPSHOT}))
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-e"}))
    assert resp.status == 200
    assert json.loads(resp.body) == settings_sync.settings_snapshot_response(_EDGE_SNAPSHOT)


async def test_settings_view_does_not_serve_a_persisted_edge_snapshot_of_another_model():
    hass = _edge_hass(
        _FakeSnapshotStore({"inv-e": _EDGE_SNAPSHOT}),
        inverters={"inv-e": {"modelId": "deye_sg01hp3"}},
    )
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-e"}))
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "not_read_yet"}


async def test_settings_view_edge_inverter_before_sync_is_not_read_yet():
    hass = _edge_hass(_FakeSnapshotStore())
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-e"}))
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "not_read_yet"}


async def test_settings_view_edge_inverter_with_nothing_to_configure():
    hass = _edge_hass(_FakeSnapshotStore(), inverters={"inv-e": {"modelId": "huawei_sun2000"}})
    resp = await SvitgridSettingsView(store=None).get(_Req(hass, {"inverter_id": "inv-e"}))
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "nothing_to_configure"}
