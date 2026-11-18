"""The household's outage schedule on a Home Assistant island (svitgrid D-2153).

The spec's "Public data" decision lets a signed-in owner's island fetch the
outage schedule, and nothing else, from the Svitgrid cloud. The island's
Telegram warning before a scheduled outage reads it (svitgrid D-2154).

The add-on does not know its household's region or group, and the public
`GET /api/v1/outages/:region` wants a Firebase ID token, so it asks
`GET /api/v1/island-outage-schedule` with its cloud API key. The answer is
`{region, group, factual}`, `factual` holding only the household's group in
the region document's shape. The call sends no readings.

At most once an hour. The last good answer is kept in the store's meta
table with the time it was fetched, so a restart neither fetches early nor
starts without a schedule. A failed fetch keeps the last answer and tries
again an hour later. A local-only (guest) entry has no key and never starts
the loop, and neither does an entry with the cloud copy on
(`async_setup_entry`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

FETCH_INTERVAL_S = 3600


def _valid(body: Any) -> dict | None:
    """The answer's three fields, or None when the body is not the route's shape."""
    if not isinstance(body, dict):
        return None
    region, group, factual = body.get("region"), body.get("group"), body.get("factual")
    if region is not None and not isinstance(region, str):
        return None
    if group is not None and not isinstance(group, str):
        return None
    if not isinstance(factual, list):
        return None
    return {"region": region, "group": group, "factual": factual}


def seconds_until_due(state: dict | None, now_s: float) -> float:
    """Seconds until the next fetch: 0 with none saved, else an hour after the last."""
    last = (state or {}).get("fetchedAt")
    if isinstance(last, bool) or not isinstance(last, int | float):
        return 0
    return min(FETCH_INTERVAL_S, max(0.0, last + FETCH_INTERVAL_S - now_s))


class OutageSchedule:
    """The last good answer for one household, and when it was last asked."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._state: dict | None = None
        self.attempted_at: float | None = None

    async def load(self) -> dict | None:
        try:
            state = await self._store.get_outage_schedule_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Outage schedule: saved state unreadable")
            state = None
        if isinstance(state, dict) and _valid(state) is not None:
            self._state = state
            fetched = state.get("fetchedAt")
            if isinstance(fetched, int | float) and not isinstance(fetched, bool):
                self.attempted_at = float(fetched)
        return self._state

    def current(self) -> dict | None:
        """`{region, group, factual, fetchedAt}`, or None before the first good answer."""
        return self._state

    async def keep(self, answer: dict, now_s: float) -> None:
        self._state = {**answer, "fetchedAt": now_s}
        self.attempted_at = now_s
        await self._store.set_outage_schedule_state(self._state)


async def fetch_once(
    holder: OutageSchedule, api_client: Any, api_key: str, *, now_s: float
) -> None:
    """Ask the cloud once. Keeps a good answer; any failure keeps the last one."""
    holder.attempted_at = now_s
    try:
        body = await api_client.get_island_outage_schedule(api_key)
    except Exception as err:  # noqa: BLE001
        _LOGGER.info("Outage schedule not fetched, keeping the last one: %s", err)
        return
    answer = _valid(body)
    if answer is None:
        _LOGGER.warning("Outage schedule: the cloud's answer is malformed, keeping the last one")
        return
    await holder.keep(answer, now_s)
    _LOGGER.debug(
        "Outage schedule fetched: region=%s group=%s days=%d",
        answer["region"],
        answer["group"],
        len(answer["factual"]),
    )


async def run_outage_schedule_loop(
    *,
    holder: OutageSchedule,
    api_client: Any,
    api_key: str,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Fetch the schedule once an hour, from the saved time on."""
    await holder.load()
    while True:
        last = holder.attempted_at
        await sleep(seconds_until_due(None if last is None else {"fetchedAt": last}, now()))
        try:
            await fetch_once(holder, api_client, api_key, now_s=now())
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Outage schedule fetch failed")
            holder.attempted_at = now()
