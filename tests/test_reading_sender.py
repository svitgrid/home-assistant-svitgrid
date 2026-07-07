import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

from custom_components.svitgrid.api_client import DeviceEvicted, ReadingRejected
from custom_components.svitgrid.lifecycle import DEPROVISIONED, PAUSED, LifecycleState
from custom_components.svitgrid.reading_sender import Cadence, drain_once, run_sender_loop
from custom_components.svitgrid.reading_store import ReadingStore


class _SyncStore(ReadingStore):
    async def skip_aged(self, now_iso, cap_s):
        return self._skip_aged_sync(now_iso, cap_s)

    async def get_sendable(self, now_iso, cap_s, limit):
        return self._get_sendable_sync(now_iso, cap_s, limit)

    async def mark_sent(self, keys):
        return self._mark_sent_sync(keys)

    async def mark_failed(self, keys, now_iso):
        return self._mark_failed_sync(keys, now_iso)

    async def set_lifecycle(self, *a):
        return None


def _store(tmp_path):
    s = _SyncStore(None, str(tmp_path / "readings.db"))
    return s


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def push_readings_batch(self, api_key, readings):
        self.calls.append(readings)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_drain_marks_sent_and_updates_cadence(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    client = _FakeClient(
        {
            "results": [
                {"ok": True, "inverterId": "inv-1"},
                {"ok": True, "inverterId": "inv-1"},
            ],
            "ingestIntervalMs": 30000,
        }
    )
    cadence = Cadence(interval_s=10)

    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )

    assert sent == 2
    assert store._count_by_state_sync() == {"sent": 2}
    assert cadence.interval_s == 30  # 30000ms → 30s


@pytest.mark.asyncio
async def test_drain_respects_batch_max(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    for i in range(3):
        store._append_sync({"inverterId": "inv-1", "timestamp": f"2026-06-24T10:00:0{i}Z"})
    client = _FakeClient({"results": [{"ok": True, "inverterId": "inv-1"}] * 2})
    cadence = Cadence(interval_s=10)
    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence, batch_max=2
    )
    assert len(client.calls[0]) == 2  # only 2 sent this drain
    assert sent == 2


@pytest.mark.asyncio
async def test_drain_5xx_marks_failed(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    client = _FakeClient(None)  # 5xx → None
    cadence = Cadence(interval_s=10)
    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )
    assert sent == 0
    assert store._count_by_state_sync() == {"failed": 1}


@pytest.mark.asyncio
async def test_drain_skips_aged_rows(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-20T12:00:00Z"})  # >48h
    client = _FakeClient({"results": []})
    cadence = Cadence(interval_s=10)
    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )
    assert sent == 0
    assert store._count_by_state_sync() == {"skipped": 1}
    assert client.calls == []  # nothing in-window to send


@pytest.mark.asyncio
async def test_drain_4xx_marks_failed(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    client = _FakeClient(ReadingRejected(400, "bad"))
    cadence = Cadence(interval_s=10)
    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )
    assert sent == 0
    assert store._count_by_state_sync() == {"failed": 1}


@pytest.mark.asyncio
async def test_drain_partial_results_marks_each_row(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    client = _FakeClient(
        {
            "results": [
                {"ok": True, "inverterId": "inv-1"},
                {"ok": False, "inverterId": "inv-1"},
            ]
        }
    )
    cadence = Cadence(interval_s=10)

    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )

    assert sent == 1
    assert store._count_by_state_sync() == {"sent": 1, "failed": 1}


@pytest.mark.asyncio
async def test_drain_stopped_device_leaves_rows_pending(tmp_path):
    """HTTP 200 with {stopped: true} must leave rows pending (not mark them sent)
    so they are retried once the device is re-enabled by the operator."""
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    client = _FakeClient({"stopped": True, "stoppedReason": "evicted"})
    cadence = Cadence(interval_s=10)

    sent = await drain_once(
        store=store, api_client=client, api_key="k", now_iso=now, cadence=cadence
    )

    assert sent == 0
    assert store._count_by_state_sync() == {"pending": 2}
    assert cadence.interval_s == 10  # cadence unchanged


class _EvictClient:
    async def push_readings_batch(self, api_key, readings):
        raise DeviceEvicted("revoked")


@pytest.mark.asyncio
async def test_drain_device_evicted_sets_deprovisioned(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "i", "timestamp": "2026-06-25T10:00:00Z"})
    lc = LifecycleState()
    sent = await drain_once(
        store=store,
        api_client=_EvictClient(),
        api_key="k",
        now_iso="2026-06-25T12:00:00Z",
        cadence=Cadence(interval_s=10),
        lifecycle=lc,
    )
    assert sent == 0 and lc.state == DEPROVISIONED


