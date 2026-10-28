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
