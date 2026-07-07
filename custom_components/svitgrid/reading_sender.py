"""Outbound buffer drain + adaptive-cadence holder (Sub-project 1)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import DeviceEvicted, ReadingRejected
from .battery_sign import flip_battery_sign
from .const import BACKFILL_CAP_S, CADENCE_DEFAULT_INTERVAL_S, INGEST_BATCH_MAX, SENDER_TICK_S

_LOGGER = logging.getLogger(__name__)


@dataclass
class Cadence:
    """Shared produce-cadence the sender updates and the publisher reads."""

    interval_s: int = CADENCE_DEFAULT_INTERVAL_S


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def drain_once(
    *,
    store,
    api_client,
    api_key: str,
    now_iso: str,
    cadence: Cadence,
    batch_max: int = INGEST_BATCH_MAX,
    cap_s: int = BACKFILL_CAP_S,
    lifecycle=None,
    discharge_positive_ids: set[str] | None = None,
) -> int:
    """Drain at most one batch. Returns number of rows marked 'sent'.

    ``discharge_positive_ids`` names inverters whose battery power we normalized
    to Svitgrid's charge-positive convention at capture (HA Solarman). We
    re-invert those before upload so the cloud keeps receiving the raw
    discharge-positive value its `home_assistant_solarman` handler negates —
    the server contract is unchanged. See battery_sign.py.
    """
    # Age out anything beyond the backfill cap so it never clogs the queue.
    await _maybe(store.skip_aged(now_iso, cap_s))

    rows = await _maybe(store.get_sendable(now_iso, cap_s, batch_max))
    if not rows:
        return 0

    flip_ids = discharge_positive_ids or set()
    keys = [(r["inverter_id"], r["ts"]) for r in rows]
    readings = [
        flip_battery_sign(r["payload"]) if r["inverter_id"] in flip_ids else r["payload"]
        for r in rows
    ]

    try:
        body = await api_client.push_readings_batch(api_key=api_key, readings=readings)
    except DeviceEvicted as exc:
        if lifecycle is not None:
            lifecycle.deprovision(str(exc), now_iso)
            await _maybe(store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since))
        return 0
    except ReadingRejected:
        await _maybe(store.mark_failed(keys, now_iso))
        return 0

    if body is None:  # transient 5xx
        await _maybe(store.mark_failed(keys, now_iso))
        return 0

    if isinstance(body, dict) and body.get("stopped"):
        _LOGGER.warning(
            "cloud reports device stopped (%s); leaving %d row(s) pending",
            body.get("stoppedReason"),
            len(keys),
        )
        if lifecycle is not None:
            lifecycle.pause(str(body.get("stoppedReason") or "stopped"), now_iso)
            await _maybe(store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since))
        return 0

    # Map per-item results back to rows (results are returned in input order).
    results = body.get("results") if isinstance(body, dict) else None
    sent_keys: list[tuple[str, str]] = []
    failed_keys: list[tuple[str, str]] = []
    if isinstance(results, list) and len(results) == len(keys):
        for key, res in zip(keys, results, strict=False):
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
    *,
    hass: HomeAssistant,
    store,
    api_client,
    api_key: str,
    cadence: Cadence,
    tick_s: int = SENDER_TICK_S,
    lifecycle=None,
    discharge_positive_ids: set[str] | None = None,
) -> None:
    wait_for_data = getattr(store, "wait_for_data", None)
    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        try:
            await drain_once(
                store=store,
                api_client=api_client,
                api_key=api_key,
                now_iso=_now_iso(),
                cadence=cadence,
                lifecycle=lifecycle,
                discharge_positive_ids=discharge_positive_ids,
            )
        except Exception:  # never let the sender loop die
            _LOGGER.exception("sender drain failed")
        # Use the store's data-available event when present so a fresh reading
        # wakes the sender immediately instead of waiting up to tick_s.
        # Fall back to a plain sleep for test doubles or stores that lack the method.
        if wait_for_data is not None:
            await wait_for_data(tick_s)
        else:
            await asyncio.sleep(tick_s)


async def _maybe(awaitable_or_value: Any) -> Any:
    """Await coroutines; pass through plain values (lets tests pass a store
    whose async wrappers are real coroutines while keeping drain_once simple)."""
    if asyncio.iscoroutine(awaitable_or_value):
        return await awaitable_or_value
    return awaitable_or_value
