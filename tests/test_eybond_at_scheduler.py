"""Interleaving scheduler for the collector's single serialized line.

The problem this module exists for, stated once:

The collector has **no transaction id**. A response is matched to its request
by ORDER and by nothing else. Meanwhile the vendor cloud is polling the same
collector through us, and we want to inject our own reads without corrupting
its session. So every frame in both directions has to pass through one place
that knows whose turn it is.

Measured 2026-08-20: request and response counts paired exactly -- 84/84
Modbus and 67/67 AT -- across 89 polling cycles. The line is strictly
serialized with no pipelining, in both protocols.
"""

import pytest

from custom_components.svitgrid.eybond_at.demux import Frame, FrameKind
from custom_components.svitgrid.eybond_at.scheduler import (
    ActionKind,
    SchedulerBusy,
    State,
    TxnScheduler,
)

# Captured frames. Note REQ_TYPE is a register-171 read -- the SAME bytes the
# vendor cloud sends. That is the point: attribution cannot come from content.
REQ_TYPE = bytes.fromhex("010300ab0001f5ea")
RESP_TYPE = bytes.fromhex("0103027803da45")
REQ_AC = bytes.fromhex("010300c9000fd5f0")
RESP_AC = bytes.fromhex("01031e000408ec1387000008fa00001387fffe000008fa00021386fffe002d0000e779")
AT_QUERY = bytes.fromhex("41542b485442543f0d0a")  # AT+HTBT?
AT_REPLY = bytes.fromhex("41542b485442543a0d0a")  # AT+HTBT:


def modbus(raw: bytes) -> Frame:
    return Frame(kind=FrameKind.MODBUS, raw=raw)


def at(raw: bytes) -> Frame:
    return Frame(kind=FrameKind.AT, raw=raw)


def kinds(actions):
    return [a.kind for a in actions]


class TestCloudPassthrough:
    def test_relays_a_cloud_request_to_the_collector(self):
        s = TxnScheduler()
        actions = s.on_cloud_frame(modbus(REQ_AC), now_ms=0)
        assert kinds(actions) == [ActionKind.SEND_TO_COLLECTOR]
        assert actions[0].payload == REQ_AC
        assert s.state is State.CLOUD_PENDING

    def test_relays_the_response_back_to_the_cloud(self):
        s = TxnScheduler()
        s.on_cloud_frame(modbus(REQ_AC), now_ms=0)
        actions = s.on_collector_frame(modbus(RESP_AC), now_ms=100)
        assert kinds(actions) == [ActionKind.SEND_TO_CLOUD]
        assert actions[0].payload == RESP_AC
        assert s.state is State.IDLE

    def test_an_at_transaction_also_occupies_the_line(self):
        # AT is request/response too -- 67/67 pairs measured. Treating it as
        # fire-and-forget would let us inject on top of an outstanding query.
        s = TxnScheduler()
        s.on_cloud_frame(at(AT_QUERY), now_ms=0)
        assert s.state is State.CLOUD_PENDING
        actions = s.on_collector_frame(at(AT_REPLY), now_ms=50)
        assert kinds(actions) == [ActionKind.SEND_TO_CLOUD]
        assert s.state is State.IDLE


class TestOwnRequests:
    def test_sends_our_request_when_the_line_is_idle(self):
        s = TxnScheduler()
        actions = s.request(REQ_TYPE, now_ms=0)
        assert kinds(actions) == [ActionKind.SEND_TO_COLLECTOR]
        assert s.state is State.OURS_PENDING

    def test_resolves_our_request_and_does_not_leak_it_to_the_cloud(self):
        # The cloud never asked for this, so relaying the answer would inject a
        # response into its session that it cannot attribute.
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        actions = s.on_collector_frame(modbus(RESP_TYPE), now_ms=200)
        assert kinds(actions) == [ActionKind.RESOLVE_OURS]
        assert actions[0].payload == RESP_TYPE
        assert s.state is State.IDLE

    def test_holds_our_request_while_a_cloud_transaction_is_open(self):
        s = TxnScheduler()
        s.on_cloud_frame(modbus(REQ_AC), now_ms=0)
        actions = s.request(REQ_TYPE, now_ms=10)
        assert actions == []  # nothing on the wire yet
        assert s.state is State.CLOUD_PENDING

    def test_sends_our_held_request_once_the_line_frees(self):
        s = TxnScheduler()
        s.on_cloud_frame(modbus(REQ_AC), now_ms=0)
        s.request(REQ_TYPE, now_ms=10)
        actions = s.on_collector_frame(modbus(RESP_AC), now_ms=100)
        assert kinds(actions) == [ActionKind.SEND_TO_CLOUD, ActionKind.SEND_TO_COLLECTOR]
        assert actions[1].payload == REQ_TYPE
        assert s.state is State.OURS_PENDING

    def test_refuses_a_second_concurrent_own_request(self):
        # We never pipeline. Two of ours in flight would be unattributable.
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        with pytest.raises(SchedulerBusy):
            s.request(REQ_AC, now_ms=1)


