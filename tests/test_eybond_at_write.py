"""`CollectorSession.write_register`: its parsing, and its correlation guards.

Bare Modbus RTU carries no transaction id -- frames correlate by ORDER alone.
These tests stub `CollectorSession._transact` directly rather than driving a
real socket (see `test_eybond_at_session.py` for that heavier harness): what
is under test here is `write_register`'s own parsing, its address guard and
its quantity guard, not the scheduler or the framing layer underneath it.

The write frame is FC16. See `test_eybond_at_write_fc16.py` for why, and for
the captured hardware vectors this file's helpers reproduce.
"""

import pytest

from custom_components.svitgrid.eybond_at.modbus_rtu import FC_WRITE_MULTIPLE, crc16
from custom_components.svitgrid.eybond_at.session import (
    CollectorSession,
    TransactionFailed,
    WriteRefused,
)


def ack_frame(slave: int, address: int, quantity: int = 1) -> bytes:
    """An FC16 acknowledgement: address and QUANTITY, never the value.

    Built here from raw bytes rather than from a codec helper, so this harness
    cannot drift into agreeing with a wrong encoder.
    """
    body = bytes(
        [
            slave,
            FC_WRITE_MULTIPLE,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (quantity >> 8) & 0xFF,
            quantity & 0xFF,
        ]
    )
    return body + crc16(body).to_bytes(2, "little")


def exception_frame(slave: int, code: int) -> bytes:
    body = bytes([slave, FC_WRITE_MULTIPLE | 0x80, code])
    return body + crc16(body).to_bytes(2, "little")


def make_session(responses: list[bytes]) -> CollectorSession:
    """A session whose `_transact` returns canned responses in order, ignoring
    whatever payload it was asked to send -- exactly the desync scenario this
    module exists to guard against."""
    session = CollectorSession(
        writer=None,
        address="test",
        slave_id=1,
        txn_timeout_ms=1_000,
        clock=lambda: 0.0,
    )
    queue = list(responses)
    session.sent: list[bytes] = []

    async def fake_transact(payload: bytes, timeout_s: float) -> bytes:
        session.sent.append(payload)
        return queue.pop(0)

    session._transact = fake_transact
    return session


async def test_write_register_sends_an_fc16_frame():
    session = make_session(responses=[ack_frame(1, 303)])
    await session.write_register(303, 0)
    assert session.sent[0][1] == FC_WRITE_MULTIPLE
    assert session.sent[0] == bytes.fromhex("0110012f0001020000b10f")


async def test_write_register_accepts_the_acknowledgement():
    """An FC16 ack carries no value, so there is nothing here to mistake for
    confirmation -- `write_register` returns None by design. Only the
    read-back in `write_register_verified` decides whether a write took."""
    session = make_session(responses=[ack_frame(1, 303)])
    assert await session.write_register(303, 0) is None


async def test_an_acknowledgement_is_accepted_whatever_the_value_written():
    """The regression that an echoed-VALUE check would cause.

    The ack says `quantity=1` no matter what was written. Comparing it against
    the value would pass for a write of 1 and fail for every other value --
    including 0, which is what the hardware capture actually wrote.
    """
    for value in (0, 1, 560, 65535):
        session = make_session(responses=[ack_frame(1, 324)])
        assert await session.write_register(324, value) is None


async def test_a_reply_for_a_different_address_is_a_failure():
    # Bare Modbus RTU carries no transaction id, so replies correlate by ORDER
    # only. A desync surfaces as a reply belonging to the PREVIOUS request; if
    # write_register accepted it, we would report a write to 303 that actually
    # landed on 324 -- a protective register.
    session = make_session(responses=[ack_frame(1, 324)])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)


async def test_a_reply_acknowledging_a_different_count_is_a_failure():
    """We send exactly one register. An ack for two describes a frame we did
    not send, which means the stream is not carrying what we think it is."""
    session = make_session(responses=[ack_frame(1, 303, quantity=2)])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)


async def test_a_malformed_frame_raises_rather_than_returning_garbage():
    session = make_session(responses=[b"\x01\x10\xff"])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)


async def test_an_fc06_reply_is_not_accepted():
    """Nothing on this device has ever produced one; accepting it would
    quietly re-open the path this fix closes."""
    body = bytes([0x01, 0x06, 0x01, 0x2F, 0x00, 0x00])
    session = make_session(responses=[body + crc16(body).to_bytes(2, "little")])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)


# ── refusals arrive typed, not flattened ─────────────────────────────────


@pytest.mark.parametrize(
    ("code", "reason"),
    [(0x01, "read_only"), (0x03, "out_of_range"), (0x07, "wrong_mode")],
)
async def test_a_refusal_raises_write_refused_carrying_the_code(code, reason):
    """A refusal is a real answer from the device, and each code implies a
    different thing to tell the user. Flattening them into one
    `TransactionFailed` string throws that away."""
    session = make_session(responses=[exception_frame(1, code)])
    with pytest.raises(WriteRefused) as excinfo:
        await session.write_register(303, 0)
    assert excinfo.value.code == code
    assert excinfo.value.reason == reason


async def test_write_refused_is_still_a_transaction_failure():
    """Existing callers catch `TransactionFailed`. A refusal must not escape
    past them as an unhandled exception type."""
    session = make_session(responses=[exception_frame(1, 0x07)])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)
