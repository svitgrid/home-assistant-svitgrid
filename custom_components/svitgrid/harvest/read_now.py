"""On-demand read ("Read now") for direct-harvest inverters.

One ``ReadNowTrigger`` per direct-harvest inverter. The app's ``poll_now``
command, the "Read now" button entity and the panel's read-now endpoint all
call ``request()``; the harvest loop waits on the trigger alongside its sleep
and runs exactly one extra poll when woken, keeping the regular deadline.

The logger accepts one connection at a time, so a request made while a poll is
already in flight is refused (``request()`` returns None) rather than queued
behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..const import DOMAIN


class ReadNowTrigger:
    """Wakes one harvest loop for a single immediate poll."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._waiters: list[asyncio.Future] = []
        self.in_flight = False
        self.last_outcome: dict[str, Any] | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> asyncio.Future | None:
        """Ask for one immediate poll.

        Returns a future resolved with the poll's outcome dict, or None when a
        poll is already in flight. Several requests made before the loop wakes
        share that one poll.
        """
        if self.in_flight:
            return None
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        self._event.set()
        return fut

    async def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True if a read was requested."""
        if not self._event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event.wait(), seconds)
        return self._event.is_set()

    def begin_tick(self) -> list[asyncio.Future]:
        """Mark a poll in flight and take the requests it will answer."""
        self.in_flight = True
        self._event.clear()
        waiters, self._waiters = self._waiters, []
        return waiters

    def end_tick(self, waiters: list[asyncio.Future], outcome: dict[str, Any]) -> None:
        self.in_flight = False
        self.last_outcome = outcome
        for fut in waiters:
            if not fut.done():
                fut.set_result(outcome)


def find_triggers(hass, inverter_id: str | None) -> dict[str, ReadNowTrigger]:
    """Triggers across config entries: the one for ``inverter_id``, or all."""
    if hass is None:
        return {}
    found: dict[str, ReadNowTrigger] = {}
    for state in (getattr(hass, "data", {}).get(DOMAIN) or {}).values():
        if not isinstance(state, dict):
            continue
        for inv_id, trigger in (state.get("read_now") or {}).items():
            if inverter_id is None or inv_id == inverter_id:
                found[inv_id] = trigger
    return found
