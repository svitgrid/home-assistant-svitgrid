"""What the user is told when the device refuses a settings write.

Three refusals are documented for this family, and they call for three
different next steps:

  0x01  the register is read-only               -> nothing the user can do
  0x03  the value is out of the permitted range -> or a CROSS-REGISTER rule
  0x07  not modifiable in the current working   -> field-reported; one user's
        mode                                       writes began working only
                                                   after an inverter restart

Collapsing them into "write failed" is what makes 0x07 unsolvable from the
outside: the user retries the identical write forever because nothing ever
suggested the obstacle is the mode rather than the value.

The 0x03 case has its own trap. The vendor documents that register 341 must
exceed 343, so an individually-valid percentage can still be refused. Reporting
that as "your value is malformed" sends the user to change the one field that
was already correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.svitgrid.executors.smg_settings_executor import (
    SmgSettingsExecutor,
    write_register_verified,
)
from custom_components.svitgrid.eybond_at.session import TransactionFailed, WriteRefused
from custom_components.svitgrid.eybond_at.smg_settings import smg_ii_protocol_number
from tests.test_smg_settings_executor import FakeLink, bench_registers


class RefusingLink(FakeLink):
    """A device that answers a write with a Modbus exception."""

    def __init__(self, registers, *, code: int, reason: str):
        super().__init__(registers)
        self._code = code
        self._reason = reason

    async def write_register(self, address: int, value: int, timeout_s: float = 5.0) -> None:
        self.writes.append((address, value))
        raise WriteRefused(code=self._code, reason=self._reason, message="refused")


def _executor(link) -> SmgSettingsExecutor:
    return SmgSettingsExecutor(
        link=link, protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24
    )


# ── write_register_verified surfaces the refusal instead of raising ───────


@pytest.mark.parametrize(
    ("code", "reason"),
    [(0x01, "read_only"), (0x03, "out_of_range"), (0x07, "wrong_mode")],
)
async def test_a_refusal_comes_back_as_its_own_outcome(code, reason):
    link = RefusingLink(bench_registers(), code=code, reason=reason)
    result = await write_register_verified(link, address=303, value=1)
    assert result.ok is False
    assert result.skipped is False
    assert result.refusal == reason


async def test_a_refusal_is_not_reported_as_an_unverifiable_write():
    """A refused write and a write that landed-but-could-not-be-read-back are
    opposite situations. The first definitely did nothing; the second may well
    have worked. Reporting them identically loses the only fact worth having.
    """
    link = RefusingLink(bench_registers(), code=0x07, reason="wrong_mode")
    refused = await write_register_verified(link, address=303, value=1)
    assert refused.refusal == "wrong_mode"
    assert refused.read_back is None

    ok_link = FakeLink(bench_registers())
    landed = await write_register_verified(ok_link, address=303, value=1)
    assert landed.refusal is None


async def test_a_plain_transport_failure_still_has_no_refusal_reason():
    """`TransactionFailed` with no exception code behind it is a timeout or a
    dropped line, not the device declining. Inventing a reason for it would
    tell the user something untrue."""

    class DeadLink(FakeLink):
        async def write_register(self, address, value, timeout_s=5.0):
            raise TransactionFailed("timed out waiting for the collector")

    link = DeadLink(bench_registers())
    with pytest.raises(TransactionFailed):
        await write_register_verified(link, address=303, value=1)


# ── the refusal reaches the command response ─────────────────────────────


async def test_the_dispatch_response_carries_the_refusal_reason():
    link = RefusingLink(bench_registers(), code=0x07, reason="wrong_mode")
    result = await _executor(link).dispatch(
        "set_inverter_setting", {"setting": "buzzerMode", "value": 1}
    )
    assert result["ok"] is False
    assert result["refusal"] == "wrong_mode"
    assert result["message"]


async def test_a_successful_write_reports_no_refusal():
    link = FakeLink(bench_registers())
    result = await _executor(link).dispatch(
        "set_inverter_setting", {"setting": "buzzerMode", "value": 1}
    )
    assert result["ok"] is True
    assert result["refusal"] is None


async def test_a_locally_rejected_write_reports_no_refusal_and_sends_nothing():
    """Our own validation refusing a value is not the DEVICE refusing it.
    Labelling it with a device reason would misattribute the decision."""
    link = FakeLink(bench_registers())
    result = await _executor(link).dispatch(
        "set_inverter_setting", {"setting": "nonsuchSetting", "value": 1}
    )
    assert result["ok"] is False
    assert result["refusal"] is None
    assert link.writes == []


# ── writes stay user-initiated ───────────────────────────────────────────


async def test_reading_the_block_never_writes():
    """These registers live in flash with undocumented write endurance, so
    nothing may write on a poll, a timer, or a coordinator refresh. A read is
    the operation those paths perform."""
    link = FakeLink(bench_registers())
    executor = _executor(link)
    await executor.read()
    await executor.read_all()
    await executor.dispatch("read_inverter_settings", {})
    assert link.writes == []


async def test_a_write_that_would_change_nothing_is_skipped():
    """Flash endurance again: re-applying the value already held must not cost
    a write cycle."""
    link = FakeLink(bench_registers())
    held = link.registers[303]
    result = await write_register_verified(link, address=303, value=held)
    assert result.skipped is True
    assert result.ok is True
    assert link.writes == []


def test_the_harvest_loop_holds_no_write_call():
    """The poll path must not be able to reach a write at all.

    Asserted against the source rather than by driving the loop: what matters
    is that no future edit quietly adds one, and a behavioural test only covers
    the branches it happens to walk.
    """
    from custom_components.svitgrid.eybond_at import harvest

    source = Path(harvest.__file__).read_text()
    assert "write_register" not in source
