"""Readings publisher: builds a payload from current HA entity states, omits
unavailable/non-numeric fields, flushes every 10s to /ingest/reading."""

from __future__ import annotations

from custom_components.svitgrid.readings_publisher import build_reading_payload


def test_build_payload_includes_mapped_entities(hass):
    hass.states.async_set("sensor.my_soc", "85", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.my_battery_power", "-1500", {"unit_of_measurement": "W"})

    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "batterySoc": "sensor.my_soc",
            "batteryPower": "sensor.my_battery_power",
        },
    )

    assert payload["inverterId"] == "inv-1"
    assert payload["batterySoc"] == 85.0
    assert payload["batteryPower"] == -1500.0
    assert "timestamp" in payload
    assert payload["source"] == "edge"


def test_build_payload_omits_unavailable_entities(hass):
    hass.states.async_set("sensor.my_soc", "unavailable")
    hass.states.async_set("sensor.my_battery_power", "-1500", {})

    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "batterySoc": "sensor.my_soc",
            "batteryPower": "sensor.my_battery_power",
        },
    )

    assert "batterySoc" not in payload
    assert payload["batteryPower"] == -1500.0


def test_build_payload_omits_non_numeric(hass):
    hass.states.async_set("sensor.soc", "unknown")
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"batterySoc": "sensor.soc"},
    )
    assert "batterySoc" not in payload


def test_build_payload_omits_missing_entity(hass):
    # Entity never registered
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"batterySoc": "sensor.does_not_exist"},
    )
    assert "batterySoc" not in payload


def test_build_payload_aggregates_pv_power(hass):
    hass.states.async_set("sensor.pv1", "1500", {})
    hass.states.async_set("sensor.pv2", "2000", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "pv1Power": "sensor.pv1",
            "pv2Power": "sensor.pv2",
        },
    )
    assert payload["pv1Power"] == 1500.0
    assert payload["pv2Power"] == 2000.0
    assert payload["pvPower"] == 3500.0


def test_build_payload_single_mppt_aggregates_to_pv1_total(hass):
    hass.states.async_set("sensor.pv1", "1500", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"pv1Power": "sensor.pv1"},
    )
    assert payload["pvPower"] == 1500.0


# ── Phase 2 T10a: adaptive ingest cadence ─────────────────────────────
#
# run_loop reads `ingestIntervalMs` from each /ingest/reading response
# and adjusts its next sleep. Matches the same adaptive cadence the edge
# connector (firmware 2.63.0+) and mobile harvester honor:
#   - 60s during active sessions / pending commands
#   - 300s during idle
# Phase 1 was hard-coded 10s.

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.readings_publisher import run_loop


def _mock_hass_one_iter() -> MagicMock:
    """hass mock that yields one publish iteration then signals stop."""
    hass = MagicMock()
    hass.states.get = lambda eid: MagicMock(state="80") if "battery" in eid else (
        MagicMock(state="1200") if "pv1" in eid else (
            MagicMock(state="-200") if "battery_power" in eid else (
                MagicMock(state="100") if "grid" in eid else (
                    MagicMock(state="500") if "load" in eid else None
                )
            )
        )
    )
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 1

    type(hass).is_stopping = property(_is_stopping)
    return hass


_RUN_KWARGS = dict(
    api_key="k",
    inverter_id="inv-1",
    entity_map={
        "batterySoc": "sensor.inverter_battery",
        "pv1Power": "sensor.inverter_pv1_power",
        "batteryPower": "sensor.inverter_battery_power",
        "gridPower": "sensor.inverter_grid_power",
        "loadPower": "sensor.inverter_load_power",
    },
)


async def _run_with_sleep_capture(monkeypatch, hass, api):
    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, api_client=api, **_RUN_KWARGS)
    return sleeps


@pytest.mark.asyncio
async def test_publisher_honors_active_ingest_interval(monkeypatch):
    api = MagicMock()
    api.push_reading = AsyncMock(
        return_value={"success": True, "ingestIntervalMs": 60_000}
    )
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    api.push_reading.assert_awaited_once()
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_publisher_honors_idle_ingest_interval(monkeypatch):
    api = MagicMock()
    api.push_reading = AsyncMock(
        return_value={"success": True, "ingestIntervalMs": 300_000}
    )
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [300.0]


@pytest.mark.asyncio
async def test_publisher_defaults_to_60s_when_field_missing(monkeypatch):
    """Older server / unexpected shape → safe 60s default (active-cadence)."""
    api = MagicMock()
    api.push_reading = AsyncMock(return_value={"success": True})  # no ingestIntervalMs
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_publisher_defaults_to_60s_when_push_returns_none(monkeypatch):
    """API call failed (4xx/5xx → push_reading returns None) → 60s default
    so we don't get stuck in a 10s-retry tight loop on a server outage."""
    api = MagicMock()
    api.push_reading = AsyncMock(return_value=None)
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_publisher_clamps_extreme_intervals(monkeypatch):
    """Misbehaving server with ingestIntervalMs=999_999_999 (or negative)
    gets clamped: prevents both 'silent freeze for hours' and 'tight 1-ms
    loop' as failure modes."""
    api = MagicMock()
    api.push_reading = AsyncMock(
        return_value={"success": True, "ingestIntervalMs": 999_999_999}
    )
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [1800.0]  # 30 min cap


@pytest.mark.asyncio
async def test_publisher_clamps_negative_or_tiny_intervals(monkeypatch):
    api = MagicMock()
    api.push_reading = AsyncMock(
        return_value={"success": True, "ingestIntervalMs": -5}
    )
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [10.0]  # 10s floor
