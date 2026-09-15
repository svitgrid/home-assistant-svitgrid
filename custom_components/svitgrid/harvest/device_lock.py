"""One connection at a time per inverter logger.

A Solarman logger serves one TCP client reliably. The harvest loop, the
settings-sync loop and the write executor each open their own connection, and
before this lock the settings read at setup raced the harvest loop's first
read and failed. Every device read or write for a harvest target now holds
the lock for that target, so the calls queue instead of colliding.

The key is the logger's address (``ip`` and ``port`` of the harvest config),
not the inverter id: two inverters behind one logger share one connection too.

Locks are kept per event loop. An ``asyncio.Lock`` binds to the loop it first
waits on, so a lock shared across loops (a test run, a restarted loop) would
raise instead of queueing. Home Assistant runs one loop, so in production
every caller of one logger gets the same lock, across config-entry reloads.
"""

from __future__ import annotations

import asyncio
import weakref

_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]]
_LOCKS = weakref.WeakKeyDictionary()


def logger_lock(cfg: dict) -> asyncio.Lock:
    """The lock for the logger that ``cfg`` (a harvest config) addresses."""
    locks = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    key = (str(cfg.get("ip", "")), str(cfg.get("port", "")))
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock
