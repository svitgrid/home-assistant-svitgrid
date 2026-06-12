"""Readings publisher: builds a payload from current HA entity states, omits
unavailable/non-numeric fields, flushes every 10s to /ingest/reading."""

from __future__ import annotations

from custom_components.svitgrid.readings_publisher import build_reading_payload, gate_payload


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
    # Per-string fields are emitted under the server's canonical names
    # (pvPower1..pvPower4) — matching the mobile harvester (upload_payload.dart)
    # and edge firmware (cloud_uploader.c). The entity_map keys stay
    # pv1Power..pv4Power (the UI labels in MAPPABLE_FIELDS), but the outbound
    # payload uses pvPowerN so the API ingest schema doesn't strip them.
    assert payload["pvPower1"] == 1500.0
    assert payload["pvPower2"] == 2000.0
    assert payload["pvPower"] == 3500.0
    # The non-canonical aliases must NOT leak into the payload.
    assert "pv1Power" not in payload
    assert "pv2Power" not in payload


def test_build_payload_single_mppt_aggregates_to_pv1_total(hass):
    hass.states.async_set("sensor.pv1", "1500", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"pv1Power": "sensor.pv1"},
    )
    assert payload["pvPower"] == 1500.0
    assert payload["pvPower1"] == 1500.0
    assert "pv1Power" not in payload


def test_gate_payload_defaults_pv_power_when_absent():
    payload = {
        "inverterId": "inv-1", "timestamp": "t", "source": "edge",
        "batterySoc": 80.0, "batteryPower": -200.0, "batteryVoltage": 52.0,
        "gridPower": 100.0, "loadPower": 500.0,
    }
    finalized, missing = gate_payload(payload)
    assert finalized["pvPower"] == 0.0   # no-solar system is allowed through
    assert missing == []


def test_gate_payload_keeps_existing_pv_power():
    payload = {"pvPower": 1200.0, "batterySoc": 80.0, "batteryPower": -200.0,
               "batteryVoltage": 52.0, "gridPower": 100.0, "loadPower": 500.0}
    finalized, missing = gate_payload(payload)
    assert finalized["pvPower"] == 1200.0
    assert missing == []


def test_gate_payload_reports_missing_core_fields_sorted():
    payload = {"inverterId": "inv-1", "timestamp": "t", "source": "edge"}
    finalized, missing = gate_payload(payload)
    # pvPower defaulted, but the five core fields are absent.
    assert finalized["pvPower"] == 0.0
    assert missing == ["batteryPower", "batterySoc", "batteryVoltage",
                       "gridPower", "loadPower"]


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

from custom_components.svitgrid.api_client import DeviceStopped
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
        "batteryVoltage": "sensor.inverter_battery_voltage",
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
    """Transient failure (5xx / network → push_reading returns None) → 60s
    default so we don't get stuck in a 10s-retry tight loop on a server
    outage. (A 4xx raises ReadingRejected instead — see the backoff test.)"""
    api = MagicMock()
    api.push_reading = AsyncMock(return_value=None)
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [60.0]


@pytest.mark.asyncio
async def test_publisher_backs_off_to_ceiling_on_reading_rejected(monkeypatch):
    """A 4xx rejection (ReadingRejected) means the payload itself is wrong —
    re-POSTing every 60s just burns requests the server keeps rejecting. The
    loop must park at the ceiling interval (30 min) and keep running, not exit
    or hammer at the default cadence."""
    from custom_components.svitgrid.api_client import ReadingRejected

    api = MagicMock()
    api.push_reading = AsyncMock(side_effect=ReadingRejected(400, "Validation error"))
    sleeps = await _run_with_sleep_capture(monkeypatch, _mock_hass_one_iter(), api)
    assert sleeps == [1800.0]  # _INTERVAL_CEILING_S


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


# ── Phase 2 T10b: sample aggregation for idle (>=120s intervals) ──────
#
# When the server's ingestIntervalMs >= 120s, instead of sleeping the
# whole interval and pushing one instantaneous snapshot, the publisher
# collects samples every 60s during the period and sends ONE averaged
# payload with sampleCount + periodSec. Mirrors the edge connector's
# aggregator behavior. Gives the server better data for charts +
# forecasting without changing the Firestore write count.

from custom_components.svitgrid.readings_publisher import _aggregate_samples


