"""CollectorSession.write_register: the codec had a write frame builder and an
echo parser, but nothing in the integration called either.

Bare Modbus RTU carries no transaction id -- frames correlate by ORDER alone.
These tests stub `CollectorSession._transact` directly rather than driving a
real socket (see `test_eybond_at_session.py` for that heavier harness): what
is under test here is `write_register`'s own parsing and its address-echo
guard, not the scheduler or the framing layer underneath it.
"""

import pytest

from custom_components.svitgrid.eybond_at.modbus_rtu import build_write_single
from custom_components.svitgrid.eybond_at.session import CollectorSession, TransactionFailed


def echo_frame(slave: int, address: int, value: int) -> bytes:
    """A conforming device echoes a write request verbatim, so the echo has
    the identical wire shape as the request that produced it."""
    return build_write_single(slave, address, value)


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

    async def fake_transact(payload: bytes, timeout_s: float) -> bytes:
        return queue.pop(0)

    session._transact = fake_transact
    return session


async def test_write_register_returns_the_echoed_value():
    session = make_session(responses=[echo_frame(1, 303, 0)])
    assert await session.write_register(303, 0) == 0


async def test_a_reply_for_a_different_address_is_a_failure():
    # Bare Modbus RTU carries no transaction id, so replies correlate by ORDER
    # only. A desync surfaces as a reply belonging to the PREVIOUS request; if
    # write_register accepted it, we would report a write to 303 that actually
    # landed on 324 -- a protective register.
    session = make_session(responses=[echo_frame(1, 324, 560)])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)


async def test_a_malformed_frame_raises_rather_than_returning_garbage():
    session = make_session(responses=[b"\x01\x06\xff"])
    with pytest.raises(TransactionFailed):
        await session.write_register(303, 0)
