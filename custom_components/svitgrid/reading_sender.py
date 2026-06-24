"""Outbound buffer drain + adaptive-cadence holder (Sub-project 1)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import ReadingRejected
from .const import BACKFILL_CAP_S, INGEST_BATCH_MAX, READINGS_INTERVAL_S, SENDER_TICK_S

_LOGGER = logging.getLogger(__name__)


@dataclass
class Cadence:
    """Shared produce-cadence the sender updates and the publisher reads."""
    interval_s: int = READINGS_INTERVAL_S


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def drain_once(
    *, store, api_client, api_key: str, now_iso: str, cadence: Cadence,
    batch_max: int = INGEST_BATCH_MAX, cap_s: int = BACKFILL_CAP_S,
) -> int:
    """Drain at most one batch. Returns number of rows marked 'sent'."""
    # Age out anything beyond the backfill cap so it never clogs the queue.
    await _maybe(store.skip_aged(now_iso, cap_s))

    rows = await _maybe(store.get_sendable(now_iso, cap_s, batch_max))
    if not rows:
        return 0

    keys = [(r["inverter_id"], r["ts"]) for r in rows]
    readings = [r["payload"] for r in rows]

    try:
        body = await api_client.push_readings_batch(api_key=api_key, readings=readings)
    except ReadingRejected:
        await _maybe(store.mark_failed(keys, now_iso))
        return 0

    if body is None:  # transient 5xx
        await _maybe(store.mark_failed(keys, now_iso))
        return 0

    # Map per-item results back to rows (results are returned in input order).
    results = body.get("results") if isinstance(body, dict) else None
    sent_keys: list[tuple[str, str]] = []
    failed_keys: list[tuple[str, str]] = []
    if isinstance(results, list) and len(results) == len(keys):
        for key, res in zip(keys, results):
            (sent_keys if res.get("ok") else failed_keys).append(key)
    else:
        # No per-item detail → 2xx means the cloud accepted the batch.
        sent_keys = keys

    if sent_keys:
        await _maybe(store.mark_sent(sent_keys))
    if failed_keys:
        await _maybe(store.mark_failed(failed_keys, now_iso))

    interval_ms = body.get("ingestIntervalMs") if isinstance(body, dict) else None
    if isinstance(interval_ms, (int, float)) and interval_ms > 0:
        cadence.interval_s = int(interval_ms / 1000)

    return len(sent_keys)


async def run_sender_loop(
    *, hass: HomeAssistant, store, api_client, api_key: str, cadence: Cadence,
    tick_s: int = SENDER_TICK_S,
) -> None:
    while not hass.is_stopping:
        try:
            await drain_once(store=store, api_client=api_client, api_key=api_key,
                             now_iso=_now_iso(), cadence=cadence)
        except Exception:  # never let the sender loop die
            _LOGGER.exception("sender drain failed")
        await asyncio.sleep(tick_s)


async def _maybe(awaitable_or_value: Any) -> Any:
    """Await coroutines; pass through plain values (lets tests pass a store
    whose async wrappers are real coroutines while keeping drain_once simple)."""
    if asyncio.iscoroutine(awaitable_or_value):
        return await awaitable_or_value
    return awaitable_or_value
