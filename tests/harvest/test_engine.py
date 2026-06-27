"""Tests for harvest/engine.py — poll_once + run_direct_harvest_loop."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest import engine as eng
from custom_components.svitgrid.harvest.register_spec import RegisterSpec

SPEC = RegisterSpec.from_dict({
    "modelId": "deye_sg04lp3", "version": 1, "protocol": "solarman_v5", "port": 8899,
    "defaultSlaveId": 1, "flags": {"batteryPositiveIsDischarge": True},
    "reads": [
        {"field": "batterySoc", "address": 588},
        {"field": "batteryPower", "address": 590, "signed": True},
        {"field": "batteryVoltage", "address": 587, "scale": 0.01},
        {"field": "gridPower", "address": 625, "signed": True},
        {"field": "loadPower", "address": 653},
        {"field": "pv1Power", "address": 672}, {"field": "pv2Power", "address": 673},
    ],
    "derivations": [
        {"field": "batteryPower", "op": "builtin", "builtin": "battery_sign_normalize",
         "inputs": ["batteryPower"]},
        {"field": "totalPvPower", "op": "sum", "inputs": ["pv1Power", "pv2Power"]},
    ],
    "writes": [],
})


@pytest.mark.asyncio
async def test_poll_once_appends_payload(hass, monkeypatch):
    raw = {1: {588: 78, 590: 1500, 587: 5230, 625: 64536, 653: 1800, 672: 1500, 673: 800}}
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value=raw))
    store = type("S", (), {"append": AsyncMock()})()
    ok = await eng.poll_once(hass=hass, spec=SPEC, cfg={"ip": "x", "logger_serial": "1"},
                             inverter_id="inv-1", store=store)
    assert ok is True
    store.append.assert_awaited_once()
    payload = store.append.await_args[0][0]
    assert payload["batterySoc"] == 78.0
    assert payload["batteryPower"] == -1500.0   # sign-normalized
    assert payload["gridPower"] == -1000.0
    assert payload["pvPower"] == 2300.0
    assert payload["pvPower1"] == 1500.0 and payload["pvPower2"] == 800.0


@pytest.mark.asyncio
async def test_poll_once_gated_when_required_missing(hass, monkeypatch):
    # only batterySoc present → CORE_PAYLOAD_FIELDS missing → gated, not appended
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value={1: {588: 50}}))
    store = type("S", (), {"append": AsyncMock()})()
    ok = await eng.poll_once(hass=hass, spec=SPEC, cfg={"ip": "x", "logger_serial": "1"},
                             inverter_id="inv-1", store=store)
    assert ok is False
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
