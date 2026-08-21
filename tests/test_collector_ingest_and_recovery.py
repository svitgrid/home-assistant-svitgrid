"""Three defects found while a real collector was onboarding, 2026-08-21.

1. The collector path never recorded an ingest, so Home Assistant reported
   "Ingests (24h): 0 / Last ingest: Unknown / Status: idle" for ever while
   readings reached the cloud perfectly well. Only `readings_publisher` (the
   entity-relay path) called `record_ingest_success`.

2. A first poll that failed slept the FULL 300 s cadence before retrying, so
   one transient timeout put five minutes on the "waiting for data" screen a
   new user is staring at.

3. On a NAT'd container every collector appears to connect from the Docker
   gateway (192.168.65.1 -- measured, 11 of 11 connections). That address was
   stored as "known", so a re-announce for a missing collector was unicast to
   the gateway and reached nothing, while the subnet sweep switched itself off
   because a collector had once been seen.
"""

from __future__ import annotations

import asyncio

from custom_components.svitgrid.eybond_at.harvest import first_poll_retry_s
from custom_components.svitgrid.eybond_at.hub import HubConfig


class TestFirstPollRetriesQuickly:
    def test_a_failed_first_poll_retries_in_seconds_not_minutes(self):
        # The reading a user waits for on the onboarding screen.
        assert first_poll_retry_s(attempt=1, cadence_s=300) <= 10

    def test_it_backs_off_rather_than_hammering_a_sick_collector(self):
        a, b, c = (first_poll_retry_s(attempt=n, cadence_s=300) for n in (1, 2, 3))
        assert a < b < c

    def test_it_never_exceeds_the_normal_cadence(self):
        # Backoff must converge on the steady-state interval, not past it.
        for n in range(1, 12):
            assert first_poll_retry_s(attempt=n, cadence_s=300) <= 300

    def test_a_short_cadence_is_never_made_slower(self):
        # An island install polling every 30 s must not be pushed to 60.
        for n in range(1, 6):
            assert first_poll_retry_s(attempt=n, cadence_s=30) <= 30


class TestReAnnounceReachesTheCollector:
    """A known address that cannot be dialled must not disable the sweep."""

    def make(self, **kw):
        return HubConfig(
            advertised_ip="192.168.1.34",
            announce_target="192.168.1.5,192.168.1.116",
            expected_collectors=1,
            **kw,
        )

    def test_a_nat_gateway_address_is_not_treated_as_dialable(self):
        from custom_components.svitgrid.eybond_at.hub import is_dialable_peer

        # Docker Desktop shows every collector as the gateway. Announcing
        # there is announcing into the NAT.
        assert is_dialable_peer("192.168.65.1", advertised_ip="192.168.1.34") is False

    def test_an_address_on_our_own_subnet_is_dialable(self):
        from custom_components.svitgrid.eybond_at.hub import is_dialable_peer

        assert is_dialable_peer("192.168.1.116", advertised_ip="192.168.1.34") is True

    def test_unknown_advertised_ip_trusts_the_peer(self):
        from custom_components.svitgrid.eybond_at.hub import is_dialable_peer

        # Host networking: no advertised_ip override, peers are real.
        assert is_dialable_peer("192.168.1.116", advertised_ip=None) is True


class TestIngestActivityOnTheCollectorPath:
    async def test_a_successful_drain_records_an_ingest(self):
        from custom_components.svitgrid.reading_sender import record_drain_activity

        class Activity:
            def __init__(self):
                self.ok = 0
                self.fail = []

            def record_ingest_success(self, **kw):
                self.ok += 1

            def record_ingest_failure(self, *, reason):
                self.fail.append(reason)

        a = Activity()
        record_drain_activity(a, sent_count=3, error=None)
        assert a.ok == 1, "HA must stop reporting 0 ingests while data flows"
        assert a.fail == []

    async def test_a_failed_drain_records_the_reason(self):
        from custom_components.svitgrid.reading_sender import record_drain_activity

        class Activity:
            def __init__(self):
                self.ok = 0
                self.fail = []

            def record_ingest_success(self, **kw):
                self.ok += 1

            def record_ingest_failure(self, *, reason):
                self.fail.append(reason)

        a = Activity()
        record_drain_activity(a, sent_count=0, error=RuntimeError("boom"))
        assert a.ok == 0
        assert a.fail and "boom" in a.fail[0]

    async def test_an_empty_drain_records_nothing(self):
        # Nothing to send is not an ingest, and not a failure either.
        from custom_components.svitgrid.reading_sender import record_drain_activity

        class Activity:
            def __init__(self):
                self.ok = 0
                self.fail = []

            def record_ingest_success(self, **kw):
                self.ok += 1

            def record_ingest_failure(self, *, reason):
                self.fail.append(reason)

        a = Activity()
        record_drain_activity(a, sent_count=0, error=None)
        assert a.ok == 0 and a.fail == []

    async def test_no_activity_tracker_is_not_an_error(self):
        from custom_components.svitgrid.reading_sender import record_drain_activity

        record_drain_activity(None, sent_count=5, error=None)


# ── wiring: a helper nothing calls is worth nothing ───────────────────────

class TestTheLoopActuallyRetriesFast:
    async def test_a_failed_first_poll_is_retried_within_seconds(self):
        """Drive the real loop and record what it sleeps.

        Guards the wiring, not the arithmetic: `first_poll_retry_s` passing its
        own unit tests means nothing if the loop still sleeps the cadence.
        """
        from custom_components.svitgrid.eybond_at.harvest import run_eybond_harvest_loop
        from custom_components.svitgrid.eybond_at.session import TransactionFailed
        from tests.test_eybond_at_harvest import (
            SERIAL,
            FakeCadence,
            FakeHass,
            FakeHub,
            FakeReader,
            FakeSession,
        )

        slept: list[float] = []
        hass = FakeHass()
        reader = FakeReader(error=TransactionFailed("transaction timeout"))

        class Store:
            appended: list = []
            def append(self, *a, **k):
                pass

        async def sleeper(s):
            slept.append(s)
            if len(slept) >= 3:
                hass.is_stopping = True
            await asyncio.sleep(0)

        await run_eybond_harvest_loop(
            hass=hass,
            hub=FakeHub({SERIAL: FakeSession()}),
            inverter_serial=SERIAL,
            reader_factory=lambda _s: reader,
            store=Store(),
            # The REAL cadence. The fake's 0.01 s default made this
            # assertion vacuous: any behaviour passed.
            cadence=FakeCadence(interval_s=300),
            inverter_id="inv1",
            sleep=sleeper,
        )
        assert slept, "the loop never slept"
        assert slept[0] <= 10, (
            f"first retry waited {slept[0]}s — a failed first poll must not "
            "cost a full cadence on the onboarding screen"
        )


class TestTheHubKeepsLookingWhenThePeerIsUndialable:
    async def test_a_gateway_peer_does_not_switch_the_sweep_off(self):
        """The measured case: every connection appears to come from the NAT."""
        from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig

        hub = EybondAtHub(
            HubConfig(
                listen_port=0,
                advertised_ip="192.168.1.34",
                announce_target="192.168.1.116",
                expected_collectors=1,
                announce_interval_s=99,
            )
        )
        # Pretend a collector dialled in from the Docker gateway.
        hub._known_addresses.add("192.168.65.1")
        sent: list = []
        class UDP:
            def sendto(self, data, addr):
                sent.append(addr[0])
        hub._udp = UDP()
        hub._actual_port = 8899
        hub._send_announce()
        assert "192.168.1.116" in sent, (
            "the configured target must still be announced to: the only known "
            "address is a NAT gateway and cannot be dialled"
        )
        assert "192.168.65.1" not in sent, "announcing into the NAT reaches nothing"

