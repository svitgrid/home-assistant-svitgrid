"""Readings publisher loop: every 10s, builds a payload from the current
state of mapped HA entities and POSTs it to the Svitgrid readings endpoint.

We read state on-demand rather than maintaining a state_changed subscription
— it's simpler and sufficient for a 10s cadence. An event-driven upgrade
is a B2/B3 optimization."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import SvitgridApiClient
from .const import READING_SOURCE, READINGS_INTERVAL_S

_LOGGER = logging.getLogger(__name__)

# States that mean "no usable value" and the field should be omitted entirely.
_UNAVAILABLE_STATES = {"unavailable", "unknown", "none", None}


def build_reading_payload(
    *, hass: HomeAssistant, inverter_id: str, entity_map: dict[str, str]
) -> dict[str, Any]:
    """Build a single reading payload. Fields whose mapped entity is
    missing / unavailable / non-numeric are omitted (not sent as null or 0).
    """
    payload: dict[str, Any] = {
        "inverterId": inverter_id,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": READING_SOURCE,
    }
    for field, entity_id in entity_map.items():
        state = hass.states.get(entity_id)
        if state is None:
            continue
        raw = state.state
        if raw in _UNAVAILABLE_STATES or not isinstance(raw, str):
            continue
        try:
            payload[field] = float(raw)
        except (TypeError, ValueError):
            continue
    # Aggregate pvPower from the per-MPPT values the server requires.
    # (Server schema requires `pvPower` as a top-level scalar; we keep
    # the pvN fields alongside for diagnostic purposes.)
    pv_total = 0.0
    has_any_pv = False
    for pv_field in ("pv1Power", "pv2Power", "pv3Power", "pv4Power"):
        if pv_field in payload:
            pv_total += payload[pv_field]
            has_any_pv = True
    if has_any_pv:
        payload["pvPower"] = pv_total
    return payload


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    api_key: str,
    inverter_id: str,
    entity_map: dict[str, str],
    interval_s: int = READINGS_INTERVAL_S,
) -> None:
    """Long-running coroutine: pushes a reading every `interval_s`. Exits
    when `hass.is_stopping` becomes True."""
    _LOGGER.info("Readings publisher started (interval=%ss)", interval_s)
    while not hass.is_stopping:
        try:
            payload = build_reading_payload(
                hass=hass, inverter_id=inverter_id, entity_map=entity_map
            )
            await api_client.push_reading(api_key=api_key, reading=payload)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Readings publish failed; will retry next tick")
        await asyncio.sleep(interval_s)
    _LOGGER.info("Readings publisher stopped")
