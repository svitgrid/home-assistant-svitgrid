"""Tests for IslandEventStore — local SQLite calendar-event store."""

import pytest

from custom_components.svitgrid.island_event_store import IslandEventStore


def _store(tmp_path):
    # Mirror reading_store pattern: hass=None, direct db_path.
    return IslandEventStore(str(tmp_path / "island_events.db"))


# ── upsert / list ──────────────────────────────────────────────────────────────


def test_upsert_then_list_roundtrips_event(tmp_path):
    store = _store(tmp_path)
    event = {"id": "evt-1", "type": "tou", "enabled": True, "slots": [{"hour": 8}]}
    store._upsert_event_sync(event)

    rows = store._list_events_sync()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "evt-1"
    assert row["type"] == "tou"
    assert row["slots"] == [{"hour": 8}]
    # executionState defaults to {} when none set
    assert row["executionState"] == {}


def test_upsert_same_id_updates_not_duplicates(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou", "slots": []})
    store._upsert_event_sync({"id": "evt-1", "type": "sell_to_grid", "slots": [1, 2]})

    rows = store._list_events_sync()
    assert len(rows) == 1
    assert rows[0]["type"] == "sell_to_grid"
    assert rows[0]["slots"] == [1, 2]


def test_upsert_multiple_events_all_listed(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "a", "type": "tou"})
    store._upsert_event_sync({"id": "b", "type": "sell_to_grid"})

    ids = {r["id"] for r in store._list_events_sync()}
    assert ids == {"a", "b"}


# ── delete ─────────────────────────────────────────────────────────────────────


def test_delete_removes_event_and_returns_true(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    result = store._delete_event_sync("evt-1")
    assert result is True
    assert store._list_events_sync() == []


def test_delete_missing_event_returns_false(tmp_path):
    store = _store(tmp_path)
    result = store._delete_event_sync("nonexistent")
    assert result is False


def test_delete_only_removes_target(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "a", "type": "tou"})
    store._upsert_event_sync({"id": "b", "type": "sell_to_grid"})
    store._delete_event_sync("a")

    rows = store._list_events_sync()
    assert len(rows) == 1
    assert rows[0]["id"] == "b"


# ── get_event ──────────────────────────────────────────────────────────────────