def test_aggregate_averages_numeric_fields():
    samples = [
        {"inverterId": "inv-1", "timestamp": "t1", "source": "edge",
         "batteryPower": -200.0, "loadPower": 500.0},
        {"inverterId": "inv-1", "timestamp": "t2", "source": "edge",
         "batteryPower": -100.0, "loadPower": 600.0},
        {"inverterId": "inv-1", "timestamp": "t3", "source": "edge",
         "batteryPower": -300.0, "loadPower": 550.0},
    ]
    agg = _aggregate_samples(samples, period_s=180)
    # Numeric fields averaged
    assert agg["batteryPower"] == pytest.approx(-200.0)
    assert agg["loadPower"] == pytest.approx(550.0)
    # Identity fields from last sample
    assert agg["inverterId"] == "inv-1"
    assert agg["timestamp"] == "t3"
    assert agg["source"] == "edge"
    # Aggregation metadata
    assert agg["sampleCount"] == 3
    assert agg["periodSec"] == 180


def test_aggregate_drops_fields_missing_from_all_samples():
    samples = [
        {"inverterId": "inv-1", "timestamp": "t1", "source": "edge",
         "batteryPower": -200.0},
        {"inverterId": "inv-1", "timestamp": "t2", "source": "edge",
         "loadPower": 500.0},
    ]
    agg = _aggregate_samples(samples, period_s=60)
    # Each field averaged across samples that have it (not zero-filled).
    assert agg["batteryPower"] == pytest.approx(-200.0)
    assert agg["loadPower"] == pytest.approx(500.0)


def test_aggregate_single_sample_returns_it_unchanged_plus_metadata():
    samples = [{"inverterId": "inv-1", "timestamp": "t1", "source": "edge",
                "batteryPower": -200.0}]
    agg = _aggregate_samples(samples, period_s=60)
    assert agg["batteryPower"] == -200.0
    assert agg["sampleCount"] == 1
    assert agg["periodSec"] == 60


@pytest.mark.asyncio
async def test_publisher_aggregates_when_interval_idle(monkeypatch):
    """ingestIntervalMs=300_000 → next iteration collects 5 samples 60s
    apart and pushes ONE aggregated payload with sampleCount=5,
    periodSec=300."""
    hass = _mock_hass_one_iter()
    # Need two iterations: first to receive the idle hint, second to
    # actually aggregate. Override is_stopping to allow two full passes.
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        # After 2 full pushes (each may have many sleep ticks), stop.
        # Count is incremented on every is_stopping read — including
        # within the aggregation sub-loop. Allow ~8 reads to cover
        # iter1 (1 read) + iter2's 5 sub-sleeps + iter2's final push.
        return call_count["n"] > 8

    type(hass).is_stopping = property(_is_stopping)

    api = MagicMock()
    api.push_reading = AsyncMock(side_effect=[
        # 1st push: snapshot, response signals idle
        {"success": True, "ingestIntervalMs": 300_000},
        # 2nd push: aggregated, response again idle (loop stops before 3rd)
        {"success": True, "ingestIntervalMs": 300_000},
    ])

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, api_client=api, **_RUN_KWARGS)

    # 2 pushes total: 1 snapshot + 1 aggregated
    assert api.push_reading.await_count == 2
    second_call = api.push_reading.await_args_list[1]
    payload = second_call.kwargs["reading"]
    assert payload["sampleCount"] == 5
    assert payload["periodSec"] == 300
    # Sleep cadence: first iter sleeps 300s after pushing.
    # Second iter (aggregation) sleeps 60s x 5 sub-sleeps during sampling.
    assert sleeps[0] == 300.0
    assert sleeps[1:6] == [60.0, 60.0, 60.0, 60.0, 60.0]


# ── Graceful stop signal in readings publisher ────────────────────────────


@pytest.mark.asyncio
async def test_publisher_stops_on_device_stopped(monkeypatch):
    """When push_reading raises DeviceStopped, the loop exits without any
    further push calls."""
    hass = MagicMock()
    hass.states.get = lambda eid: MagicMock(state="80")
    type(hass).is_stopping = property(lambda _self: False)  # would loop forever without the stop

    api = MagicMock()
    api.push_reading = AsyncMock(side_effect=DeviceStopped("zombie poll cost"))

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, api_client=api, **_RUN_KWARGS)

    # Called exactly once — raised on first push and loop exited.
    api.push_reading.assert_awaited_once()
    # No sleeps: loop returned before reaching the post-push sleep.
    assert sleeps == []


# ── Task 4: gate_payload wired into run_loop — skip ingest on missing core ──


