"""Tests for harvest/engine.py — poll_once + run_direct_harvest_loop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest import engine as eng
from custom_components.svitgrid.harvest.register_spec import RegisterSpec

SPEC = RegisterSpec.from_dict(
    {
        "modelId": "deye_sg04lp3",
        "version": 1,
        "protocol": "solarman_v5",
        "port": 8899,
        "defaultSlaveId": 1,
        "flags": {"batteryPositiveIsDischarge": True},
        "reads": [
            {"field": "batterySoc", "address": 588},
            {"field": "batteryPower", "address": 590, "signed": True},
            {"field": "batteryVoltage", "address": 587, "scale": 0.01},
            {"field": "gridPower", "address": 625, "signed": True},
            {"field": "loadPower", "address": 653},
            {"field": "pv1Power", "address": 672},
            {"field": "pv2Power", "address": 673},
        ],
        "derivations": [
            {
                "field": "batteryPower",
                "op": "builtin",
                "builtin": "battery_sign_normalize",
                "inputs": ["batteryPower"],
            },
            {"field": "totalPvPower", "op": "sum", "inputs": ["pv1Power", "pv2Power"]},
        ],
        "writes": [],
    }
)


@pytest.mark.asyncio
async def test_poll_once_appends_payload(hass, monkeypatch):
    raw = {1: {588: 78, 590: 1500, 587: 5230, 625: 64536, 653: 1800, 672: 1500, 673: 800}}
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value=raw))
    store = type("S", (), {"append": AsyncMock()})()
    returned = await eng.poll_once(
        hass=hass,
        spec=SPEC,
        cfg={"ip": "x", "logger_serial": "1"},
        inverter_id="inv-1",
        store=store,
    )
    assert returned is not None
    store.append.assert_awaited_once()
    # The returned value IS the payload dict that was appended.
    assert returned["batterySoc"] == 78.0
    assert returned["batteryPower"] == -1500.0  # sign-normalized
    assert returned["gridPower"] == -1000.0
    assert returned["pvPower"] == 2300.0
    assert returned["pvPower1"] == 1500.0 and returned["pvPower2"] == 800.0


@pytest.mark.asyncio
async def test_poll_once_gated_when_required_missing(hass, monkeypatch):
    # only batterySoc present → CORE_PAYLOAD_FIELDS missing → gated, not appended
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value={1: {588: 50}}))
    store = type("S", (), {"append": AsyncMock()})()
    result = await eng.poll_once(
        hass=hass,
        spec=SPEC,
        cfg={"ip": "x", "logger_serial": "1"},
        inverter_id="inv-1",
        store=store,
    )
    assert result is None
    store.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_direct_harvest_loop_single_iteration(monkeypatch):
    """Loop runs exactly one iteration then exits when hass.is_stopping flips True."""
    poll_calls: list[dict] = []

    async def fake_poll_once(**kwargs):
        poll_calls.append(kwargs)
        # Flip is_stopping so the while-condition fails after this iteration.
        fake_hass.is_stopping = True
        return True

    monkeypatch.setattr(eng, "poll_once", fake_poll_once)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()
    spec_holder = type("SH", (), {"spec": SPEC})()
    cadence = type("C", (), {"interval_s": 60})()

    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=cadence,
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1"},
        spec_holder=spec_holder,
    )

    assert len(poll_calls) == 1
    assert poll_calls[0]["inverter_id"] == "inv-1"
    assert poll_calls[0]["spec"] is SPEC


@pytest.mark.asyncio
async def test_run_direct_harvest_loop_exception_is_fail_soft(monkeypatch):
    """An exception from poll_once must be caught; loop continues and exits cleanly."""
    call_count = 0

    async def exploding_poll_once(**kwargs):
        nonlocal call_count
        call_count += 1
        # Flip is_stopping so the loop exits after this single iteration.
        fake_hass.is_stopping = True
        raise RuntimeError("simulated inverter read failure")

    monkeypatch.setattr(eng, "poll_once", exploding_poll_once)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()
    spec_holder = type("SH", (), {"spec": SPEC})()
    cadence = type("C", (), {"interval_s": 60})()

    # Must complete without raising — the exception is caught inside the loop.
    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=cadence,
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1"},
        spec_holder=spec_holder,
    )

    assert call_count == 1


# ---------------------------------------------------------------------------
# A spec that never arrives (404 / 500 / parse refusal) must stop being silent.
# Today `spec is None` idles the loop behind logger.debug for ever: the user
# sees an inverter that set up fine and reports nothing, with no way to tell
# that apart from an unreachable logger.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_spec_escalates_after_repeated_ticks(monkeypatch):
    from custom_components.svitgrid.activity import ActivityTracker
    from custom_components.svitgrid.harvest.spec_health import SPEC_UNAVAILABLE_TICKS

    ticks = 0

    async def fake_sleep(_s):
        nonlocal ticks
        ticks += 1
        if ticks >= SPEC_UNAVAILABLE_TICKS:
            fake_hass.is_stopping = True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()
    activity = ActivityTracker()

    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=type("C", (), {"interval_s": 60})(),
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1", "model_id": "srne_asf_10k"},
        spec_holder=type("SH", (), {"spec": None})(),
        activity=activity,
    )

    assert activity.spec_problem is not None
    assert "srne_asf_10k" in activity.spec_problem


@pytest.mark.asyncio
async def test_a_spec_arriving_late_clears_the_problem(monkeypatch):
    """The cache can fill in on a later tick — the warning must not stick."""
    from custom_components.svitgrid.activity import ActivityTracker
    from custom_components.svitgrid.harvest.spec_health import SPEC_UNAVAILABLE_TICKS

    holder = type("SH", (), {"spec": None})()
    ticks = 0

    async def fake_sleep(_s):
        nonlocal ticks
        ticks += 1
        if ticks == SPEC_UNAVAILABLE_TICKS:
            holder.spec = SPEC  # cache filled in
        if ticks >= SPEC_UNAVAILABLE_TICKS + 1:
            fake_hass.is_stopping = True

    async def fake_poll_once(**kwargs):
        return {"inverterId": "inv-1"}

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(eng, "poll_once", fake_poll_once)

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()
    activity = ActivityTracker()

    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=type("C", (), {"interval_s": 60})(),
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1", "model_id": "srne_asf_10k"},
        spec_holder=holder,
        activity=activity,
    )

    assert activity.spec_problem is None


# ---------------------------------------------------------------------------
# #7: before the first reading is stored, a gated or failed tick retries after
# a short back-off instead of sleeping out the full cadence (300 s default),
# which outlasted the app's 90 s first-reading wait.
# ---------------------------------------------------------------------------


def _run_loop_with(monkeypatch, outcomes, *, interval_s=300, read_now=None, clock=None):
    """Drive the loop with scripted poll outcomes; stop after the last one.

    Each outcome is "stored", "gated" or "error". Returns the recorded
    asyncio.sleep delays and the number of polls.
    """
    delays: list[float] = []
    polls = 0
    script = list(outcomes)

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()

    async def fake_poll_once(**kwargs):
        nonlocal polls
        polls += 1
        outcome = script[polls - 1]
        if polls == len(script):
            fake_hass.is_stopping = True
        if outcome == "error":
            raise RuntimeError("solarman: all ranges failed — logger unreachable")
        if outcome == "gated":
            kwargs.get("missing_out", []).append("loadPower")
            return None
        return {"inverterId": "inv-1"}

    async def fake_sleep(s):
        delays.append(s)

    monkeypatch.setattr(eng, "poll_once", fake_poll_once)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    kwargs = {}
    if read_now is not None:
        kwargs["read_now"] = read_now
    if clock is not None:
        kwargs["clock"] = clock

    async def go():
        await eng.run_direct_harvest_loop(
            hass=fake_hass,
            store=None,
            cadence=type("C", (), {"interval_s": interval_s})(),
            inverter_id="inv-1",
            cfg={"ip": "x", "logger_serial": "1"},
            spec_holder=type("SH", (), {"spec": SPEC})(),
            **kwargs,
        )
        return delays, polls

    return go()


@pytest.mark.asyncio
async def test_gated_first_tick_retries_within_seconds(monkeypatch):
    delays, polls = await _run_loop_with(monkeypatch, ["gated", "gated", "stored"])
    assert polls == 3
    # Short back-off before the first stored reading, well inside the app's 90 s.
    assert delays[0] <= 5
    assert delays[1] <= 10
    assert sum(delays[:2]) < 90


@pytest.mark.asyncio
async def test_failed_first_tick_retries_within_seconds(monkeypatch):
    delays, polls = await _run_loop_with(monkeypatch, ["error", "stored"])
    assert polls == 2
    assert delays[0] <= 5


@pytest.mark.asyncio
async def test_first_reading_backoff_is_capped_well_under_the_app_budget(monkeypatch):
    delays, _ = await _run_loop_with(monkeypatch, ["gated"] * 6 + ["stored"])
    assert max(delays[:6]) <= 30
    # The first four retries all land inside the app's 90 s first-reading wait.
    assert sum(delays[:4]) < 90


@pytest.mark.asyncio
async def test_after_first_stored_reading_the_loop_uses_the_normal_cadence(monkeypatch):
    delays, _ = await _run_loop_with(monkeypatch, ["gated", "stored", "gated", "stored"])
    # delays[0] is the fast retry; after the stored reading every sleep is the
    # cadence, including after a later gated tick.
    assert delays[1] == 300
    assert delays[2] == 300


@pytest.mark.asyncio
async def test_gated_tick_is_recorded_with_its_missing_fields(monkeypatch):
    from custom_components.svitgrid.activity import ActivityTracker

    activity = ActivityTracker()

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()

    async def fake_poll_once(**kwargs):
        kwargs["missing_out"].append("loadPower")
        fake_hass.is_stopping = True
        return None

    monkeypatch.setattr(eng, "poll_once", fake_poll_once)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=type("C", (), {"interval_s": 300})(),
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1"},
        spec_holder=type("SH", (), {"spec": SPEC})(),
        activity=activity,
    )

    assert activity.last_ingest_status == "skipped"
    assert list(activity.recent_ingests())[-1]["missing_fields"] == ["loadPower"]


@pytest.mark.asyncio
async def test_poll_once_reports_missing_fields_when_gated(hass, monkeypatch):
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value={1: {588: 50}}))
    store = type("S", (), {"append": AsyncMock()})()
    missing: list[str] = []
    result = await eng.poll_once(
        hass=hass,
        spec=SPEC,
        cfg={"ip": "x", "logger_serial": "1"},
        inverter_id="inv-1",
        store=store,
        missing_out=missing,
    )
    assert result is None
    assert "loadPower" in missing


# ---------------------------------------------------------------------------
# #7: "Read now" (button, panel, app poll_now) wakes the loop for exactly one
# extra poll. The regular schedule keeps its original deadline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_now_runs_one_extra_poll_without_resetting_the_cadence(monkeypatch):
    from custom_components.svitgrid.harvest.read_now import ReadNowTrigger

    now = [0.0]
    timeouts: list[float] = []
    # Each wait: (seconds to advance, press Read now?)
    steps = [(100.0, True), (200.0, False)]

    class ScriptedTrigger(ReadNowTrigger):
        async def wait(self, seconds):
            timeouts.append(seconds)
            advance, press = steps.pop(0)
            now[0] += advance
            if press:
                assert self.request() is not None
            return self.requested

    trigger = ScriptedTrigger()
    delays, polls = await _run_loop_with(
        monkeypatch,
        ["stored", "stored", "stored"],
        read_now=trigger,
        clock=lambda: now[0],
    )
    # Regular poll at t=0, extra poll at t=100, regular poll at t=300.
    assert polls == 3
    assert timeouts == [300.0, 200.0]
    assert delays == []  # the trigger replaces asyncio.sleep


@pytest.mark.asyncio
async def test_read_now_is_refused_while_a_poll_is_in_flight(monkeypatch):
    from custom_components.svitgrid.harvest.read_now import ReadNowTrigger

    trigger = ReadNowTrigger()
    seen: list = []

    class FakeHass:
        is_stopping = False

    fake_hass = FakeHass()

    async def fake_poll_once(**kwargs):
        seen.append(trigger.request())
        fake_hass.is_stopping = True
        return {"inverterId": "inv-1"}

    monkeypatch.setattr(eng, "poll_once", fake_poll_once)

    await eng.run_direct_harvest_loop(
        hass=fake_hass,
        store=None,
        cadence=type("C", (), {"interval_s": 300})(),
        inverter_id="inv-1",
        cfg={"ip": "x", "logger_serial": "1"},
        spec_holder=type("SH", (), {"spec": SPEC})(),
        read_now=trigger,
    )
    assert seen == [None]
    assert trigger.requested is False


@pytest.mark.asyncio
async def test_read_now_request_is_resolved_with_the_poll_outcome():
    from custom_components.svitgrid.harvest.read_now import ReadNowTrigger

    trigger = ReadNowTrigger()
    fut = trigger.request()
    waiters = trigger.begin_tick()
    assert trigger.in_flight is True
    trigger.end_tick(waiters, {"outcome": "incomplete", "missingFields": ["loadPower"]})
    assert trigger.in_flight is False
    assert (await fut)["missingFields"] == ["loadPower"]
    assert trigger.last_outcome["outcome"] == "incomplete"
