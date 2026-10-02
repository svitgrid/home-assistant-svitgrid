"""Direct-harvest engine: poll the inverter, decode, append to the store.

Reuses the existing store/cadence/sender/gate pipeline from readings_publisher.
Single-snapshot-per-tick cadence only (no idle aggregation — spec §3.4).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..readings_publisher import (
    _DEFAULT_INTERVAL_S,
    _FIRST_READING_RETRY_S,
    _clamp_interval,
    _summary_of,
    assemble_payload,
    gate_payload,
)
from .decoder import decode, sanitize
from .read_now import ReadNowTrigger
from .spec_health import report_spec_unavailable
from .transport import read_raw

_LOGGER = logging.getLogger(__name__)

# Until the first reading is stored, a gated or failed tick is retried on a
# doubling back-off from _FIRST_READING_RETRY_S, capped here: 5, 10, 20, 30, 30…
# The first four retries land within 65 s, inside the app's 90 s first-reading
# wait. The regular cadence (300 s default) outlasted that wait entirely.
_FIRST_READING_RETRY_MAX_S = 30


def _first_reading_backoff(attempt: int) -> float:
    return float(min(_FIRST_READING_RETRY_S * (2**attempt), _FIRST_READING_RETRY_MAX_S))


def _is_unreachable(exc: Exception) -> bool:
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return True
    text = str(exc).lower()
    return "unreachable" in text or "failed to connect" in text


async def poll_once(
    *, hass, spec, cfg, inverter_id: str, store, missing_out: list[str] | None = None
) -> dict | None:
    """Read the inverter once, decode + sanitize, gate, and append to store.

    Returns the appended payload dict on success; None when gated (missing
    required fields) — caller should log the outcome at the right verbosity but
    need not treat None as an error (transient gaps are normal at startup /
    cloudy days). When gated, the missing field names are appended to
    ``missing_out`` if given.
    """
    raw = await read_raw(hass, spec, cfg)
    fields = sanitize(decode(spec, raw), spec)
    non_none: dict[str, Any] = {k: v for k, v in fields.items() if v is not None}
    payload = assemble_payload(inverter_id=inverter_id, fields=non_none)
    payload, missing = gate_payload(payload)
    if missing:
        _LOGGER.debug("harvest %s gated: missing required fields %s", inverter_id, missing)
        if missing_out is not None:
            missing_out.extend(missing)
        return None
    await store.append(payload)
    return payload


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
    read_now: ReadNowTrigger | None = None,
    clock=time.monotonic,
) -> None:
    """Single-snapshot-per-tick harvest loop mirroring readings_publisher.run_loop.

    Lifecycle: exits when hass.is_stopping is True or (when provided)
    lifecycle.active becomes False — same contract as the entity publisher.

    Cadence: interval_s is read from the shared cadence holder each tick
    (so the sender can adjust it) and clamped to sane bounds. On exception
    the loop falls back to _DEFAULT_INTERVAL_S to avoid tight retry loops.
    Before the first reading is stored, a gated or failed tick is retried on
    a short capped back-off instead (see _first_reading_backoff).

    Read now: when ``read_now`` is given, the loop waits on it instead of a
    plain sleep. A request runs one extra poll and the loop then waits out the
    REST of the original interval, so the regular schedule is neither reset
    nor doubled.

    spec_holder.spec is refreshed by the cache; the loop skips a tick (with
    a debug log) when it is None so we don't attempt reads before the spec
    is ready.

    No idle sub-sampling (spec §3.4): each tick is one synchronous inverter
    read + decode, never an aggregation window.
    """
    _LOGGER.info("Direct harvest loop started for inverter %s", inverter_id)
    # Consecutive ticks skipped because no spec is loaded. A missing spec is
    # the SAME user-visible symptom as a broken one (nothing at all, for ever),
    # so it gets the same escalation instead of an eternal debug line.
    specless_ticks = 0

    def alive() -> bool:
        return not hass.is_stopping and (lifecycle is None or lifecycle.active)

    async def tick(period_s: float) -> dict[str, Any]:
        nonlocal specless_ticks
        waiters = read_now.begin_tick() if read_now is not None else []
        outcome: dict[str, Any] = {"inverterId": inverter_id, "missingFields": [], "detail": ""}
        try:
            spec = getattr(spec_holder, "spec", None)
            if spec is None:
                specless_ticks += 1
                report_spec_unavailable(
                    model_id=str(cfg.get("model_id", "unknown")),
                    inverter_id=inverter_id,
                    consecutive=specless_ticks,
                    activity=activity,
                )
                outcome["outcome"] = "no_spec"
                return outcome
            if specless_ticks:
                # The cache filled in — retract the warning so the
                # diagnostics sensor stops accusing a healthy install.
                specless_ticks = 0
                if activity is not None and getattr(activity, "spec_problem", None):
                    activity.clear_spec_problem()
            missing: list[str] = []
            payload = await poll_once(
                hass=hass,
                spec=spec,
                cfg=cfg,
                inverter_id=inverter_id,
                store=store,
                missing_out=missing,
            )
            if payload is None:
                outcome["outcome"] = "incomplete"
                outcome["missingFields"] = missing
                if activity is not None:
                    activity.record_ingest_skipped(
                        missing_fields=missing, entities=dict.fromkeys(missing)
                    )
                return outcome
            outcome["outcome"] = "stored"
            if activity is not None:
                # Mirror the entity run_loop call: sample_count=1, period_sec
                # from the current cadence, summary from the payload so the
                # activity dashboard shows headline fields (parity with entity
                # publisher which passes _summary_of(payload)).
                activity.record_ingest_success(
                    sample_count=1,
                    period_sec=int(period_s),
                    summary=_summary_of(payload),
                )
            return outcome
        except Exception as exc:  # noqa: BLE001  — fail-soft, retry next tick
            _LOGGER.exception("harvest %s poll failed; backing off", inverter_id)
            reason = str(exc) or type(exc).__name__
            if activity is not None:
                activity.record_ingest_failure(reason=reason)
            outcome["outcome"] = "unreachable" if _is_unreachable(exc) else "failed"
            outcome["detail"] = reason
            return outcome
        finally:
            if read_now is not None:
                read_now.end_tick(waiters, outcome)

    stored_any = False
    first_retries = 0
    while alive():
        interval_s = _clamp_interval(float(cadence.interval_s))
        result = (await tick(interval_s))["outcome"]
        stored_any = stored_any or result == "stored"
        failed = result in ("unreachable", "failed")
        if not stored_any and (failed or result == "incomplete"):
            next_sleep_s = min(_first_reading_backoff(first_retries), interval_s)
            first_retries += 1
        elif failed:
            next_sleep_s = float(_DEFAULT_INTERVAL_S)
        else:
            next_sleep_s = interval_s

        if read_now is None:
            await asyncio.sleep(next_sleep_s)
            continue
        deadline = clock() + next_sleep_s
        while alive():
            remaining = deadline - clock()
            if remaining <= 0:
                break
            if await read_now.wait(remaining):
                extra = await tick(interval_s)
                stored_any = stored_any or extra["outcome"] == "stored"
    _LOGGER.info("Direct harvest loop stopped for inverter %s", inverter_id)
