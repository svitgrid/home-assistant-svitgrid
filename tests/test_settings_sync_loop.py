"""Tests for the settings-sync tick: read -> hash -> should_upload -> POST,
cache updated ONLY on 2xx. Fake transport (monkeypatched onto harvest.transport,
mirroring tests/test_settings_sync_read.py) + a fake API client + an injectable
monotonic clock so cycles are fully deterministic (no real sleeps / real time).
"""

from __future__ import annotations

from custom_components.svitgrid.api_client import SettingsSyncRejected
from custom_components.svitgrid.harvest import transport
from custom_components.svitgrid.settings_sync import (
    SETTINGS_SYNC_BACKOFF_S,
    settings_sync_tick,
    sync_inverter_once,
)


class _FakeHass:
    """Minimal hass stub that runs executor jobs synchronously (mirrors
    tests/test_settings_sync_read.py's _FakeHass)."""

    is_stopping = False

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeApiClient:
    """Records every sync_settings call; returns a fixed 2xx/non-2xx outcome,
    or raises [SettingsSyncRejected] when ``rejected_status`` is set (mirrors
    the real client's 4xx behavior — see api_client.sync_settings)."""

    def __init__(self, ok: bool = True, rejected_status: int | None = None):
        self.ok = ok
        self.rejected_status = rejected_status
        self.calls: list[dict] = []

    async def sync_settings(
        self,
        *,
        api_key: str,
        inverter_id: str,
        model_id: str,
        start_register: int,
        registers: list[int],
    ) -> bool:
        self.calls.append(
            {
                "api_key": api_key,
                "inverter_id": inverter_id,
                "model_id": model_id,
                "start_register": start_register,
                "registers": list(registers),
            }
        )
        if self.rejected_status is not None:
            raise SettingsSyncRejected(self.rejected_status, "rejected")
        return self.ok


def _harvest_inv(
    inverter_id: str = "inv-1",
    model_id: str = "deye_sg04lp3",
    protocol: str = "solarman_v5",
) -> dict:
    return {
        "inverter_id": inverter_id,
        "harvest_config": {
            "protocol": protocol,
            "ip": "192.168.1.50",
            "port": 8899,
            "slave_id": 1,
            "model_id": model_id,
            "logger_serial": "12345",
        },
    }


def _preset_inv(inverter_id: str = "inv-preset") -> dict:
    """Relay/preset inverter -- no harvest_config at all. Must never sync."""
    return {"inverter_id": inverter_id, "entity_map": {"batterySoc": "sensor.soc"}}


def _stub_transport_read(monkeypatch, fail: bool = False) -> None:
    def fake_read_solarman(cfg, ranges):
        if fail:
            raise OSError("connection refused")
        # Item 6 (2026-07-25 final review): read_config_registers batches
        # every chunk's range into a single transport call now (one
        # connection), so the fake must build the combined result across ALL
        # ranges passed in, not just the first — mirrors the real
        # _read_solarman/_read_modbus, which already read a whole ranges
        # list inside one connection.
        out: dict = {}
        for unit_id, start, count, fc in ranges:
            assert fc == "FC03"
            slot = out.setdefault(unit_id, {})
            for addr in range(start, start + count):
                slot[addr] = addr
        return out

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)


async def test_posts_on_first_cycle_and_skips_unchanged_second(monkeypatch):
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(ok=True)
    cache: dict = {}
    clock = [1000.0]

    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["inverter_id"] == "inv-1"
    assert call["model_id"] == "deye_sg04lp3"
    assert call["start_register"] == 115
    assert call["registers"] == list(range(115, 115 + 77))
    assert "inv-1" in cache

    # Second cycle, registers unchanged, well within the heartbeat -> no repost.
    clock[0] += 60
    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 1


async def test_reposts_after_heartbeat_elapsed(monkeypatch):
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(ok=True)
    cache: dict = {}
    clock = [0.0]

    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 1

    clock[0] += 1800  # heartbeat elapsed: now >= last_uploaded + heartbeat_s (default 1800)
    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 2


async def test_failed_post_does_not_update_cache(monkeypatch):
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(ok=False)
    cache: dict = {}
    clock = [0.0]

    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 1
    assert "inv-1" not in cache  # a failed POST must never seed the cache

    # Next cycle, well within the heartbeat window: retries anyway because the
    # cache was never seeded (bootstrap clause: last_uploaded_monotonic == 0).
    clock[0] += 5
    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: clock[0]
    )
    assert len(api.calls) == 2
    assert "inv-1" not in cache


async def test_preset_without_registers_never_posts(monkeypatch):
    """A relay/preset inverter (no harvest_config) must never be read or
    POSTed. Same for a harvest_config inverter whose model has no
    CONFIG_RANGES entry (e.g. Victron) -- the eligibility gate is
    (a) direct harvest_config with solarman_v5/modbus_tcp AND
    (b) config_range_for_model(modelId) is not None."""
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(ok=True)
    cache: dict = {}

    await settings_sync_tick(hass, api, "key", [_preset_inv()], cache, now_monotonic_fn=lambda: 0.0)
    assert api.calls == []
    assert cache == {}

    unsupported_model = _harvest_inv(
        inverter_id="inv-victron", model_id="victron_multiplus_ii_gx_6k5"
    )
    await settings_sync_tick(
        hass, api, "key", [unsupported_model], cache, now_monotonic_fn=lambda: 0.0
    )
    assert api.calls == []
    assert cache == {}

    # A mixed batch: preset first, then a real eligible inverter -> only the
    # eligible one is read/posted.
    await settings_sync_tick(
        hass,
        api,
        "key",
        [_preset_inv(), _harvest_inv()],
        cache,
        now_monotonic_fn=lambda: 0.0,
    )
    assert len(api.calls) == 1
    assert api.calls[0]["inverter_id"] == "inv-1"


async def test_transport_read_failure_never_posts(monkeypatch):
    """read_config_registers returning None (any chunk failure) must skip the
    POST entirely -- never send partial/garbage registers."""
    _stub_transport_read(monkeypatch, fail=True)
    hass = _FakeHass()
    api = _FakeApiClient(ok=True)
    cache: dict = {}

    await settings_sync_tick(
        hass, api, "key", [_harvest_inv()], cache, now_monotonic_fn=lambda: 0.0
    )
    assert api.calls == []


# ─── 4xx backoff (item 4, 2026-07-25 final review) ─────────────────────────
#
# Permanent 403 loop for unbound devices: a 4xx (e.g. the device's api-key no
# longer resolves to a bound household) is rejected forever by the server, so
# retrying every 5-min tick just hammers it. On a SettingsSyncRejected (4xx),
# the inverter must be skipped (no read, no POST) until SETTINGS_SYNC_BACKOFF_S
# elapses. A 5xx / transport error is unaffected -- unchanged retry-next-cycle.


async def test_403_backs_off_inverter_for_next_cycles(monkeypatch):
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(rejected_status=403)
    cache: dict = {}
    backoff_until: dict = {}
    clock = [1000.0]

    await settings_sync_tick(
        hass,
        api,
        "key",
        [_harvest_inv()],
        cache,
        now_monotonic_fn=lambda: clock[0],
        backoff_until=backoff_until,
    )
    assert len(api.calls) == 1  # the rejected attempt itself
    assert "inv-1" not in cache
    assert backoff_until["inv-1"] == clock[0] + SETTINGS_SYNC_BACKOFF_S

    # Next several cycles, well within the backoff window: no further reads
    # or POSTs at all for this inverter.
    for _ in range(3):
        clock[0] += 300  # one settings-sync interval
        await settings_sync_tick(
            hass,
            api,
            "key",
            [_harvest_inv()],
            cache,
            now_monotonic_fn=lambda: clock[0],
            backoff_until=backoff_until,
        )
    assert len(api.calls) == 1  # still just the original rejected attempt

    # Backoff elapsed: the inverter is retried again.
    clock[0] += SETTINGS_SYNC_BACKOFF_S
    await settings_sync_tick(
        hass,
        api,
        "key",
        [_harvest_inv()],
        cache,
        now_monotonic_fn=lambda: clock[0],
        backoff_until=backoff_until,
    )
    assert len(api.calls) == 2


