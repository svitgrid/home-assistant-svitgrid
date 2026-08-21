"""Two defects an adversarial audit found on 2026-08-21.

1. The fast first-poll retry only fired on `except TransactionFailed`, but
   `EybondAtReader.read()` catches that per block and returns an INCOMPLETE
   reading instead of raising. `poll_once` then gates it and returns None, and
   the loop sleeps the full cadence. Protocol 11's read plan is a SINGLE block,
   so any one timeout empties the whole reading -- meaning the common failure
   was exactly the one the retry did not cover. Measured on the bench: bound
   17:08:36, gated, next attempt 17:13:45.

2. The announce silence gate compared `expected_collectors` against
   `len(self._sessions)` -- raw TCP sessions, identified or not, configured or
   not. A second, UNPAIRED Anenji on the LAN that dials in first therefore
   silences the announce for the paired one, permanently: the unpaired session
   is kept alive by the heartbeat, so the slot never frees.
"""

from __future__ import annotations

import asyncio

from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig


class _UDP:
    def __init__(self):
        self.sent: list[str] = []

    def sendto(self, data, addr):
        self.sent.append(addr[0])


def _hub(**kw) -> tuple[EybondAtHub, _UDP]:
    hub = EybondAtHub(
        HubConfig(
            listen_port=0,
            advertised_ip="192.168.1.34",
            announce_target="192.168.1.116",
            announce_interval_s=99,
            **kw,
        )
    )
    udp = _UDP()
    hub._udp = udp
    hub._actual_port = 8899
    return hub, udp


class _Session:
    def __init__(self, serial, address="192.168.1.116"):
        self.serial = serial
        self.address = address


class TestAnnounceGateCountsTheRightCollectors:
    def test_an_unpaired_collector_does_not_silence_the_announce(self):
        hub, udp = _hub(expected_collectors=1, expected_serials=("PAIRED-1",))
        hub._sessions["k"] = _Session("SOMEONE-ELSE")
        hub._send_announce()
        assert udp.sent, (
            "an unpaired sibling collector occupied the slot and the paired one "
            "was never called back"
        )

    def test_the_configured_collector_being_present_does_silence_it(self):
        # The measured reason the gate exists: announcing at a CONNECTED
        # collector makes it redial (18 reconnects in 45 s).
        hub, udp = _hub(expected_collectors=1, expected_serials=("PAIRED-1",))
        hub._sessions["k"] = _Session("PAIRED-1")
        hub._send_announce()
        assert udp.sent == []

    def test_a_session_with_no_serial_yet_does_not_count(self):
        # Mid-identification. It may turn out to be someone else's.
        hub, udp = _hub(expected_collectors=1, expected_serials=("PAIRED-1",))
        hub._sessions["k"] = _Session(None)
        hub._send_announce()
        assert udp.sent

    def test_without_configured_serials_the_old_count_still_applies(self):
        # Discovery runs with expected_collectors=0 and no serials; and an
        # older entry may carry no serial at all. Must not regress those.
        hub, udp = _hub(expected_collectors=1)
        hub._sessions["k"] = _Session("ANYTHING")
        hub._send_announce()
        assert udp.sent == []


class TestGatedFirstReadingRetriesFast:
    async def _run(self, *, complete: bool, cadence_s: float = 300):
        from dataclasses import replace

        from custom_components.svitgrid.eybond_at.harvest import run_eybond_harvest_loop
        from tests.test_eybond_at_harvest import (
            SERIAL,
            FakeCadence,
            FakeHass,
            FakeHub,
            FakeSession,
            make_reading,
        )

        hass = FakeHass()
        slept: list[float] = []

        class Reader:
            calls = 0

            def invalidate(self):
                pass

            async def read(self):
                Reader.calls += 1
                if complete:
                    return make_reading()
                # An INCOMPLETE reading: exactly what a per-block timeout
                # produces, because reader.read() catches it and records the
                # block instead of raising.
                return replace(make_reading(), values={}, missing_blocks=((201, 29),))

        class Store:
            async def append(self, *a, **k):
                pass

        async def sleeper(s):
            slept.append(s)
            if len(slept) >= 2:
                hass.is_stopping = True
            await asyncio.sleep(0)

        await run_eybond_harvest_loop(
            hass=hass,
            hub=FakeHub({SERIAL: FakeSession()}),
            inverter_serial=SERIAL,
            reader_factory=lambda _s: Reader(),
            store=Store(),
            cadence=FakeCadence(interval_s=cadence_s),
            inverter_id="inv1",
            sleep=sleeper,
        )
        return slept

    async def test_a_gated_first_reading_is_retried_in_seconds(self):
        slept = await self._run(complete=False)
        assert slept, "the loop never slept"
        assert slept[0] <= 10, (
            f"waited {slept[0]}s after an incomplete first reading — this is the "
            "common failure, and it must not cost a full cadence"
        )

    async def test_it_backs_off_rather_than_hammering(self):
        slept = await self._run(complete=False)
        assert len(slept) >= 2 and slept[1] > slept[0]
