"""The household's outage schedule on a Home Assistant island (svitgrid D-2153).

A signed-in island fetches its household's outage schedule from
`GET /api/v1/island-outage-schedule` with its cloud API key, at most once an
hour, and keeps the last answer across restarts. A failed fetch keeps the
last answer. A guest (local-only) entry has no key and fetches nothing, and a
cloud station never starts the loop. The call carries no readings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import SvitgridApiClient, SvitgridApiError
from custom_components.svitgrid.outage_schedule import (
    FETCH_INTERVAL_S,
    OutageSchedule,
    fetch_once,
    run_outage_schedule_loop,
    seconds_until_due,
)

NOW = 1_763_460_000.0

ANSWER = {
    "region": "kyiv",
    "group": "3.1",
    "factual": [{"date": "2026-11-18", "groups": [{"group": "3.1", "hourly": {"9": "off"}}]}],
}


class _MemStore:
    def __init__(self, state=None) -> None:
        self.state = state

    async def get_outage_schedule_state(self):
        return self.state

    async def set_outage_schedule_state(self, state):
        self.state = state


def test_due_at_once_without_a_saved_fetch_and_hourly_after():
    assert seconds_until_due(None, NOW) == 0
    assert seconds_until_due({"fetchedAt": NOW - 600}, NOW) == FETCH_INTERVAL_S - 600
    assert seconds_until_due({"fetchedAt": NOW - 2 * FETCH_INTERVAL_S}, NOW) == 0
    # A clock set back cannot push the next fetch past one interval.
    assert seconds_until_due({"fetchedAt": NOW + 99_999}, NOW) == FETCH_INTERVAL_S


async def test_fetch_keeps_the_answer_and_its_time():
    store = _MemStore()
    holder = OutageSchedule(store)
    client = MagicMock()
    client.get_island_outage_schedule = AsyncMock(return_value=ANSWER)
    await fetch_once(holder, client, "key", now_s=NOW)
    client.get_island_outage_schedule.assert_awaited_once_with("key")
    assert holder.current() == {**ANSWER, "fetchedAt": NOW}
    assert store.state == {**ANSWER, "fetchedAt": NOW}


async def test_a_failed_fetch_keeps_the_last_answer_and_retries_in_an_hour():
    store = _MemStore({**ANSWER, "fetchedAt": NOW - 7200})
    holder = OutageSchedule(store)
    await holder.load()
    client = MagicMock()
    client.get_island_outage_schedule = AsyncMock(side_effect=RuntimeError("offline"))
    await fetch_once(holder, client, "key", now_s=NOW)
    assert holder.current()["factual"] == ANSWER["factual"]
    assert holder.attempted_at == NOW
    assert seconds_until_due({"fetchedAt": holder.attempted_at}, NOW) == FETCH_INTERVAL_S


async def test_a_malformed_answer_is_not_kept():
    holder = OutageSchedule(_MemStore())
    client = MagicMock()
    client.get_island_outage_schedule = AsyncMock(return_value={"region": 5})
    await fetch_once(holder, client, "key", now_s=NOW)
    assert holder.current() is None


async def test_no_region_set_is_kept_as_an_empty_schedule():
    holder = OutageSchedule(_MemStore())
    client = MagicMock()
    client.get_island_outage_schedule = AsyncMock(
        return_value={"region": None, "group": None, "factual": []}
    )
    await fetch_once(holder, client, "key", now_s=NOW)
    assert holder.current() == {"region": None, "group": None, "factual": [], "fetchedAt": NOW}


async def test_the_loop_waits_out_a_saved_fetch_after_a_restart():
    store = _MemStore({**ANSWER, "fetchedAt": NOW - 600})
    holder = OutageSchedule(store)
    client = MagicMock()
    client.get_island_outage_schedule = AsyncMock(return_value=ANSWER)
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) > 1:
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        await run_outage_schedule_loop(
            holder=holder, api_client=client, api_key="key", now=lambda: NOW, sleep=sleep
        )
    assert sleeps[0] == FETCH_INTERVAL_S - 600
    assert holder.current()["fetchedAt"] == NOW


async def test_client_reads_the_route_with_the_key_and_no_body():
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=ANSWER)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    client = SvitgridApiClient(session, "https://api.example")
    assert await client.get_island_outage_schedule("k") == ANSWER
    args, kwargs = session.get.call_args
    assert args[0] == "https://api.example/api/v1/island-outage-schedule"
    assert kwargs == {"headers": {"x-api-key": "k"}}


async def test_client_raises_on_a_refusal():
    resp = MagicMock()
    resp.status = 404
    resp.text = AsyncMock(return_value="not found")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    client = SvitgridApiClient(session, "https://api.example")
    with pytest.raises(SvitgridApiError):
        await client.get_island_outage_schedule("k")


def test_store_round_trips_the_state(tmp_path):
    from custom_components.svitgrid.reading_store import ReadingStore

    store = ReadingStore(None, str(tmp_path / "r.db"))
    assert store._get_outage_schedule_state_sync() is None
    store._set_outage_schedule_state_sync({**ANSWER, "fetchedAt": NOW})
    assert store._get_outage_schedule_state_sync() == {**ANSWER, "fetchedAt": NOW}
