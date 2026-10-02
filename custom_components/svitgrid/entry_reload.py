"""Write config entry data without the update listener reloading the entry.

A command that changes `entry.data` and then reloads the entry itself would
otherwise reload twice: once explicitly, and once from `_async_reload_entry`,
which Home Assistant fires for every changed update. Two back-to-back setups
restart the harvest loop, the command poller and MQTT twice per apply
(ivanursul/svitgrid#751).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .const import DOMAIN

SKIP_LISTENER_RELOAD_KEY = "_skip_listener_reload_entry_ids"


def update_entry_skipping_listener_reload(hass, entry, data: Mapping[str, Any]) -> bool:
    """Update `entry.data`; the caller schedules the one reload itself.

    Home Assistant starts update listeners eagerly, so `_async_reload_entry`
    reads the skip before `async_update_entry` returns. The `finally` clears a
    skip that no listener consumed — an unchanged update fires none, and an
    entry whose setup failed has none registered — so it cannot swallow the
    reload of a later, unrelated update. If listeners ever stopped starting
    eagerly, this degrades to the old double reload, never to a missed one.
    """
    skips = hass.data.setdefault(DOMAIN, {}).setdefault(SKIP_LISTENER_RELOAD_KEY, set())
    skips.add(entry.entry_id)
    try:
        return hass.config_entries.async_update_entry(entry, data=data)
    finally:
        skips.discard(entry.entry_id)


def consume_listener_reload_skip(hass, entry) -> bool:
    """Return True, once, if the update that fired the listener asked to skip."""
    skips = hass.data.get(DOMAIN, {}).get(SKIP_LISTENER_RELOAD_KEY)
    if skips and entry.entry_id in skips:
        skips.discard(entry.entry_id)
        return True
    return False
