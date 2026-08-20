"""Harvest loop: poll the collector and feed the existing reading pipeline.

The loop's wait is injected rather than slept for real. `_clamp_interval` has
a 5 s floor -- correct in production, far too slow for a test -- so the tests
override the WAIT while leaving the clamp itself exercised.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from custom_components.svitgrid.eybond_at.harvest import (
    poll_once,
    run_eybond_harvest_loop,
)
from custom_components.svitgrid.eybond_at.identity import (
    DeviceIdentity,
    UnknownPlatform,
)
from custom_components.svitgrid.eybond_at.reader import Reading
from custom_components.svitgrid.eybond_at.register_map import (
    SMG_II_PROTOCOL_11,
    Confidence,
)
from custom_components.svitgrid.eybond_at.session import TransactionFailed


async def fast_sleep(_seconds: float) -> None:
    """Drive the loop fast. See the module docstring for why this is injected."""
    await asyncio.sleep(0.001)


SERIAL = "99432604107106"

IDENTITY = DeviceIdentity(
    protocol_number=11,
    device_type=0x7803,
    serial="99432604107106",
    firmware="7803_A6260126v1",
)

# The four fields CORE_PAYLOAD_FIELDS requires, plus the AC values our bench
# unit actually produced.
FULL_VALUES = {
    "gridVoltageL1": 228.4,
    "gridFrequency": 49.99,
    "gridPower": 0,
    "loadVoltageL1": 229.8,
    "loadPower": -2,
    "batteryVoltage": 0.0,
    "batteryPower": 0,
    "batterySoc": 5,
    "pv1Power": 0,
    "inverterTemperature": 27,
}


def make_reading(values=None, missing=()):
    values = FULL_VALUES if values is None else values
    return Reading(
        identity=IDENTITY,
        register_map=SMG_II_PROTOCOL_11,
        values=dict(values),
        confidence=dict.fromkeys(values, Confidence.IDENTIFIED),
        missing_blocks=tuple(missing),
    )


class FakeStore:
    def __init__(self):
        self.appended = []

    async def append(self, payload):
        self.appended.append(payload)


@dataclass
class FakeReader:
    readings: list = field(default_factory=list)
    error: Exception | None = None
    invalidated: int = 0
    calls: int = 0

    def invalidate(self):
        self.invalidated += 1

    async def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.readings.pop(0) if self.readings else make_reading()


@dataclass
class FakeSession:
    address: str = "192.168.1.116"


@dataclass
class FakeHub:
    """Routes by serial, like the real hub."""

    connected: dict = field(default_factory=dict)
    waits: list = field(default_factory=list)
    changed: object = None

    def session_for(self, serial):
        return self.connected.get(serial)

    async def wait_for_change(self, limit_s):
        self.waits.append(limit_s)
        if self.changed is not None:
            try:
                await asyncio.wait_for(self.changed.wait(), limit_s)
                return True
            except TimeoutError:
                return False
        await asyncio.sleep(0.001)
        return False


@dataclass
class FakeHass:
    is_stopping: bool = False


@dataclass
class FakeCadence:
    interval_s: float = 0.01


@dataclass
class FakeLifecycle:
    active: bool = True


class TestPollOnce:
    async def test_appends_a_complete_reading(self):
        store = FakeStore()
        payload = await poll_once(reader=FakeReader(), inverter_id="inv1", store=store)
        assert payload is not None
        assert store.appended == [payload]
        assert payload["inverterId"] == "inv1"
        assert payload["gridVoltageL1"] == 228.4

    async def test_the_payload_carries_the_pipeline_identity_fields(self):
        payload = await poll_once(reader=FakeReader(), inverter_id="inv1", store=FakeStore())
        assert "timestamp" in payload
        assert payload["source"] == "edge"

    async def test_gates_a_reading_missing_core_fields(self):
        """A half-reading must never reach the API.

        An unreadable block yields no values at all here, because the whole
        map is one block -- so the gate is what stands between a transport
        failure and a reading that looks real.
        """
        store = FakeStore()
        reader = FakeReader(readings=[make_reading(values={}, missing=((201, 29),))])
        payload = await poll_once(reader=reader, inverter_id="inv1", store=store)
        assert payload is None
        assert store.appended == []


class TestLoop:
    async def test_polls_and_appends_then_stops(self):
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=FakeHub({SERIAL: FakeSession()}),
                inverter_serial=SERIAL,
                reader_factory=lambda _session: reader,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
            ),
            stop_after_a_moment(),
        )
        assert store.appended

    async def test_skips_ticks_while_no_collector_is_connected(self):
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=FakeHub({}),
                inverter_serial=SERIAL,
                reader_factory=lambda _session: reader,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
            ),
            stop_after_a_moment(),
        )
        assert reader.calls == 0
        assert store.appended == []

    async def test_rebinds_when_the_collector_reconnects(self):
        """A new session may be a different inverter.

        Reusing the reader across a reconnect would decode the new device with
        the previous one's register map, so a changed session gets a fresh
        reader.
        """
        hass, store = FakeHass(), FakeStore()
        hub = FakeHub({})
        made = []

        def factory(session):
            made.append(session)
            return FakeReader()

        async def flap():
            await asyncio.sleep(0.02)
            hub.connected[SERIAL] = FakeSession()
            await asyncio.sleep(0.03)
            hub.connected.clear()
            await asyncio.sleep(0.02)
            hub.connected[SERIAL] = FakeSession()  # a DIFFERENT session object
            await asyncio.sleep(0.03)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=hub,
                inverter_serial=SERIAL,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
                reader_factory=factory,
            ),
            flap(),
        )
        assert len(made) >= 2  # a fresh reader per session

    async def test_an_unknown_platform_does_not_stop_the_loop(self):
        hass, store = FakeHass(), FakeStore()
        reader = FakeReader(error=UnknownPlatform("unrecognised protocol number 4"))

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=FakeHub({SERIAL: FakeSession()}),
                inverter_serial=SERIAL,
                reader_factory=lambda _session: reader,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
            ),
            stop_after_a_moment(),
        )
        assert reader.calls > 1  # kept polling
        assert store.appended == []  # and published nothing

    async def test_an_unknown_platform_is_logged_once_not_every_tick(self, caplog):
        # It cannot resolve without a capture or a different device, so
        # repeating it every tick would bury the rest of the log.
        hass, store = FakeHass(), FakeStore()
        reader = FakeReader(error=UnknownPlatform("unrecognised protocol number 4"))

        async def stop_after_a_moment():
            await asyncio.sleep(0.06)
            hass.is_stopping = True

        with caplog.at_level("ERROR"):
            await asyncio.gather(
                run_eybond_harvest_loop(
                    hass=hass,
                    hub=FakeHub({SERIAL: FakeSession()}),
                    inverter_serial=SERIAL,
                    reader_factory=lambda _session: reader,
                    store=store,
                    cadence=FakeCadence(),
                    inverter_id="inv1",
                    sleep=fast_sleep,
                ),
                stop_after_a_moment(),
            )
        refusals = [r for r in caplog.records if "refusing to publish" in r.message]
        assert reader.calls > 1
        assert len(refusals) == 1

    async def test_a_transport_failure_does_not_stop_the_loop(self):
        hass, store = FakeHass(), FakeStore()
        reader = FakeReader(error=TransactionFailed("no collector connected"))

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=FakeHub({SERIAL: FakeSession()}),
                inverter_serial=SERIAL,
                reader_factory=lambda _session: reader,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
            ),
            stop_after_a_moment(),
        )
        assert reader.calls > 1

    async def test_lifecycle_deactivation_ends_the_loop(self):
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()
        lifecycle = FakeLifecycle()

        async def deactivate():
            await asyncio.sleep(0.03)
            lifecycle.active = False

        await asyncio.wait_for(
            asyncio.gather(
                run_eybond_harvest_loop(
                    hass=hass,
                    hub=FakeHub({SERIAL: FakeSession()}),
                    inverter_serial=SERIAL,
                    reader_factory=lambda _session: reader,
                    store=store,
                    cadence=FakeCadence(),
                    inverter_id="inv1",
                    lifecycle=lifecycle,
                    sleep=fast_sleep,
                ),
                deactivate(),
            ),
            timeout=2.0,
        )
        assert hass.is_stopping is False  # it was lifecycle, not shutdown


@pytest.mark.parametrize("field_name", ["batteryPower", "batteryVoltage", "gridPower", "loadPower"])
async def test_every_core_field_is_produced_by_the_map(field_name):
    """The map must satisfy CORE_PAYLOAD_FIELDS or nothing ever publishes.

    Losing one of these silently turns every reading into a gated no-op, and
    the only symptom is an inverter that never appears.
    """
    assert field_name in {spec.field for spec in SMG_II_PROTOCOL_11.fields}


class TestRouting:
    async def test_an_inverter_whose_serial_is_absent_publishes_nothing(self):
        """Normal for a switched-off inverter, and it must not be an error.

        Publishing whatever session happens to be connected would attribute
        one inverter's readings to another.
        """
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                # Another inverter IS connected -- just not this one.
                hub=FakeHub({"88888888888888": FakeSession()}),
                inverter_serial=SERIAL,
                store=store,
                cadence=FakeCadence(),
                inverter_id="inv1",
                sleep=fast_sleep,
                reader_factory=lambda _s: reader,
            ),
            stop_after_a_moment(),
        )
        assert reader.calls == 0
        assert store.appended == []


class TestColdStart:
    """A fresh install must not wait a full poll cadence for its first reading.

    Measured on real hardware 2026-08-20: the loop's first tick ran 0.6 s
    BEFORE the collector connected, so it correctly skipped -- and then slept
    the whole 300 s cadence while the app showed "Waiting for data". The pipe
    was working the entire time.
    """

    async def test_waits_for_the_collector_instead_of_sleeping_the_cadence(self):
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()
        hub = FakeHub()

        async def stop_after_a_moment():
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=hub,
                inverter_serial=SERIAL,
                store=store,
                cadence=FakeCadence(interval_s=300),
                inverter_id="inv1",
                sleep=fast_sleep,
                reader_factory=lambda _s: reader,
            ),
            stop_after_a_moment(),
        )
        assert hub.waits, "did not wait on the hub at all"
        # 30 s ceiling, never the 300 s cadence.
        assert max(hub.waits) <= 30

    async def test_reads_as_soon_as_the_collector_arrives(self):
        """The whole point: the first reading lands with onboarding.

        Waking on the event rather than the cadence is the difference between
        a reading in seconds and one five minutes later.
        """
        hass, store, reader = FakeHass(), FakeStore(), FakeReader()
        hub = FakeHub(changed=asyncio.Event())

        async def collector_arrives():
            await asyncio.sleep(0.03)
            hub.connected[SERIAL] = FakeSession()
            hub.changed.set()
            await asyncio.sleep(0.05)
            hass.is_stopping = True

        await asyncio.gather(
            run_eybond_harvest_loop(
                hass=hass,
                hub=hub,
                inverter_serial=SERIAL,
                store=store,
                cadence=FakeCadence(interval_s=300),
                inverter_id="inv1",
                sleep=fast_sleep,
                reader_factory=lambda _s: reader,
            ),
            collector_arrives(),
        )
        # Published despite a 300 s cadence, because it woke on the event.
        assert store.appended, "did not read after the collector arrived"
