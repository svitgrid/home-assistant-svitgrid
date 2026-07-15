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
        "inverterId": "inv-1",
        "timestamp": "t",
        "source": "edge",
        "batterySoc": 80.0,
        "batteryPower": -200.0,
        "batteryVoltage": 52.0,
        "gridPower": 100.0,
        "loadPower": 500.0,
    }
    finalized, missing = gate_payload(payload)
    assert finalized["pvPower"] == 0.0  # no-solar system is allowed through
    assert missing == []


def test_gate_payload_keeps_existing_pv_power():
    payload = {
        "pvPower": 1200.0,
        "batterySoc": 80.0,
        "batteryPower": -200.0,
        "batteryVoltage": 52.0,
        "gridPower": 100.0,
        "loadPower": 500.0,
    }
    finalized, missing = gate_payload(payload)
    assert finalized["pvPower"] == 1200.0
    assert missing == []


def test_gate_payload_reports_missing_core_fields_sorted():
    payload = {"inverterId": "inv-1", "timestamp": "t", "source": "edge"}
    finalized, missing = gate_payload(payload)
    # pvPower defaulted, but the five core fields are absent.
    assert finalized["pvPower"] == 0.0
    assert missing == ["batteryPower", "batterySoc", "batteryVoltage", "gridPower", "loadPower"]


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
    hass.states.get = lambda eid: (
        MagicMock(state="80")
        if "battery" in eid
        else (
            MagicMock(state="1200")
            if "pv1" in eid
            else (
                MagicMock(state="-200")
                if "battery_power" in eid
                else (
                    MagicMock(state="100")
                    if "grid" in eid
                    else (MagicMock(state="500") if "load" in eid else None)
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
    larger than the 30-min ceiling. The first pass is an immediate snapshot
    followed by a short transitional sleep; later iterations take the idle
    branch and sample every 60s — so the invariant we prove is: every captured
    sleep is <= 1800.0 (no hour-long freeze)."""
    hass = _mock_hass_one_iter()
    # Let the first snapshot land, then the idle sampling sub-loop run a couple
    # ticks before stopping so we can observe both sleep kinds.
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 4  # iter1 snapshot + iter2 top + 2 sub reads

    type(hass).is_stopping = property(_is_stopping)

    cadence = Cadence(interval_s=999_999_999)
    sleeps = await _run_with_sleep_capture(monkeypatch, hass, _RecordingStore(), cadence)
    assert sleeps, "expected the first-pass transitional sleep + idle sub-loop sleeps"
    # Ceiling clamp keeps every sleep <= 1800; nothing freezes us at 999_999_999.
    assert all(s <= 1800.0 for s in sleeps)
    # First reading → short transitional sleep, not the un-clamped cadence.
    assert sleeps[0] == 5.0
    # Later iterations aggregate → 60s sampling sub-sleeps.
    assert 60.0 in sleeps


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
        {
            "inverterId": "inv-1",
            "timestamp": "t1",
            "source": "edge",
            "batteryPower": -200.0,
            "loadPower": 500.0,
        },
        {
            "inverterId": "inv-1",
            "timestamp": "t2",
            "source": "edge",
            "batteryPower": -100.0,
            "loadPower": 600.0,
        },
        {
            "inverterId": "inv-1",
            "timestamp": "t3",
            "source": "edge",
            "batteryPower": -300.0,
            "loadPower": 550.0,
        },
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
        {"inverterId": "inv-1", "timestamp": "t1", "source": "edge", "batteryPower": -200.0},
        {"inverterId": "inv-1", "timestamp": "t2", "source": "edge", "loadPower": 500.0},
    ]
    agg = _aggregate_samples(samples, period_s=60)
    # Each field averaged across samples that have it (not zero-filled).
    assert agg["batteryPower"] == pytest.approx(-200.0)
    assert agg["loadPower"] == pytest.approx(500.0)


def test_aggregate_single_sample_returns_it_unchanged_plus_metadata():
    samples = [{"inverterId": "inv-1", "timestamp": "t1", "source": "edge", "batteryPower": -200.0}]
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
    """cadence.interval_s=300 (>=120): iteration 1 is the immediate snapshot
    (ASAP first reading), and iteration 2 is the idle aggregation path —
    collect 5 samples 60s apart and capture ONE aggregated reading with
    sampleCount=5, periodSec=300."""
    hass = _mock_hass_one_iter()
    # is_stopping reads: iter1 snapshot top (#1); iter2 top (#2) + 5 sampling
    # sub-loop reads (#3-#7); stop on the read that would start iter3 (#8).
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 7

    type(hass).is_stopping = property(_is_stopping)

    store = _RecordingStore()
    cadence = Cadence(interval_s=300)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)

    # iter1: immediate snapshot (no aggregation metadata). iter2: aggregate.
    assert len(store.appended) == 2
    snapshot = store.appended[0]
    assert "sampleCount" not in snapshot
    aggregated = store.appended[1]
    assert aggregated["sampleCount"] == 5
    assert aggregated["periodSec"] == 300
    # iter1 snapshot transitional sleep (5s), then iter2's 5×60s sampling sub-sleeps.
    assert sleeps == [5.0, 60.0, 60.0, 60.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_publisher_emits_first_reading_immediately_at_idle_cadence(monkeypatch):
    """Cold-start ASAP: even at an idle cadence (>=120), the FIRST iteration
    must capture a single instantaneous snapshot and store it right away —
    like the edge connector's boot reading — so the "waiting for data" screen
    clears in seconds instead of after a full aggregation window (~5 min).
    Aggregation only kicks in on later iterations."""
    hass = _mock_hass_one_iter()  # one iteration then stop
    store = _RecordingStore()
    cadence = Cadence(interval_s=300)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)

    # First reading captured as a single snapshot — no aggregation metadata.
    assert len(store.appended) == 1
    first = store.appended[0]
    assert "sampleCount" not in first
    assert "periodSec" not in first
    # No 60s sampling sub-loop before the first reading; one short transitional
    # sleep (so the sender's real cadence is adopted on the next iteration).
    assert sleeps == [5.0]


@pytest.mark.asyncio
async def test_publisher_retries_fast_for_first_reading_until_sensors_ready(monkeypatch):
    """After an HA restart the source sensors are often not populated for the
    first few seconds. While still waiting for the FIRST reading, the publisher
    must retry on a short interval (not the full idle cadence) so data lands as
    soon as the sensors come online — otherwise the first reading is parked for
    a whole ~5-min cadence. Once the first reading is stored, normal cadence
    resumes."""
    soc_calls = {"n": 0}

    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "battery_voltage" in eid:
            return MagicMock(state="52")
        if "battery" in eid:  # batterySoc: unavailable on the 1st build, then ready
            soc_calls["n"] += 1
            return MagicMock(state="unavailable" if soc_calls["n"] == 1 else "80")
        if "grid" in eid:
            return MagicMock(state="100")
        if "load" in eid:
            return MagicMock(state="500")
        return None

    hass = MagicMock()
    hass.states.get = _get
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        # iter1 (missing → fast retry) #1; iter2 (ready → store) #2; stop #3.
        return call_count["n"] > 2

    type(hass).is_stopping = property(_is_stopping)

    store = _RecordingStore()
    cadence = Cadence(interval_s=300)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)

    # The first reading was stored once sensors were ready — a single snapshot.
    assert len(store.appended) == 1
    assert "sampleCount" not in store.appended[0]
    # Fast retry (5s floor) while sensors were missing, THEN the fast
    # transitional sleep after the first reading landed — not two 300s parks.
    assert sleeps == [5.0, 5.0]


@pytest.mark.asyncio
async def test_publisher_adopts_server_cadence_right_after_first_reading(monkeypatch):
    """The sender confirms the real server cadence asynchronously (it drains
    eagerly the moment a reading lands, ~1s later). The publisher must not park
    on the stale cold-start default (300s) after its first reading: it sleeps a
    short transitional interval, then re-reads the freshly-confirmed cadence at
    the top of the loop and honors it. Here the server cadence (30s) is
    confirmed during the first post-reading sleep."""
    hass = _mock_hass_one_iter()
    call_count = {"n": 0}

    def _is_stopping(_self):
        call_count["n"] += 1
        return call_count["n"] > 2  # iter1 (first reading) + iter2 (steady), stop iter3

    type(hass).is_stopping = property(_is_stopping)

    store = _RecordingStore()
    cadence = Cadence(interval_s=300)

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)
        # Sender confirms the real 30s cadence during the first post-reading sleep.
        if len(sleeps) == 1:
            cadence.interval_s = 30

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await run_loop(hass=hass, store=store, cadence=cadence, **_RUN_KWARGS)

    # Two single snapshots — no 300s aggregation window sneaked in.
    assert len(store.appended) == 2
    assert all("sampleCount" not in r for r in store.appended)
    # First reading → short transitional sleep; then the adopted 30s cadence.
    assert sleeps == [5.0, 30.0]


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
        if "battery" in eid:  # batterySoc entity → unavailable
            return MagicMock(state="unavailable")
        return None  # pv, batteryVoltage unmapped/missing

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

    await run_loop(hass=_mock_hass_incomplete_one_iter(), store=store, cadence=cadence, **kwargs)

    assert store.appended == []  # never captured junk
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    # Cold start (still no first reading) → fast retry, not the cadence interval,
    # so the first reading lands the moment the sensors come online.
    assert sleeps == [5.0]


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
            return MagicMock(state="80")  # batterySoc
        if "grid" in eid:
            return MagicMock(state="100")
        if "load" in eid:
            return MagicMock(state="500")
        return None  # no pv entity

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
    """Idle path (cadence>=120): once past the immediate first snapshot, an
    aggregation window whose samples are incomplete (batterySoc goes
    unavailable) is skipped, not captured as junk. batterySoc is available for
    the first-pass snapshot only, then drops out for the aggregation window."""
    soc_calls = {"n": 0}

    def _get(eid):
        if "battery_power" in eid:
            return MagicMock(state="-200")
        if "battery_voltage" in eid:
            return MagicMock(state="52")
        if "battery" in eid:  # batterySoc → available only for the 1st snapshot
            soc_calls["n"] += 1
            return MagicMock(state="80" if soc_calls["n"] == 1 else "unavailable")
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
        # iter1 snapshot top (#1); iter2 top (#2) + 5 sampling sub-loop reads
        # (#3-#7); stop on the read that would start iter3 (#8).
        return call_count["n"] > 7

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

    # Only the valid first-pass snapshot is stored; the incomplete aggregated
    # window was skipped (no junk reading, no sampleCount payload).
    assert len(store.appended) == 1
    assert "sampleCount" not in store.appended[0]
    activity.record_ingest_skipped.assert_called_once()
    _, ckwargs = activity.record_ingest_skipped.call_args
    assert "batterySoc" in ckwargs["missing_fields"]
    # iter1 snapshot transitional sleep (5s), then iter2's 5×60s sampling ticks.
    assert sleeps == [5.0, 60.0, 60.0, 60.0, 60.0, 60.0]


# ── Capture-then-drain: publisher appends to the store (Task 5) ────────────


@pytest.mark.asyncio
async def test_run_loop_appends_to_store(monkeypatch):
    """With all core sensors present and cadence=60s (active branch), one
    iteration captures exactly one reading into the store."""
    store = _RecordingStore()
    cadence = Cadence(interval_s=60)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await run_loop(hass=_mock_hass_one_iter(), store=store, cadence=cadence, **_RUN_KWARGS)

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

    async def _noop_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    await run_loop(
        hass=hass, store=store, cadence=Cadence(interval_s=60), lifecycle=lc, **_RUN_KWARGS
    )
    assert store.appended == []  # loop exited immediately, nothing captured
