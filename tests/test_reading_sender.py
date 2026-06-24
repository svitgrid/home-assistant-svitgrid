import pytest

from custom_components.svitgrid.api_client import ReadingRejected
from custom_components.svitgrid.reading_sender import Cadence, drain_once
from custom_components.svitgrid.reading_store import ReadingStore


class _SyncStore(ReadingStore):
    async def skip_aged(self, now_iso, cap_s): return self._skip_aged_sync(now_iso, cap_s)
    async def get_sendable(self, now_iso, cap_s, limit): return self._get_sendable_sync(now_iso, cap_s, limit)
    async def mark_sent(self, keys): return self._mark_sent_sync(keys)
    async def mark_failed(self, keys, now_iso): return self._mark_failed_sync(keys, now_iso)


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
    client = _FakeClient({"results": [
        {"ok": True, "inverterId": "inv-1"},
        {"ok": True, "inverterId": "inv-1"},
    ], "ingestIntervalMs": 30000})
    cadence = Cadence(interval_s=10)

    sent = await drain_once(store=store, api_client=client, api_key="k",
                            now_iso=now, cadence=cadence)

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
    sent = await drain_once(store=store, api_client=client, api_key="k",
                            now_iso=now, cadence=cadence, batch_max=2)
    assert len(client.calls[0]) == 2  # only 2 sent this drain
    assert sent == 2


@pytest.mark.asyncio
async def test_drain_5xx_marks_failed(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    client = _FakeClient(None)  # 5xx → None
    cadence = Cadence(interval_s=10)
    sent = await drain_once(store=store, api_client=client, api_key="k",
                            now_iso=now, cadence=cadence)
    assert sent == 0
    assert store._count_by_state_sync() == {"failed": 1}


@pytest.mark.asyncio
async def test_drain_skips_aged_rows(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-20T12:00:00Z"})  # >48h
    client = _FakeClient({"results": []})
    cadence = Cadence(interval_s=10)
    sent = await drain_once(store=store, api_client=client, api_key="k",
                            now_iso=now, cadence=cadence)
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
    sent = await drain_once(store=store, api_client=client, api_key="k",
                            now_iso=now, cadence=cadence)
    assert sent == 0
    assert store._count_by_state_sync() == {"failed": 1}