class TestAttribution:
    def test_identical_bytes_are_attributed_by_turn_not_content(self):
        """The heart of it.

        The cloud and we both read register 171, so the request bytes are
        byte-identical and so are the response bytes. Only whose turn it is
        can tell them apart.
        """
        s = TxnScheduler()
        # Cloud's turn.
        s.on_cloud_frame(modbus(REQ_TYPE), now_ms=0)
        first = s.on_collector_frame(modbus(RESP_TYPE), now_ms=100)
        assert kinds(first) == [ActionKind.SEND_TO_CLOUD]
        # Now ours, same bytes.
        s.request(REQ_TYPE, now_ms=200)
        second = s.on_collector_frame(modbus(RESP_TYPE), now_ms=300)
        assert kinds(second) == [ActionKind.RESOLVE_OURS]

    def test_queues_a_cloud_request_that_arrives_while_ours_is_open(self):
        # Forwarding it immediately would put two requests on a line that can
        # only answer one, and the two responses would be indistinguishable.
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        actions = s.on_cloud_frame(modbus(REQ_AC), now_ms=10)
        assert actions == []
        assert s.state is State.OURS_PENDING

    def test_drains_the_queued_cloud_request_after_ours_resolves(self):
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        s.on_cloud_frame(modbus(REQ_AC), now_ms=10)
        actions = s.on_collector_frame(modbus(RESP_TYPE), now_ms=100)
        assert kinds(actions) == [ActionKind.RESOLVE_OURS, ActionKind.SEND_TO_COLLECTOR]
        assert actions[1].payload == REQ_AC
        assert s.state is State.CLOUD_PENDING

    def test_preserves_cloud_order_when_several_queue_up(self):
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        s.on_cloud_frame(modbus(REQ_AC), now_ms=10)
        s.on_cloud_frame(at(AT_QUERY), now_ms=20)
        actions = s.on_collector_frame(modbus(RESP_TYPE), now_ms=100)
        # Only the FIRST queued frame goes out; the line takes one at a time.
        assert kinds(actions) == [ActionKind.RESOLVE_OURS, ActionKind.SEND_TO_COLLECTOR]
        assert actions[0].payload == RESP_TYPE
        assert actions[1].payload == REQ_AC
        follow = s.on_collector_frame(modbus(RESP_AC), now_ms=200)
        assert kinds(follow) == [ActionKind.SEND_TO_CLOUD, ActionKind.SEND_TO_COLLECTOR]
        assert follow[1].payload == AT_QUERY


class TestDesync:
    def test_an_unsolicited_response_drops_the_connection(self):
        """No transaction id means no way to resynchronise by matching one.

        A response nobody asked for proves the stream is desynchronised, and
        every later attribution would be a guess.
        """
        s = TxnScheduler()
        actions = s.on_collector_frame(modbus(RESP_AC), now_ms=0)
        assert kinds(actions) == [ActionKind.DROP_COLLECTOR]
        assert s.state is State.DESYNCED

    def test_a_timeout_drops_the_connection(self):
        s = TxnScheduler(txn_timeout_ms=3000)
        s.on_cloud_frame(modbus(REQ_AC), now_ms=0)
        assert s.on_tick(now_ms=2999) == []
        actions = s.on_tick(now_ms=3001)
        assert ActionKind.DROP_COLLECTOR in kinds(actions)
        assert s.state is State.DESYNCED

    def test_a_timeout_on_our_request_also_fails_the_waiter(self):
        # Otherwise the poller waits forever on a future nothing will resolve.
        s = TxnScheduler(txn_timeout_ms=3000)
        s.request(REQ_TYPE, now_ms=0)
        actions = s.on_tick(now_ms=3001)
        assert kinds(actions) == [ActionKind.FAIL_OURS, ActionKind.DROP_COLLECTOR]

    def test_refuses_to_send_anything_once_desynced(self):
        s = TxnScheduler(txn_timeout_ms=3000)
        s.request(REQ_TYPE, now_ms=0)
        s.on_tick(now_ms=3001)
        assert s.on_cloud_frame(modbus(REQ_AC), now_ms=4000) == []
        with pytest.raises(SchedulerBusy):
            s.request(REQ_AC, now_ms=4000)

    def test_an_overlong_cloud_queue_drops_rather_than_discarding_frames(self):
        # Silently dropping a vendor frame breaks the customer's SmartESS
        # session in a way nobody can diagnose. Fail loudly instead.
        s = TxnScheduler(max_queued=4)
        s.request(REQ_TYPE, now_ms=0)
        for i in range(4):
            assert s.on_cloud_frame(modbus(REQ_AC), now_ms=10 + i) == []
        actions = s.on_cloud_frame(modbus(REQ_AC), now_ms=99)
        assert kinds(actions) == [ActionKind.DROP_COLLECTOR]


class TestReset:
    def test_reset_clears_state_for_a_fresh_connection(self):
        s = TxnScheduler()
        s.request(REQ_TYPE, now_ms=0)
        s.on_cloud_frame(modbus(REQ_AC), now_ms=10)
        actions = s.reset()
        # A pending own request must fail, not hang, when the socket goes away.
        assert kinds(actions) == [ActionKind.FAIL_OURS]
        assert s.state is State.IDLE
        assert s.on_cloud_frame(modbus(REQ_AC), now_ms=20)[0].kind is (ActionKind.SEND_TO_COLLECTOR)
