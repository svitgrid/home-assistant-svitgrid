"""The collector path must record ingests, against the REAL tracker.

Shipped 2026-08-21 and broken on arrival: `record_drain_activity` called
`record_ingest_success(count=...)` while the method is
`(*, sample_count, period_sec, summary, unresolved=None)`. Every successful
drain raised TypeError, was swallowed by the sender's catch-all, logged
"sender drain failed", and recorded an ingest FAILURE — so the status sensor
read "error" precisely because uploads were working.

The three tests that covered it used fakes declaring `record_ingest_success(**kw)`,
which accepts anything. A fake with a permissive signature cannot catch a
signature mismatch. These tests use the real ActivityTracker.
"""

from __future__ import annotations

import asyncio

from custom_components.svitgrid.activity import ActivityTracker


class TestAgainstTheRealTracker:
    def test_the_real_tracker_rejects_the_shipped_call(self):
        # Pins the exact defect, so it cannot come back through a fake.
        import inspect

        sig = inspect.signature(ActivityTracker.record_ingest_success)
        try:
            sig.bind(None, count=1)
            raised = False
        except TypeError:
            raised = True
        assert raised, "if this ever binds, the guard below is meaningless"

    async def test_a_collector_reading_records_an_ingest_on_the_real_tracker(self):
        """The whole point: HA must stop reporting 0 ingests while data flows."""
        from custom_components.svitgrid.eybond_at.harvest import run_eybond_harvest_loop
        from tests.test_eybond_at_harvest import (
            SERIAL,
            FakeCadence,
            FakeHass,
            FakeHub,
            FakeReader,
            FakeSession,
        )

        activity = ActivityTracker()
        hass = FakeHass()

        class Store:
            def __init__(self):
                self.appended = []

            async def append(self, *a, **k):
                self.appended.append((a, k))

        slept = []

        async def sleeper(s):
            slept.append(s)
            hass.is_stopping = True
            await asyncio.sleep(0)

        await run_eybond_harvest_loop(
            hass=hass,
            hub=FakeHub({SERIAL: FakeSession()}),
            inverter_serial=SERIAL,
            reader_factory=lambda _s: FakeReader(),
            store=Store(),
            cadence=FakeCadence(interval_s=300),
            inverter_id="inv1",
            activity=activity,
            sleep=sleeper,
        )
        # A real ingest, recorded through the real signature.
        assert activity.ingest_count_24h == 1
        assert activity.last_ingest_status == "ok"
        assert "error" not in activity.diagnostics_line().lower()


class TestTheSenderNoLongerClobbers:
    def test_the_sender_does_not_record_ingests_at_all(self):
        """Two recorders for one event is how the richer one got overwritten.

        `harvest/engine.py` and `readings_publisher.py` already record their
        own ingests with real sample_count/period/summary. A second record from
        the shared sender stamped over them — and stamped "error" over paths
        that were working.
        """
        import inspect

        from custom_components.svitgrid import reading_sender

        src = inspect.getsource(reading_sender)
        assert "record_ingest_success" not in src, (
            "the sender must not record ingests; the path that produced the "
            "reading does, where it knows the sample count and period"
        )
        assert "record_ingest_failure" not in src