def _mock_hass_incomplete_one_iter() -> MagicMock:
    """hass mock where the battery SOC entity is unavailable → core field
    missing → reading must be skipped (not POSTed)."""
    hass = MagicMock()

    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "grid" in eid:
            return MagicMock(state="100")
        if "load" in eid:
            return MagicMock(state="500")
        if "battery" in eid:           # batterySoc entity → unavailable
            return MagicMock(state="unavailable")
        return None                     # pv, batteryVoltage unmapped/missing

    hass.states.get = _get
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 1

    type(hass).is_stopping = property(_is_stopping)
    return hass


@pytest.mark.asyncio
async def test_publisher_skips_post_when_core_field_missing(monkeypatch):
    api = MagicMock()
    api.push_reading = AsyncMock(return_value={"success": True})
    activity = MagicMock()
    kwargs = dict(_RUN_KWARGS, activity=activity)

    sleeps = []
    async def _record_sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    await run_loop(hass=_mock_hass_incomplete_one_iter(), api_client=api, **kwargs)

    api.push_reading.assert_not_awaited()        # never POSTed junk
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    assert sleeps == [60.0]                       # still slept the default cadence


@pytest.mark.asyncio
async def test_publisher_posts_when_no_solar_but_core_present(monkeypatch):
    """Battery-only system (no PV entity) must still POST — pvPower defaults to 0."""
    hass = MagicMock()

    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "battery_voltage" in eid:
            return MagicMock(state="52")
        if "battery" in eid:
            return MagicMock(state="80")          # batterySoc
        if "grid" in eid:
            return MagicMock(state="100")
        if "load" in eid:
            return MagicMock(state="500")
        return None                               # no pv entity

    hass.states.get = _get
    cc = {"n": 0}
    def _is_stopping(_self):
        cc["n"] += 1
        return cc["n"] > 1
    type(hass).is_stopping = property(_is_stopping)

    api = MagicMock()
    api.push_reading = AsyncMock(return_value={"success": True})
    kwargs = dict(
        _RUN_KWARGS,
        entity_map={
            "batterySoc": "sensor.inverter_battery",
            "batteryPower": "sensor.inverter_battery_power",
            "batteryVoltage": "sensor.inverter_battery_voltage",
            "gridPower": "sensor.inverter_grid_power",
            "loadPower": "sensor.inverter_load_power",
        },
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await run_loop(hass=hass, api_client=api, **kwargs)

    api.push_reading.assert_awaited_once()
    sent = api.push_reading.await_args.kwargs["reading"]
    assert sent["pvPower"] == 0.0


@pytest.mark.asyncio
async def test_publisher_skips_aggregated_post_when_core_field_missing(monkeypatch):
    """Idle path: after a complete snapshot bumps cadence to idle, the next
    iteration's aggregated samples are incomplete (batterySoc goes
    unavailable) → the aggregated POST is skipped, not sent as junk."""
    # batterySoc is healthy until the first push lands, then goes unavailable
    # so every idle-path sample omits it → aggregated reading is incomplete.
    state = {"complete": True}

    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "battery_voltage" in eid:
            return MagicMock(state="52")
        if "battery" in eid:  # batterySoc
            return MagicMock(state="80" if state["complete"] else "unavailable")
        if "grid" in eid:
            return MagicMock(state="100")
        if "load" in eid:
            return MagicMock(state="500")
        return None  # no pv entity → pvPower defaults to 0

    hass = MagicMock()
    hass.states.get = _get
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        # iter1 top read (#1) + iter2 top read (#2) + iter2's 5 sampling
        # sub-loop reads (#3-#7); stop on the read that would start iter3.
        return call_count["n"] > 7

    type(hass).is_stopping = property(_is_stopping)

    def _push(**_kw):
        # First (snapshot) push succeeds and signals idle cadence; thereafter
        # the soc entity drops out so the aggregated reading is incomplete.
        state["complete"] = False
        return {"success": True, "ingestIntervalMs": 300_000}

    api = MagicMock()
    api.push_reading = AsyncMock(side_effect=_push)
    activity = MagicMock()
    kwargs = dict(_RUN_KWARGS, activity=activity)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, api_client=api, **kwargs)

    # Only the first (snapshot) push happened; the aggregated one was skipped.
    api.push_reading.assert_awaited_once()
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    # 300s post-snapshot sleep + 5×60s sampling ticks; idle skip adds no sleep.
    assert sleeps == [300.0, 60.0, 60.0, 60.0, 60.0, 60.0]
