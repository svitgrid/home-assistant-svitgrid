"""End-to-end battery-sign normalization: capture flips for solarman inverters
(so the local store + panel show the right charge/discharge direction), and the
sender re-inverts before upload (so the server's existing negation still yields
correct cloud data). Regression for the local HA panel showing a charging
battery as 'discharging' (bilinskij.yaroslav, ha-2ab2f102335d, 2026-07-04)."""

from __future__ import annotations

import pytest

from custom_components.svitgrid.reading_sender import Cadence, drain_once
from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid.readings_publisher import build_reading_payload


# --- capture side -----------------------------------------------------------

def test_capture_flips_battery_for_solarman(hass):
    # HA Solarman reports a CHARGING battery as a NEGATIVE value.
    hass.states.async_set("sensor.batt_power", "-800", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="ha-x",
        entity_map={"batteryPower": "sensor.batt_power"},
        discharge_positive=True,
    )
    # Stored in Svitgrid convention: charge is POSITIVE → panel shows charging.
    assert payload["batteryPower"] == 800.0


def test_capture_does_not_flip_without_convention(hass):
    hass.states.async_set("sensor.batt_power", "-800", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="ha-x",
        entity_map={"batteryPower": "sensor.batt_power"},
        # discharge_positive defaults False (manual / non-solarman inverter)
    )
    assert payload["batteryPower"] == -800.0


# --- send side --------------------------------------------------------------

class _SyncStore(ReadingStore):
    async def skip_aged(self, now_iso, cap_s): return self._skip_aged_sync(now_iso, cap_s)
    async def get_sendable(self, now_iso, cap_s, limit): return self._get_sendable_sync(now_iso, cap_s, limit)
    async def mark_sent(self, keys): return self._mark_sent_sync(keys)
    async def mark_failed(self, keys, now_iso): return self._mark_failed_sync(keys, now_iso)
    async def set_lifecycle(self, *a): return None


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def push_readings_batch(self, api_key, readings):
        self.calls.append(readings)
        return self._response


@pytest.mark.asyncio
async def test_sender_reinverts_battery_for_solarman(tmp_path):
    store = _SyncStore(None, str(tmp_path / "readings.db"))
    now = "2026-07-04T12:00:00Z"
    # Local store holds Svitgrid convention (charge positive) after capture.
    store._append_sync({"inverterId": "ha-solar", "timestamp": "2026-07-04T10:00:00Z", "batteryPower": 800.0})
    store._append_sync({"inverterId": "ha-manual", "timestamp": "2026-07-04T10:00:01Z", "batteryPower": 800.0})
    client = _FakeClient({"results": [
        {"ok": True, "inverterId": "ha-solar"},
        {"ok": True, "inverterId": "ha-manual"},
    ]})
    cadence = Cadence(interval_s=10)

    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence,
        discharge_positive_ids={"ha-solar"},
    )

    assert sent == 2
    uploaded = {r["inverterId"]: r["batteryPower"] for r in client.calls[0]}
    # Solarman inverter re-inverted to raw discharge-positive for the server.
    assert uploaded["ha-solar"] == -800.0
    # Non-solarman inverter uploaded unchanged.
    assert uploaded["ha-manual"] == 800.0


@pytest.mark.asyncio
async def test_sender_leaves_battery_when_no_solarman_ids(tmp_path):
    store = _SyncStore(None, str(tmp_path / "readings.db"))
    now = "2026-07-04T12:00:00Z"
    store._append_sync({"inverterId": "ha-manual", "timestamp": "2026-07-04T10:00:00Z", "batteryPower": 500.0})
    client = _FakeClient({"results": [{"ok": True, "inverterId": "ha-manual"}]})
    cadence = Cadence(interval_s=10)

    await drain_once(store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence)

    assert client.calls[0][0]["batteryPower"] == 500.0
