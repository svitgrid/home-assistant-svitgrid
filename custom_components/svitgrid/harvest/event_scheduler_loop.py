"""Gated local event scheduler loop — pure island only (Task 5).

Each tick:
  1. List all enabled events from IslandEventStore.
  2. Get the latest reading per inverter via ReadingStore.live_snapshot().
  3. For each enabled event: pick its inverter's reading (skip if absent);
     call evaluate_event(event, reading, exec_state, now_utc, tz).
  4. On action in ('activate', 'deactivate'): dispatch each (cmd_name, payload)
     via executor_for(inverter_id).dispatch(cmd_name, payload).
  5. Persist decision.new_state via event_store.async_set_execution_state()
     EVERY tick — regardless of action — so activation guards and hysteresis
     bookkeeping survive across ticks.

Per-event try/except: a failure in one event's evaluation or dispatch is logged
and skipped; the remaining events in the same tick are still processed.

GATED: this coroutine is spawned ONLY when cloud_ingest_enabled=False (pure
island mode).  With cloud-sync ON the cloud engine handles calendar events;
double-fire must not occur.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .event_evaluator import evaluate_event

_LOGGER = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 60


async def _tick(
    store: object,
    event_store: object,
    executor_for: object,
    tz: str,
    now_utc: datetime,
) -> None:
    """Single scheduler tick.

    Parameters
    ----------
    store        : ReadingStore — provides live_snapshot().
    event_store  : IslandEventStore — lists events + persists execution states.
    executor_for : callable(inverter_id: str) -> executor | None.
    tz           : IANA timezone string.
    now_utc      : Current UTC time (injectable for testing).
    """
    # 1. List and filter events
    all_events = await event_store.async_list_events()
    enabled_events = [e for e in all_events if e.get("enabled", True)]
    if not enabled_events:
        return

    # 2. Live reading per inverter
    snapshot = await store.live_snapshot()
    reading_by_inverter = {r["inverterId"]: r for r in snapshot}

    # 3+4+5. Evaluate → dispatch → persist, one event at a time
    for event in enabled_events:
        event_id = event.get("id")
        inverter_id = event.get("inverterId")
        exec_state = event.get("executionState") or {}

        reading_entry = reading_by_inverter.get(inverter_id)
        if reading_entry is None:
            _LOGGER.debug(
                "Skipping event %s — no live reading for inverter %s",
                event_id,
                inverter_id,
            )
            continue

        try:
            decision = evaluate_event(
                event,
                reading_entry["payload"],
                exec_state,
                now_utc,
                tz,
            )

            # Dispatch commands only on activate / deactivate transitions
            if decision.action in ("activate", "deactivate"):
                executor = executor_for(inverter_id)
                if executor is not None:
                    for cmd_name, payload in decision.commands:
                        try:
                            await executor.dispatch(cmd_name, payload)
                        except Exception:  # noqa: BLE001
                            _LOGGER.exception(
                                "executor.dispatch(%s) failed for event %s; skipping command",
                                cmd_name,
                                event_id,
                            )
                else:
                    _LOGGER.warning(
                        "No executor for inverter %s (event %s); commands not dispatched",
                        inverter_id,
                        event_id,
                    )

            # Persist new_state EVERY tick — guard survival + hysteresis bookkeeping
            await event_store.async_set_execution_state(event_id, decision.new_state)

        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Event %s evaluation/dispatch failed; skipping to next event",
                event_id,
            )


async def run_event_scheduler_loop(
    hass: object,
    store: object,
    event_store: object,
    executor_for: object,
    tz: str,
    interval_s: int = _DEFAULT_INTERVAL_S,
) -> None:
    """Island-mode event scheduler loop.

    Parameters
    ----------
    hass         : HomeAssistant — provides hass.is_stopping exit sentinel.
    store        : ReadingStore — supplies live_snapshot().
    event_store  : IslandEventStore — source of events + target for state persists.
    executor_for : callable(inverter_id: str) -> executor | None.
                   In production this is ``executors_by_inverter.get`` (the dict's
                   bound method), so writes reach the per-inverter WriteExecutor or
                   YamlDispatcher — the same path the command poller uses.
    tz           : IANA timezone string from hass.config.time_zone.
    interval_s   : Tick cadence in seconds (default 60).

    GATED: spawned ONLY when cloud_ingest_enabled=False.  With cloud-sync ON
    the cloud engine handles events; double-fire must not occur.
    """
    _LOGGER.info(
        "Island event scheduler loop started (interval=%ss, tz=%s)", interval_s, tz
    )

    while not hass.is_stopping:
        try:
            await _tick(store, event_store, executor_for, tz, datetime.now(UTC))
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Island event scheduler tick failed unexpectedly; will retry next interval"
            )

        await asyncio.sleep(interval_s)

    _LOGGER.info("Island event scheduler loop stopped")
