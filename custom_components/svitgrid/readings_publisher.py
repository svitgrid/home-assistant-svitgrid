"""Readings publisher loop: builds a payload from current HA entity states
and POSTs it to /api/v1/ingest/reading. Sleep cadence is adaptive — the
server response carries `ingestIntervalMs` which the publisher uses to
size the next sleep. Matches the edge connector + mobile harvester:
60s during active sessions, 300s during idle.

We read state on-demand rather than maintaining a state_changed
subscription — simpler and sufficient at these cadences."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import SvitgridApiClient
from .const import READING_SOURCE, READINGS_INTERVAL_S

# Sane bounds for the server-driven cadence. Floor prevents a misbehaving
# server from spinning us in a 1ms tight loop; ceiling prevents the same
# server from silently parking us for hours on end.
_INTERVAL_FLOOR_S = 10
_INTERVAL_CEILING_S = 30 * 60
# Default when the response is missing ingestIntervalMs (old server, network
# error). 60s = active-session cadence; safer than the legacy 10s and never
# slower than idle.
_DEFAULT_INTERVAL_S = 60

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


def _next_interval_s(response: dict[str, Any] | None) -> float:
    """Pick the next sleep duration based on the server's ingestIntervalMs.
    Falls back to default on missing field / null response; clamps to
    [floor, ceiling] to absorb pathological server values."""
    if response is None:
        return float(_DEFAULT_INTERVAL_S)
    raw_ms = response.get("ingestIntervalMs")
    if not isinstance(raw_ms, (int, float)):
        return float(_DEFAULT_INTERVAL_S)
    seconds = raw_ms / 1000.0
    if seconds < _INTERVAL_FLOOR_S:
        return float(_INTERVAL_FLOOR_S)
    if seconds > _INTERVAL_CEILING_S:
        return float(_INTERVAL_CEILING_S)
    return seconds


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    api_key: str,
    inverter_id: str,
    entity_map: dict[str, str],
    # interval_s kept for backwards-compat with callers; ignored once the
    # server returns ingestIntervalMs. Used only as the very first sleep
    # before any response is in hand (which never actually happens since we
    # sleep AFTER pushing, not before — kept here so existing call sites
    # don't have to change).
    interval_s: int = READINGS_INTERVAL_S,
) -> None:
    """Long-running coroutine. Pushes a reading, then sleeps for the
    server-driven `ingestIntervalMs` (clamped 10s–30min). Exits when
    `hass.is_stopping` becomes True."""
    _LOGGER.info(
        "Readings publisher started (cadence adaptive; default=%ss, floor=%ss, ceiling=%ss)",
        _DEFAULT_INTERVAL_S, _INTERVAL_FLOOR_S, _INTERVAL_CEILING_S,
    )
    while not hass.is_stopping:
        next_sleep_s = float(_DEFAULT_INTERVAL_S)
        try:
            payload = build_reading_payload(
                hass=hass, inverter_id=inverter_id, entity_map=entity_map
            )
            response = await api_client.push_reading(api_key=api_key, reading=payload)
            next_sleep_s = _next_interval_s(response)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Readings publish failed; will retry next tick")
        await asyncio.sleep(next_sleep_s)
    _LOGGER.info("Readings publisher stopped")
