"""The battery charge current cap on a directly harvested inverter.

A ``set_battery_charge_current`` writes Deye "Max A Charge" (register 108 on a
3-phase LV unit, 210 on a 1-phase one). The register survives an inverter
restart, so a cap whose restore never arrives would stop the battery charging
for good. ``ChargeCapGuard`` keeps the inverter's own value (the held prior)
on disk before the write, and a revert deadline the scheduler tick honours.

Mirrors the edge firmware's ``execute_battery_charge_current`` and
``bcc_guard_board_tick`` (zigbee_poller.c) and the phone harvester's
``BatteryChargeCurrentPriorStore``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.svitgrid.harvest.charge_cap_guard import (
    BatteryChargeCurrentError,
    ChargeCapGuard,
    charge_cap_guard_tick,
)
from custom_components.svitgrid.harvest.register_spec import RegisterSpec

MODULE = "custom_components.svitgrid.harvest.write_executor"
_SPECS = Path(__file__).resolve().parents[1] / "custom_components" / "svitgrid" / "register_specs"
_CFG = {"ip": "192.168.1.100", "logger_serial": "99999", "port": "8899", "slave_id": "1"}
_REVERT_AT = 1_800_000_000_000


def _spec(model_id: str) -> RegisterSpec:
    return RegisterSpec.from_dict(json.loads((_SPECS / f"{model_id}.json").read_text()))


@pytest.fixture
def guard(tmp_path):
    return ChargeCapGuard(str(tmp_path / "charge_cap_guard.db"))


# ═══════════════════════════ The store ═══════════════════════════════════════


async def test_a_new_guard_holds_nothing(guard):
    assert await guard.prior("inv-1") is None
    assert await guard.due(_REVERT_AT) == []


async def test_record_prior_keeps_the_first_value(guard):
    await guard.record_prior("inv-1", 120)
    # A later set reads the cap itself back: it must not replace the owner's.
    await guard.record_prior("inv-1", 0)
    assert await guard.prior("inv-1") == 120


async def test_zero_is_a_legal_prior(guard):
    await guard.record_prior("inv-1", 0)
    assert await guard.prior("inv-1") == 0


@pytest.mark.parametrize("bad", [-1, 351, 12.5, True, None, "100"])
async def test_record_prior_refuses_a_value_outside_the_register(guard, bad):
    with pytest.raises(ValueError):
        await guard.record_prior("inv-1", bad)
    assert await guard.prior("inv-1") is None


async def test_a_deadline_is_due_once_it_has_passed(guard):
    await guard.record_prior("inv-1", 120)
    await guard.set_deadline("inv-1", _REVERT_AT)
    assert await guard.due(_REVERT_AT - 1) == []
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]


async def test_a_deadline_without_a_prior_is_not_kept(guard):
    await guard.set_deadline("inv-1", _REVERT_AT)
    assert await guard.due(_REVERT_AT + 1) == []


async def test_clear_drops_the_prior_and_its_deadline(guard):
    await guard.record_prior("inv-1", 120)
    await guard.set_deadline("inv-1", _REVERT_AT)
    await guard.clear("inv-1")
    assert await guard.prior("inv-1") is None
    assert await guard.due(_REVERT_AT + 1) == []


async def test_the_guard_survives_a_restart(tmp_path):
    path = str(tmp_path / "g.db")
    await ChargeCapGuard(path).record_prior("inv-1", 90)
    await ChargeCapGuard(path).set_deadline("inv-1", _REVERT_AT)
    assert await ChargeCapGuard(path).due(_REVERT_AT) == [("inv-1", 90)]


async def test_forget_except_drops_inverters_that_left_the_config(guard):
    await guard.record_prior("inv-1", 90)
    await guard.record_prior("inv-gone", 60)
    await guard.forget_except({"inv-1"})
    assert await guard.prior("inv-1") == 90
    assert await guard.prior("inv-gone") is None


# ═══════════════════════════ Dispatch ════════════════════════════════════════


class _Inverter:
    """One holding register file behind read_word / write_registers."""

    def __init__(self, regs: dict[int, int] | None = None):
        self.regs = dict(regs or {})
        self.writes: list[list[tuple[int, int, int]]] = []
        self.read_fails = False
        self.write_fails = False
        self.sticks = False  # the inverter acks the write and keeps its value

    async def read_word(self, hass, spec, cfg, unit, addr, *a):
        return None if self.read_fails else self.regs.get(addr)

    async def write_registers(self, hass, spec, cfg, writes):
        self.writes.append(list(writes))
        if self.write_fails:
            raise OSError("write timeout")
        if not self.sticks:
            for _unit, addr, value in writes:
                self.regs[addr] = value


def _executor(model_id, guard, inverter: _Inverter, monkeypatch, inverter_id="inv-1"):
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    monkeypatch.setattr(f"{MODULE}.read_word", inverter.read_word)
    monkeypatch.setattr(f"{MODULE}.write_registers", inverter.write_registers)
    holder = SimpleNamespace(spec=_spec(model_id))
    return WriteExecutor(object(), holder, _CFG, guard=guard, inverter_id=inverter_id)


async def test_a_set_records_the_prior_before_it_writes(guard, monkeypatch):
    inv = _Inverter({108: 120})
    seen = {}

    async def write(hass, spec, cfg, writes):
        seen["prior"] = await guard.prior("inv-1")
        inv.regs[108] = writes[0][2]

    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    monkeypatch.setattr(f"{MODULE}.write_registers", write)
    result = await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    assert seen["prior"] == 120
    assert result["prevBatteryChargeCurrentA"] == 120
    assert result["targetBatteryChargeCurrentA"] == 0
    assert result["verifyBatteryChargeCurrentA"] == 0


async def test_a_failed_pre_read_writes_nothing(guard, monkeypatch):
    inv = _Inverter({108: 120})
    inv.read_fails = True
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    with pytest.raises(BatteryChargeCurrentError) as err:
        await ex.dispatch(
            "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
        )
    assert str(err.value) == "prior_read_failed"
    assert err.value.ack == {"error": "prior_read_failed"}
    assert inv.writes == []
    assert await guard.prior("inv-1") is None
    assert await guard.due(_REVERT_AT + 1) == []


async def test_a_pre_read_above_the_register_bound_writes_nothing(guard, monkeypatch):
    inv = _Inverter({108: 351})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    with pytest.raises(BatteryChargeCurrentError, match="prior_read_failed"):
        await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    assert inv.writes == []


async def test_a_re_set_acks_the_held_prior_not_the_cap(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    # The renewal reads the rule's own 0 A back.
    result = await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
    )
    assert result["prevBatteryChargeCurrentA"] == 120
    assert result["skipped"] is True
    assert result["verifyBatteryChargeCurrentA"] == 0
    assert len(inv.writes) == 1
    # A skipped set is how the API renews the deadline.
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]


async def test_a_set_clamps_its_target_to_the_register(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    result = await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 999})
    assert result["targetBatteryChargeCurrentA"] == 350
    assert inv.writes == [[(1, 108, 350)]]


async def test_the_revert_is_armed_only_below_the_prior(guard, monkeypatch):
    inv = _Inverter({108: 50})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 80, "revertAtMs": _REVERT_AT}
    )
    assert await guard.due(_REVERT_AT + 1) == []
    assert await guard.prior("inv-1") == 50


async def test_the_revert_is_armed_when_the_write_fails(guard, monkeypatch):
    inv = _Inverter({108: 120})
    inv.write_fails = True
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    with pytest.raises(BatteryChargeCurrentError) as err:
        await ex.dispatch(
            "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
        )
    assert str(err.value) == "write_failed"
    assert err.value.ack == {
        "error": "write_failed",
        "prevBatteryChargeCurrentA": 120,
        "targetBatteryChargeCurrentA": 0,
    }
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]


async def test_a_verify_mismatch_fails_and_arms_the_revert(guard, monkeypatch):
    inv = _Inverter({108: 120})
    inv.sticks = True
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    with pytest.raises(BatteryChargeCurrentError) as err:
        await ex.dispatch(
            "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
        )
    assert str(err.value) == "verify_mismatch"
    assert err.value.ack == {
        "error": "verify_mismatch",
        "prevBatteryChargeCurrentA": 120,
        "targetBatteryChargeCurrentA": 0,
        "verifyBatteryChargeCurrentA": 120,
    }
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]


async def test_a_restore_without_a_value_writes_the_held_prior_and_clears_it(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
    )
    result = await ex.dispatch("restore_battery_charge_current", {})
    assert inv.writes[-1] == [(1, 108, 120)]
    assert result["prevBatteryChargeCurrentA"] == 0
    assert result["targetBatteryChargeCurrentA"] == 120
    assert result["verifyBatteryChargeCurrentA"] == 120
    assert await guard.prior("inv-1") is None
    assert await guard.due(_REVERT_AT + 1) == []


async def test_a_restore_takes_the_payload_value_over_the_prior(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    await ex.dispatch("restore_battery_charge_current", {"batteryChargeCurrentA": 100})
    assert inv.regs[108] == 100
    assert await guard.prior("inv-1") is None


async def test_a_restore_with_no_value_and_no_prior_writes_nothing(guard, monkeypatch):
    inv = _Inverter({108: 0})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    with pytest.raises(BatteryChargeCurrentError) as err:
        await ex.dispatch("restore_battery_charge_current", {})
    assert str(err.value) == "no_prior"
    assert inv.writes == []


async def test_an_unconfirmed_restore_keeps_the_prior(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    inv.sticks = True
    with pytest.raises(BatteryChargeCurrentError, match="verify_mismatch"):
        await ex.dispatch("restore_battery_charge_current", {})
    assert await guard.prior("inv-1") == 120


async def test_a_restore_already_at_its_value_is_skipped_and_clears(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await guard.record_prior("inv-1", 120)
    result = await ex.dispatch("restore_battery_charge_current", {})
    assert result["skipped"] is True
    assert inv.writes == []
    assert await guard.prior("inv-1") is None


async def test_an_hv_model_has_no_recipe(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg01hp3", guard, inv, monkeypatch)
    with pytest.raises(NotImplementedError):
        await ex.dispatch("set_battery_charge_current", {"batteryChargeCurrentA": 0})
    assert inv.writes == []


# ═══════════════════════════ The revert tick ═════════════════════════════════


async def test_a_due_deadline_writes_the_prior_back_and_clears(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
    )

    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT - 1)
    assert inv.regs[108] == 0

    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT)
    assert inv.regs[108] == 120
    assert await guard.prior("inv-1") is None


async def test_a_failed_revert_is_retried_on_the_next_tick(guard, monkeypatch):
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
    )
    inv.write_fails = True
    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT)
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]

    inv.write_fails = False
    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT)
    assert inv.regs[108] == 120
    assert await guard.prior("inv-1") is None


async def test_a_revert_for_an_inverter_with_no_executor_drops_its_prior(guard):
    await guard.record_prior("inv-gone", 120)
    await guard.set_deadline("inv-gone", _REVERT_AT)
    await charge_cap_guard_tick(guard, {}.get, _REVERT_AT)
    assert await guard.prior("inv-gone") is None


async def test_a_revert_waits_for_a_spec_that_is_not_loaded_yet(guard):
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    ex = WriteExecutor(object(), SimpleNamespace(spec=None), _CFG, guard=guard, inverter_id="inv-1")
    await guard.record_prior("inv-1", 120)
    await guard.set_deadline("inv-1", _REVERT_AT)
    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT)
    assert await guard.due(_REVERT_AT) == [("inv-1", 120)]


async def test_a_deadline_moved_after_the_tick_listed_it_is_not_reverted(guard, monkeypatch):
    """The tick lists due priors outside the executor's lock. A renewal that
    lands in between moves the deadline, and the revert must not undo it."""
    inv = _Inverter({108: 120})
    ex = _executor("deye_sg04lp3", guard, inv, monkeypatch)
    await ex.dispatch(
        "set_battery_charge_current", {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT}
    )
    listed = guard.due

    async def due_then_renew(now_ms):
        stale = await listed(now_ms)
        await ex.dispatch(
            "set_battery_charge_current",
            {"batteryChargeCurrentA": 0, "revertAtMs": _REVERT_AT + 6 * 3600_000},
        )
        return stale

    monkeypatch.setattr(guard, "due", due_then_renew)
    await charge_cap_guard_tick(guard, {"inv-1": ex}.get, _REVERT_AT)
    assert inv.regs[108] == 0
    assert await guard.prior("inv-1") == 120
    assert await guard.deadline("inv-1") == _REVERT_AT + 6 * 3600_000
