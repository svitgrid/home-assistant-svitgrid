"""Retry backoff for the reading sender (0.17.1).

Prod finding (2026-07-22, svitgrid-prod logs): four installs were POSTing
`/api/v1/ingest/readings` every ~5s around the clock —

  - one with a dead API key, receiving 401 on every attempt (~17k POSTs/day),
  - three whose rows permanently fail per-item, re-sent forever with HTTP 200
    and zero server writes (~52k POSTs/day combined).

Root cause: `mark_failed` incremented `attempts` and stamped
`last_attempt_at`, but nothing ever READ them — a 'failed' row was
immediately sendable again, and the sender loop ticks every
SENDER_TICK_S=5s. `ReadingRejected`'s own docstring says "back off HARD";
this implements it:

  1. 401 is an AUTH failure, not a reading failure — rows are left pending
     (they are fine; the key is not) and the sender enters a long cooldown.
  2. Non-401 rejects / per-item failures burn `attempts`; after
     MAX_SEND_ATTEMPTS the row is 'skipped' (give-up) and stops blocking
     the queue.
  3. Batch-level failures escalate a sender-wide backoff
     (10s, 20s, 40s, ... capped at 5 min); any success resets it.
  4. Transient 5xx leaves rows PENDING without burning attempts — a long
     server outage must not consume the give-up budget of readings that
     would succeed on recovery (the 48h backfill contract is unchanged).
"""

import pytest

from custom_components.svitgrid.api_client import ReadingRejected
from custom_components.svitgrid.reading_sender import (
    AUTH_COOLDOWN_S,
    Cadence,
    SenderHealth,
    drain_once,
)
from custom_components.svitgrid.reading_store import MAX_SEND_ATTEMPTS, ReadingStore


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
    return _SyncStore(None, str(tmp_path / "readings.db"))


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def push_readings_batch(self, api_key, readings):
        self.calls.append(readings)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


NOW = "2026-07-22T12:00:00Z"
TS = "2026-07-22T10:00:00Z"


def _clock(start=1000.0):
    """Controllable monotonic clock for SenderHealth."""
    state = {"t": start}

    def now():
        return state["t"]

    def advance(s):
        state["t"] += s

    return now, advance


@pytest.mark.asyncio
async def test_401_leaves_rows_pending_and_enters_auth_cooldown(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient(ReadingRejected(401, "Invalid API key"))
    now, _ = _clock()
    health = SenderHealth(now=now)

    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )

    assert sent == 0
    # The readings are FINE — the key is dead. They must not burn attempts.
    assert store._count_by_state_sync() == {"pending": 1}
    assert health.in_cooldown()


@pytest.mark.asyncio
async def test_auth_cooldown_blocks_http_entirely(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient(ReadingRejected(401, "Invalid API key"))
    now, advance = _clock()
    health = SenderHealth(now=now)

    await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert len(client.calls) == 1

    # Every drain inside the cooldown must be a no-op — zero HTTP.
    for _ in range(5):
        sent = await drain_once(
            store=store,
            api_client=client,
            api_key="k",
            now_iso=NOW,
            cadence=Cadence(interval_s=10),
            health=health,
        )
        assert sent == 0
    assert len(client.calls) == 1

    # After the cooldown lapses, the sender tries again.
    advance(AUTH_COOLDOWN_S + 1)
    await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_non_401_reject_still_marks_failed(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient(ReadingRejected(400, "bad"))
    now, _ = _clock()
    health = SenderHealth(now=now)

    await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert store._count_by_state_sync() == {"failed": 1}
    assert health.in_cooldown()  # batch failure escalates the backoff


@pytest.mark.asyncio
async def test_transient_5xx_leaves_rows_pending(tmp_path):
    # Outage recovery: a 5xx must not burn the give-up budget — rows stay
    # pending so the full backlog lands when the server comes back (48h cap).
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient(None)  # transient
    now, _ = _clock()
    health = SenderHealth(now=now)

    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert sent == 0
    assert store._count_by_state_sync() == {"pending": 1}
    assert health.in_cooldown()


@pytest.mark.asyncio
async def test_give_up_after_max_attempts(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    keys = [("inv-1", TS)]

    for _ in range(MAX_SEND_ATTEMPTS):
        store._mark_failed_sync(keys, NOW)

    assert store._count_by_state_sync() == {"skipped": 1}
    assert store._get_sendable_sync(NOW, 48 * 3600, 50) == []


@pytest.mark.asyncio
async def test_row_below_max_attempts_is_still_sendable(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    keys = [("inv-1", TS)]

    for _ in range(MAX_SEND_ATTEMPTS - 1):
        store._mark_failed_sync(keys, NOW)

    assert store._count_by_state_sync() == {"failed": 1}
    rows = store._get_sendable_sync(NOW, 48 * 3600, 50)
    assert len(rows) == 1


def test_failure_streak_escalates_and_success_resets():
    now, advance = _clock()
    health = SenderHealth(now=now)

    # 10s, 20s, 40s ... capped at 300s.
    expected = [10, 20, 40, 80, 160, 300, 300]
    for exp in expected:
        health.note_batch_failure()
        assert health.cooldown_remaining() == pytest.approx(exp)
        advance(exp + 0.1)  # let it lapse before the next escalation

    health.note_ok()
    assert not health.in_cooldown()
    health.note_batch_failure()
    assert health.cooldown_remaining() == pytest.approx(10)  # streak reset


@pytest.mark.asyncio
async def test_per_item_all_failed_escalates_backoff(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient({"results": [{"ok": False, "error": "unknown inverter"}]})
    now, _ = _clock()
    health = SenderHealth(now=now)

    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert sent == 0
    assert store._count_by_state_sync() == {"failed": 1}
    assert health.in_cooldown()


@pytest.mark.asyncio
async def test_partial_success_resets_streak(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-07-22T10:00:05Z"})
    client = _FakeClient({"results": [{"ok": True}, {"ok": False, "error": "nope"}]})
    now, advance = _clock()
    health = SenderHealth(now=now)
    health.note_batch_failure()  # pre-existing streak...
    advance(11)  # ...whose cooldown has lapsed (else the guard blocks the drain)

    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
        health=health,
    )
    assert sent == 1
    assert not health.in_cooldown()  # any accepted row proves the pipe works


@pytest.mark.asyncio
async def test_drain_without_health_is_unchanged(tmp_path):
    # Callers that don't pass health (older call sites, tests) keep the
    # exact pre-0.17.1 behaviour minus nothing — no crash, no cooldown.
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": TS})
    client = _FakeClient({"results": [{"ok": True}]})

    sent = await drain_once(
        store=store,
        api_client=client,
        api_key="k",
        now_iso=NOW,
        cadence=Cadence(interval_s=10),
    )
    assert sent == 1
