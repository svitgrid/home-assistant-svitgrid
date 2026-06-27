"""Direct-harvest engine: poll the inverter, decode, append to the store.

Reuses the existing store/cadence/sender/gate pipeline from readings_publisher.
Single-snapshot-per-tick cadence only (no idle aggregation — spec §3.4).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..readings_publisher import (
    _DEFAULT_INTERVAL_S,
    _clamp_interval,
    assemble_payload,
    gate_payload,
)
from .decoder import decode, sanitize
from .transport import read_raw

_LOGGER = logging.getLogger(__name__)


async def poll_once(*, hass, spec, cfg, inverter_id: str, store) -> bool:
    """Read the inverter once, decode + sanitize, gate, and append to store.

    Returns True when a reading was appended; False when gated (missing required
    fields) — caller should log the outcome at the right verbosity but need not
    treat False as an error (transient gaps are normal at startup / cloudy days).
    """
    raw = await read_raw(hass, spec, cfg)
    fields = sanitize(decode(spec, raw), spec)
    non_none: dict[str, Any] = {k: v for k, v in fields.items() if v is not None}
    payload = assemble_payload(inverter_id=inverter_id, fields=non_none)
    payload, missing = gate_payload(payload)
    if missing:
        _LOGGER.debug("harvest %s gated: missing required fields %s", inverter_id, missing)
        return False
    await store.append(payload)
    return True


async def run_direct_harvest_loop(
    *,
    hass,
    store,
    cadence,
    inverter_id: str,
    cfg: dict,
    spec_holder,
    lifecycle=None,
    activity=None,
) -> None:
    """Single-snapshot-per-tick harvest loop mirroring readings_publisher.run_loop.

    Lifecycle: exits when hass.is_stopping is True or (when provided)
    lifecycle.active becomes False — same contract as the entity publisher.

    Cadence: interval_s is read from the shared cadence holder each tick
    (so the sender can adjust it) and clamped to sane bounds. On exception
    the loop falls back to _DEFAULT_INTERVAL_S to avoid tight retry loops.

    spec_holder.spec is refreshed by the cache; the loop skips a tick (with
    a debug log) when it is None so we don't attempt reads before the spec
    is ready.

    No idle sub-sampling (spec §3.4): each tick is one synchronous inverter
    read + decode, never an aggregation window.
    """
    _LOGGER.info("Direct harvest loop started for inverter %s", inverter_id)
    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        next_sleep_s = _clamp_interval(float(cadence.interval_s))
        try:
            spec = getattr(spec_holder, "spec", None)
            if spec is None:
                _LOGGER.debug(
                    "harvest %s: spec not ready yet, skipping tick", inverter_id
                )
            else:
                ok = await poll_once(
                    hass=hass, spec=spec, cfg=cfg,
                    inverter_id=inverter_id, store=store,
                )
                if ok and activity is not None:
                    # Mirror the entity run_loop call: sample_count=1, period_sec
                    # from the current cadence. summary is omitted (payload not
                    # returned from poll_once to keep its interface simple).
                    activity.record_ingest_success(
                        sample_count=1,
                        period_sec=int(next_sleep_s),
                        summary={},
                    )
        except Exception as exc:  # noqa: BLE001  — fail-soft, retry next tick
            _LOGGER.exception(
                "harvest %s poll failed; backing off to default interval", inverter_id
            )
            if activity is not None:
                activity.record_ingest_failure(reason=str(exc) or type(exc).__name__)
            next_sleep_s = float(_DEFAULT_INTERVAL_S)
        await asyncio.sleep(next_sleep_s)
    _LOGGER.info("Direct harvest loop stopped for inverter %s", inverter_id)
