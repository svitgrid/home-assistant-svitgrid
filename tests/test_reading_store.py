import json

from custom_components.svitgrid.reading_store import ReadingStore


def _store(tmp_path):
    # hass is unused by the sync core; pass None.
    return ReadingStore(None, str(tmp_path / "readings.db"))


def test_append_then_recent_roundtrips_payload(tmp_path):
    store = _store(tmp_path)
    reading = {
        "inverterId": "inv-1",
        "timestamp": "2026-06-24T10:00:00Z",
        "source": "edge",
        "batterySoc": 85.0,
        "gridPower": 1200.0,
    }
    store._append_sync(reading)

    rows = store._recent_sync("inv-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["payload"] == reading
    assert rows[0]["sync_state"] == "pending"
    assert rows[0]["ts"] == "2026-06-24T10:00:00Z"


def test_append_is_idempotent_on_same_ts(tmp_path):
    store = _store(tmp_path)
    r = {"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z", "batterySoc": 1.0}
    store._append_sync(r)
    store._append_sync({**r, "batterySoc": 2.0})  # same (inverter, ts) → replace
    rows = store._recent_sync("inv-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["payload"]["batterySoc"] == 2.0


def test_count_by_state_groups_pending(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    assert store._count_by_state_sync() == {"pending": 2}