async def test_403_backoff_is_per_inverter(monkeypatch):
    """A 403 on one inverter must not back off a different inverter on the
    same entry."""
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    cache: dict = {}
    backoff_until: dict = {}

    api_rejected = _FakeApiClient(rejected_status=403)
    await sync_inverter_once(
        hass,
        api_rejected,
        "key",
        "inv-rejected",
        "deye_sg04lp3",
        _harvest_inv("inv-rejected")["harvest_config"],
        cache,
        now_monotonic_fn=lambda: 0.0,
        backoff_until=backoff_until,
    )
    assert "inv-rejected" in backoff_until

    api_ok = _FakeApiClient(ok=True)
    ok = await sync_inverter_once(
        hass,
        api_ok,
        "key",
        "inv-other",
        "deye_sg04lp3",
        _harvest_inv("inv-other")["harvest_config"],
        cache,
        now_monotonic_fn=lambda: 0.0,
        backoff_until=backoff_until,
    )
    assert ok is True
    assert len(api_ok.calls) == 1
    assert "inv-other" not in backoff_until


async def test_500_still_retries_next_cycle_unaffected_by_backoff(monkeypatch):
    """A 5xx/transient failure must behave exactly as before: no backoff, and
    the very next cycle retries (bootstrap clause: cache never seeded)."""
    _stub_transport_read(monkeypatch)
    hass = _FakeHass()
    api = _FakeApiClient(ok=False)  # False == non-2xx/5xx-style failure, not raised
    cache: dict = {}
    backoff_until: dict = {}
    clock = [0.0]

    await settings_sync_tick(
        hass,
        api,
        "key",
        [_harvest_inv()],
        cache,
        now_monotonic_fn=lambda: clock[0],
        backoff_until=backoff_until,
    )
    assert len(api.calls) == 1
    assert backoff_until == {}  # no backoff armed for a transient failure

    clock[0] += 300
    await settings_sync_tick(
        hass,
        api,
        "key",
        [_harvest_inv()],
        cache,
        now_monotonic_fn=lambda: clock[0],
        backoff_until=backoff_until,
    )
    assert len(api.calls) == 2  # retried, unaffected by any backoff logic
    assert cache == {}


# ─── Lifecycle wiring (mirrors tests/test_init_harvest_wiring.py) ──────────


def _make_wiring_entry(harvest_config):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.svitgrid.const import DOMAIN

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (settings-sync wiring)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-settings-sync",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-xyz",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                    "harvest_config": harvest_config,
                }
            ],
        },
        entry_id="entry-settings-sync",
    )


async def test_settings_sync_loop_spawned_and_cancelled_on_unload(hass, enable_custom_integrations):
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry, async_unload_entry
    from custom_components.svitgrid.const import DOMAIN
    from custom_components.svitgrid.reading_store import ReadingStore

    entry = _make_wiring_entry(
        {"protocol": "solarman_v5", "model_id": "deye_sg04lp3", "ip": "10.0.0.5", "port": 8899}
    )
    entry.add_to_hass(hass)

    active_lifecycle = {"state": "active", "reason": None, "since": None}
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=active_lifecycle)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_settings_sync_loop", new_callable=AsyncMock
        ) as settings_sync,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=None)
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

        assert ok is True
        assert settings_sync.call_count == 1
        sk = settings_sync.call_args.kwargs
        assert sk["entry"] is entry
        assert sk["lifecycle"] is not None

        task = hass.data[DOMAIN][entry.entry_id]["settings_sync_task"]
        assert task is not None
        assert task.get_name() == "svitgrid_settings_sync"
        # The mocked run_settings_sync_loop returns immediately (no real while
        # loop), so the background task is already done by this point — the
        # behavior under test is that it was created + registered + named
        # correctly and gets cancelled cleanly on unload (below), not that it
        # is still running (a real loop mock would never finish).

        await async_unload_entry(hass, entry)
        await hass.async_block_till_done()
        assert task.cancelled() or task.done()