def test_get_event_returns_event_dict(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou", "value": 42})
    row = store._get_event_sync("evt-1")
    assert row is not None
    assert row["id"] == "evt-1"
    assert row["value"] == 42


def test_get_event_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store._get_event_sync("missing") is None


def test_get_event_includes_execution_state(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    store._set_execution_state_sync("evt-1", {"status": "running"})
    row = store._get_event_sync("evt-1")
    assert row["executionState"] == {"status": "running"}


# ── set_execution_state ────────────────────────────────────────────────────────


def test_set_execution_state_persists(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    store._set_execution_state_sync("evt-1", {"status": "done", "lastRun": "2026-06-29T10:00:00Z"})

    rows = store._list_events_sync()
    assert rows[0]["executionState"] == {"status": "done", "lastRun": "2026-06-29T10:00:00Z"}


def test_set_execution_state_overwrites_previous(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    store._set_execution_state_sync("evt-1", {"status": "running"})
    store._set_execution_state_sync("evt-1", {"status": "done"})

    rows = store._list_events_sync()
    assert rows[0]["executionState"] == {"status": "done"}


def test_list_events_merges_execution_state(tmp_path):
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    store._upsert_event_sync({"id": "evt-2", "type": "sell_to_grid"})
    store._set_execution_state_sync("evt-1", {"status": "pending"})

    rows = {r["id"]: r for r in store._list_events_sync()}
    assert rows["evt-1"]["executionState"] == {"status": "pending"}
    assert rows["evt-2"]["executionState"] == {}


# ── COALESCE preservation ──────────────────────────────────────────────────────


def test_upsert_preserves_existing_execution_state(tmp_path):
    """Re-upserting event metadata must NOT wipe in-flight execution state."""
    store = _store(tmp_path)
    store._upsert_event_sync({"id": "evt-1", "type": "tou"})
    store._set_execution_state_sync("evt-1", {"status": "running"})
    store._upsert_event_sync({"id": "evt-1", "type": "sell_to_grid"})  # re-upsert metadata
    row = store._get_event_sync("evt-1")
    assert row["executionState"] == {"status": "running"}  # preserved by COALESCE
    assert row["type"] == "sell_to_grid"  # metadata updated


# ── async wrappers ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_upsert_and_list(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-async", "type": "tou"})
    rows = await store.async_list_events()
    assert len(rows) == 1
    assert rows[0]["id"] == "evt-async"


@pytest.mark.asyncio
async def test_async_delete(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-1", "type": "tou"})
    removed = await store.async_delete_event("evt-1")
    missing = await store.async_delete_event("does-not-exist")
    assert removed is True
    assert missing is False


@pytest.mark.asyncio
async def test_async_get_event(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-1", "type": "tou", "x": 99})
    found = await store.async_get_event("evt-1")
    not_found = await store.async_get_event("nope")
    assert found["x"] == 99
    assert not_found is None


@pytest.mark.asyncio
async def test_async_set_execution_state(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-1", "type": "tou"})
    await store.async_set_execution_state("evt-1", {"status": "idle"})
    rows = await store.async_list_events()
    assert rows[0]["executionState"] == {"status": "idle"}


# ── the phone's signature (edge island, Task 12, svitgrid#1232) ───────────────

SIGNING = {
    "signedEventData": {"id": "evt-1", "type": "tou"},
    "signature": "MEUCIQ",
    "signingKeyId": "phone-1",
}


@pytest.mark.asyncio
async def test_event_store_round_trips_signing(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-1", "type": "tou"}, signing=SIGNING)
    await store.async_upsert_event({"id": "evt-2", "type": "tou"})

    by_id = {e["id"]: e for e in await store.async_list_events()}
    assert by_id["evt-1"]["signing"] == SIGNING
    assert by_id["evt-2"]["signing"] is None
    assert (await store.async_get_event("evt-1"))["signing"] == SIGNING


@pytest.mark.asyncio
async def test_set_execution_state_keeps_signing(tmp_path):
    store = _store(tmp_path)
    await store.async_upsert_event({"id": "evt-1", "type": "tou"}, signing=SIGNING)
    await store.async_set_execution_state("evt-1", {"status": "active"})

    event = await store.async_get_event("evt-1")
    assert event["signing"] == SIGNING
    assert event["executionState"] == {"status": "active"}


@pytest.mark.asyncio
async def test_table_written_by_0_25_0_opens_and_keeps_every_event(tmp_path):
    """A database written by 0.25.0 has no signing_json column. It opens, every
    event and its execution state survive, and the events read as unsigned."""
    import sqlite3

    db_path = str(tmp_path / "island_events.db")
    conn = sqlite3.connect(db_path)
    # The 0.25.0 schema, verbatim.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS island_events (
            event_id        TEXT PRIMARY KEY,
            event_json      TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            execution_state TEXT NOT NULL DEFAULT '{}',
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_island_events_updated
            ON island_events (updated_at);
        """
    )
    conn.execute(
        "INSERT INTO island_events VALUES (?, ?, ?, ?, ?)",
        ("evt-old", '{"id":"evt-old","type":"tou"}', 1, '{"status":"active"}', "2026-10-01Z"),
    )
    conn.execute(
        "INSERT INTO island_events VALUES (?, ?, ?, ?, ?)",
        ("evt-off", '{"id":"evt-off","enabled":false}', 0, "{}", "2026-10-02Z"),
    )
    conn.commit()
    conn.close()

    store = IslandEventStore(db_path)
    events = await store.async_list_events()

    assert [e["id"] for e in events] == ["evt-old", "evt-off"]
    assert events[0]["executionState"] == {"status": "active"}
    assert events[1]["enabled"] is False
    assert all(e["signing"] is None for e in events)

    # The migrated table takes a signed upsert and keeps the execution state.
    await store.async_upsert_event({"id": "evt-old", "type": "tou"}, signing=SIGNING)
    event = await store.async_get_event("evt-old")
    assert event["signing"] == SIGNING
    assert event["executionState"] == {"status": "active"}
