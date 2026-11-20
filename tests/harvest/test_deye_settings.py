"""`set_inverter_setting` for a Deye hybrid (svitgrid D-2346).

The planner is a port of `planWrite` in the monorepo's
`packages/inverter_protocol/lib/src/protocol/deye/deye_settings.dart`; these
cases mirror `deye_settings_executor_test.dart`, so the add-on refuses what
the phone and the board refuse.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.harvest import deye_settings as ds

MONOREPO_CATALOGUE = (
    Path.home() / "git" / "svitgrid" / "packages" / "inverter_protocol" / "deye-settings.json"
)


class FakeInverter:
    """Holding registers, with reads and writes recorded."""

    def __init__(self, words: dict[int, int], *, fail_reads=(), keep_old=False):
        self.words = dict(words)
        self.fail_reads = set(fail_reads)
        self.keep_old = keep_old
        self.writes: list[tuple[int, int]] = []

    async def read_word(self, register: int):
        if register in self.fail_reads:
            return None
        return self.words.get(register, 0)

    async def write_word(self, register: int, word: int):
        self.writes.append((register, word))
        if not self.keep_old:
            self.words[register] = word


async def _no_sleep(_s):
    return None


async def _apply(inv: FakeInverter, model: str, setting: str, value, **extra):
    return await ds.apply_deye_setting(
        model_id=model,
        payload={"inverterId": "inv-1", "setting": setting, "value": value, **extra},
        read_word=inv.read_word,
        write_word=inv.write_word,
        sleep=_no_sleep,
    )


# 3-phase: battery mode SOC, Shutdown 10, Restart 50, Low Batt 20.
def _three_phase(over: dict[int, int] | None = None) -> FakeInverter:
    return FakeInverter({111: 1, 115: 10, 116: 50, 117: 20, 130: 0x0001, **(over or {})})


def test_vendored_catalogue_is_the_monorepo_file():
    """`scripts/sync-edge-command-fixtures.sh` copies it; a drift means a
    host plans differently from the phone, the board and the API."""
    if not MONOREPO_CATALOGUE.exists():
        pytest.skip("monorepo checkout not found")
    assert json.loads(ds.CATALOGUE_PATH.read_text()) == json.loads(MONOREPO_CATALOGUE.read_text())


def test_the_catalogue_writes_no_blocked_register():
    for s in ds.CATALOGUE.settings:
        assert s.register3p not in ds._BLOCKED_REGISTERS
        assert s.register1p not in ds._BLOCKED_REGISTERS


@pytest.mark.asyncio
async def test_writes_low_batt_on_three_phase_and_reads_it_back():
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "lowBattSoc", 25)
    assert r == {"ok": True, "message": None, "readBack": 25}
    assert inv.writes == [(117, 25)]


@pytest.mark.asyncio
async def test_writes_on_the_one_phase_register():
    inv = FakeInverter({213: 1, 217: 10, 218: 50, 219: 20})
    r = await _apply(inv, "deye_sg03lp1", "lowBattSoc", 25)
    assert r["ok"] is True
    assert inv.writes == [(219, 25)]


@pytest.mark.asyncio
async def test_bit_setting_changes_only_its_bit():
    # Bit 0 of 130 is the grid-charge master and must survive.
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "gridChargeStartSocEnabled", 1)
    assert r["ok"] is True and r["readBack"] == 1
    assert inv.writes == [(130, 0x0003)]


@pytest.mark.asyncio
async def test_a_failed_pre_read_writes_nothing():
    inv = _three_phase()
    inv.fail_reads = {130}
    r = await _apply(inv, "deye_sg04lp3", "gridChargeStartSocEnabled", 1)
    assert r["ok"] is False
    assert "Register 130 could not be read" in r["message"]
    assert inv.writes == []


@pytest.mark.asyncio
async def test_refuses_a_threshold_outside_percent_mode():
    inv = _three_phase({111: 0})
    r = await _apply(inv, "deye_sg04lp3", "lowBattSoc", 25)
    assert r["ok"] is False
    assert "percent thresholds" in r["message"]
    assert r["readBack"] == 20
    assert inv.writes == []


@pytest.mark.asyncio
async def test_refuses_shutdown_at_or_above_low_batt():
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "shutdownSoc", 20)
    assert r == {"ok": False, "message": "Shutdown must be below Low Batt.", "readBack": 10}
    assert inv.writes == []


@pytest.mark.asyncio
async def test_refuses_a_partner_that_could_not_be_read():
    inv = _three_phase()
    inv.fail_reads = {116}
    r = await _apply(inv, "deye_sg04lp3", "shutdownSoc", 5)
    assert r["ok"] is False
    assert inv.writes == []


@pytest.mark.asyncio
async def test_max_sell_power_needs_the_rated_power():
    inv = _three_phase({143: 5000})
    r = await _apply(inv, "deye_sg04lp3", "maxSellPowerW", 6000)
    assert r["ok"] is False and "rated power" in r["message"]
    r = await _apply(inv, "deye_sg04lp3", "maxSellPowerW", 6000, ratedPowerW=12000)
    assert r["ok"] is True and inv.writes == [(143, 6000)]
    r = await _apply(inv, "deye_sg04lp3", "maxSellPowerW", 13000, ratedPowerW=12000)
    assert r["ok"] is False and r["message"] == "maxSellPowerW must be 0-12000W."


@pytest.mark.asyncio
async def test_max_sell_power_is_never_written_on_a_ten_watt_model():
    inv = _three_phase({143: 500})
    r = await _apply(inv, "deye_sg01hp3_50k", "maxSellPowerW", 3000, ratedPowerW=50000)
    assert r["ok"] is False and "10 W" in r["message"]
    assert inv.writes == []


@pytest.mark.asyncio
async def test_gen_port_mode_takes_only_its_choices():
    inv = _three_phase({133: 0})
    assert (await _apply(inv, "deye_sg04lp3", "genPortMode", 3))["ok"] is False
    assert (await _apply(inv, "deye_sg04lp3", "genPortMode", 2))["ok"] is True
    assert inv.writes == [(133, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "25", 25.5, None])
async def test_refuses_anything_but_a_whole_number(value):
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "lowBattSoc", value)
    assert r["ok"] is False and "whole number" in r["message"]
    assert inv.writes == []


@pytest.mark.asyncio
async def test_a_value_already_set_writes_nothing():
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "lowBattSoc", 20)
    assert r == {"ok": True, "message": None, "readBack": 20}
    assert inv.writes == []


@pytest.mark.asyncio
async def test_reports_a_write_the_inverter_ignored():
    inv = _three_phase()
    inv.keep_old = True
    r = await _apply(inv, "deye_sg04lp3", "lowBattSoc", 25)
    assert r == {"ok": False, "message": "The inverter kept its old value.", "readBack": 20}


@pytest.mark.asyncio
async def test_unknown_setting_and_non_deye_model_are_refused_unread():
    inv = _three_phase()
    r = await _apply(inv, "deye_sg04lp3", "voltageMode", 1)
    assert r["ok"] is False and "does not write voltageMode" in r["message"]
    r = await _apply(inv, "huawei_sun2000_10ktl_m1", "lowBattSoc", 25)
    assert r["ok"] is False and "not in the Deye family" in r["message"]
    assert inv.writes == []


# ── WriteExecutor wiring ──────────────────────────────────────────────

_WE = "custom_components.svitgrid.harvest.write_executor"


def _executor(model_id: str):
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    spec = SimpleNamespace(default_slave_id=1, writes=[])
    cfg = {"ip": "192.168.1.100", "logger_serial": "1", "port": "8899", "model_id": model_id}
    return WriteExecutor(object(), SimpleNamespace(spec=spec), cfg, sleep_fn=_no_sleep)


@pytest.mark.asyncio
async def test_write_executor_runs_set_inverter_setting_on_a_deye():
    inv = _three_phase()

    async def read_word(hass, spec, cfg, unit, addr):
        assert unit == 1
        return await inv.read_word(addr)

    async def write_registers(hass, spec, cfg, writes):
        for _unit, addr, word in writes:
            await inv.write_word(addr, word)

    ex = _executor("deye_sg04lp3")
    with (
        patch(f"{_WE}.read_word", AsyncMock(side_effect=read_word)),
        patch(f"{_WE}.write_registers", AsyncMock(side_effect=write_registers)),
    ):
        r = await ex.dispatch(
            "set_inverter_setting", {"inverterId": "inv-1", "setting": "lowBattSoc", "value": 25}
        )
    assert r == {"ok": True, "message": None, "readBack": 25}
    assert inv.writes == [(117, 25)]


@pytest.mark.asyncio
async def test_write_executor_leaves_set_inverter_setting_unsupported_off_deye():
    ex = _executor("huawei_sun2000_10ktl_m1")
    with pytest.raises(NotImplementedError):
        await ex.dispatch("set_inverter_setting", {"setting": "lowBattSoc", "value": 25})