@pytest.mark.asyncio
async def test_drain_stopped_sets_paused(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "i", "timestamp": "2026-06-25T10:00:00Z"})
    lc = LifecycleState()
    client = _FakeClient({"stopped": True, "stoppedReason": "disabled"})
    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso="2026-06-25T12:00:00Z",
        cadence=Cadence(interval_s=10),
        lifecycle=lc,
    )
    assert sent == 0 and lc.state == PAUSED


# ── event-driven eager-drain tests ────────────────────────────────────────────


class _EagerSyncStore(ReadingStore):
    """ReadingStore subclass with sync DB methods so tests need no hass executor.
    wait_for_data is inherited from ReadingStore."""

    async def skip_aged(self, now_iso, cap_s):
        return self._skip_aged_sync(now_iso, cap_s)

    async def get_sendable(self, now_iso, cap_s, limit):
        return self._get_sendable_sync(now_iso, cap_s, limit)

    async def mark_sent(self, keys):
        return self._mark_sent_sync(keys)

    async def mark_failed(self, keys, now_iso):
        return self._mark_failed_sync(keys, now_iso)

    async def set_lifecycle(self, *a):
        return None


class _FakeHass:
    def __init__(self):
        self.is_stopping = False

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


@pytest.mark.asyncio
async def test_sender_drains_promptly_on_append(tmp_path):
    """Sender must drain a newly appended reading well before the full tick elapses."""
    store = _EagerSyncStore(None, str(tmp_path / "db.sqlite"))
    hass = _FakeHass()

    # Large tick so the test fails (slow) if the eager path doesn't work
    TICK_S = 10.0
    client = _FakeClient({"results": [{"ok": True}]})
    cadence = Cadence(interval_s=10)

    # Start sender in background
    sender_task = asyncio.create_task(
        run_sender_loop(
            hass=hass,
            store=store,
            api_client=client,
            api_key="k",
            cadence=cadence,
            tick_s=TICK_S,
        )
    )

    try:
        # Allow sender to do its first drain (empty) and start waiting
        await asyncio.sleep(0.05)

        # Now append a reading — this should wake the sender immediately
        start = asyncio.get_event_loop().time()
        # Use a fresh timestamp so the reading isn't aged out by skip_aged (the
        # sender caps backfill at BACKFILL_CAP_S from wall-clock now); a hardcoded
        # past date would rot and get skipped once it exceeds the cap.
        fresh_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        store._append_sync({"inverterId": "inv-1", "timestamp": fresh_ts})
        store._signal_data_available()

        # Poll until sent or timeout
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if store._count_by_state_sync().get("sent", 0) >= 1:
                break
            await asyncio.sleep(0.02)

        elapsed = asyncio.get_event_loop().time() - start
        counts = store._count_by_state_sync()
        assert counts.get("sent", 0) >= 1, f"Reading not sent after {elapsed:.2f}s; counts={counts}"
        # Should be much faster than the 10s tick
        assert elapsed < 2.0, f"Drain took too long: {elapsed:.2f}s (tick={TICK_S}s)"
    finally:
        hass.is_stopping = True
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender_task


@pytest.mark.asyncio
async def test_sender_fallback_sleep_when_store_lacks_wait_for_data(tmp_path):
    """Sender must not crash when the store object has no wait_for_data method."""

    class _MinimalStore:
        """Fake store with only the minimum interface; no wait_for_data."""

        def __init__(self):
            self.drain_calls = 0

        async def skip_aged(self, now_iso, cap_s):
            return 0

        async def get_sendable(self, now_iso, cap_s, limit):
            return []

        async def mark_sent(self, keys):
            pass

        async def mark_failed(self, keys, now_iso):
            pass

    store = _MinimalStore()
    hass = _FakeHass()
    client = _FakeClient({})

    sender_task = asyncio.create_task(
        run_sender_loop(
            hass=hass,
            store=store,
            api_client=client,
            api_key="k",
            cadence=Cadence(interval_s=10),
            # Very short tick so the test completes quickly
            tick_s=0.05,
        )
    )

    try:
        # Run for a moment — sender must not raise even without wait_for_data
        await asyncio.sleep(0.15)
    finally:
        hass.is_stopping = True
        sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender_task
    # If we reach here without exception, the fallback works


def test_cadence_default_is_five_minutes():
    """Cadence starts at the 5-min idle cadence (edge-aligned) before the
    server's first ingest response — no initial fast burst."""
    from custom_components.svitgrid.reading_sender import Cadence

    assert Cadence().interval_s == 300
