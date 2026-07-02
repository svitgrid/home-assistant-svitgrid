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

from custom_components.svitgrid.reading_sender import Cadence
from custom_components.svitgrid.readings_publisher import run_loop


class _RecordingStore:
    """Minimal store double — records every reading the publisher captures."""

    def __init__(self):
        self.appended = []

    async def append(self, reading):
        self.appended.append(reading)


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


async def _run_with_sleep_capture(monkeypatch, hass, store, cadence):
    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)
    return sleeps


@pytest.mark.asyncio
async def test_publisher_clamps_extreme_intervals(monkeypatch):
    """A misbehaving cadence value (999_999_999) must NEVER produce a sleep
    larger than the 30-min ceiling. interval >= 120 → idle branch, which
    samples every 60s and does NOT add a final sleep — so the invariant we
    prove is: every captured sleep is <= 1800.0 (no hour-long freeze)."""
    hass = _mock_hass_one_iter()
    # Let the idle sampling sub-loop run a few ticks before stopping so we can
    # observe the sub-sleeps. (The default mock stops after one read, which
    # would skip the sub-loop entirely.)
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 4  # top read + 3 sampling sub-loop reads

    type(hass).is_stopping = property(_is_stopping)

    cadence = Cadence(interval_s=999_999_999)
    sleeps = await _run_with_sleep_capture(
        monkeypatch, hass, _RecordingStore(), cadence
    )
    assert sleeps, "expected the idle-branch sampling sub-loop to sleep"
    # Idle path: sampling sub-sleeps are 60s; nothing freezes us at the
    # un-clamped 999_999_999s. Ceiling clamp keeps every sleep <= 1800.
    assert all(s <= 1800.0 for s in sleeps)
    assert sleeps[0] == 60.0


@pytest.mark.asyncio
async def test_publisher_clamps_negative_or_tiny_intervals(monkeypatch):
    """A negative cadence (-5) is < 120 → active branch; the post-append sleep
    must be clamped UP to the 5s floor (never a tight sub-second loop)."""
    cadence = Cadence(interval_s=-5)
    sleeps = await _run_with_sleep_capture(
        monkeypatch, _mock_hass_one_iter(), _RecordingStore(), cadence
    )
    assert sleeps == [5.0]  # 5s floor


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
    # Identity fields must be present so store._append_sync can key on them
    # without a KeyError being silently swallowed by the publisher loop.
    assert agg["inverterId"] == "inv-1"
    assert agg["timestamp"] == "t1"
    assert agg["source"] == "edge"


@pytest.mark.asyncio
async def test_publisher_aggregates_when_interval_idle(monkeypatch):
    """cadence.interval_s=300 (>=120) → the FIRST iteration is the idle
    aggregation path directly: collect 5 samples 60s apart and capture ONE
    aggregated reading with sampleCount=5, periodSec=300."""
    hass = _mock_hass_one_iter()
    # is_stopping is read once at the loop top, then once per sampling tick.
    # Allow one full aggregation iteration: top read (#1) + 5 sub-loop reads
    # (#2-#6); stop on the read that would start iteration 2 (#7).
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 6

    type(hass).is_stopping = property(_is_stopping)

    store = _RecordingStore()
    cadence = Cadence(interval_s=300)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)

    # ONE aggregated reading captured.
    assert len(store.appended) == 1
    aggregated = store.appended[0]
    assert aggregated["sampleCount"] == 5
    assert aggregated["periodSec"] == 300
    # Idle path: 5 sampling sub-sleeps of 60s each, no final post-capture sleep.
    assert sleeps == [60.0, 60.0, 60.0, 60.0, 60.0]


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
    store = _RecordingStore()
    cadence = Cadence(interval_s=60)
    activity = MagicMock()
    kwargs = dict(_RUN_KWARGS, activity=activity)

    sleeps = []
    async def _record_sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    await run_loop(
        hass=_mock_hass_incomplete_one_iter(), store=store, cadence=cadence, **kwargs
    )

    assert store.appended == []                   # never captured junk
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    assert sleeps == [60.0]                       # still slept the cadence interval


@pytest.mark.asyncio
async def test_publisher_posts_when_no_solar_but_core_present(monkeypatch):
    """Battery-only system (no PV entity) must still capture — pvPower defaults
    to 0."""
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

    store = _RecordingStore()
    cadence = Cadence(interval_s=60)
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
    await run_loop(hass=hass, store=store, cadence=cadence, **kwargs)

    assert len(store.appended) == 1
    sent = store.appended[0]
    assert sent["pvPower"] == 0.0


@pytest.mark.asyncio
async def test_publisher_skips_aggregated_post_when_core_field_missing(monkeypatch):
    """Idle path (cadence>=120): the aggregated samples are incomplete
    (batterySoc is unavailable for every sample) → the aggregated reading is
    skipped, not captured as junk."""
    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "battery_voltage" in eid:
            return MagicMock(state="52")
        if "battery" in eid:  # batterySoc → always unavailable
            return MagicMock(state="unavailable")
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
        # iter1 top read (#1) + iter1's 5 sampling sub-loop reads (#2-#6);
        # stop on the read that would start iter2 (#7).
        return call_count["n"] > 6

    type(hass).is_stopping = property(_is_stopping)

    store = _RecordingStore()
    cadence = Cadence(interval_s=300)
    activity = MagicMock()
    kwargs = dict(_RUN_KWARGS, activity=activity)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **kwargs)

    # Nothing captured; the aggregated reading was skipped.
    assert store.appended == []
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    # 5×60s sampling ticks; idle skip adds no extra sleep.
    assert sleeps == [60.0, 60.0, 60.0, 60.0, 60.0]


# ── Capture-then-drain: publisher appends to the store (Task 5) ────────────


@pytest.mark.asyncio
async def test_run_loop_appends_to_store(monkeypatch):
    """With all core sensors present and cadence=60s (active branch), one
    iteration captures exactly one reading into the store."""
    store = _RecordingStore()
    cadence = Cadence(interval_s=60)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await run_loop(
        hass=_mock_hass_one_iter(), store=store, cadence=cadence, **_RUN_KWARGS
    )

    assert len(store.appended) == 1
    reading = store.appended[0]
    assert reading["inverterId"] == "inv-1"
    assert reading["batterySoc"] == 80.0


@pytest.mark.asyncio
async def test_run_loop_does_not_capture_when_deprovisioned(monkeypatch):
    from custom_components.svitgrid.lifecycle import LifecycleState
    hass = _mock_hass_one_iter()
    store = _RecordingStore()
    lc = LifecycleState()
    lc.deprovision("revoked", "2026-06-25T10:00:00Z")
    async def _noop_sleep(_): pass
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await run_loop(hass=hass, store=store, cadence=Cadence(interval_s=60), lifecycle=lc, **_RUN_KWARGS)
    assert store.appended == []  # loop exited immediately, nothing captured
