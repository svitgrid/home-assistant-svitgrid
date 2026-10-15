"""Gated local event scheduler loop — pure island only (Task 5).

Each tick:
  1. List all enabled events from IslandEventStore.
  2. Get the latest reading per inverter via ReadingStore.live_snapshot().
  3. For each enabled event: pick its inverter's reading (skip if absent);
     call evaluate_event(event, reading, exec_state, now_utc, tz).
  4. On action in ('activate', 'update', 'deactivate'): dispatch each
     (cmd_name, payload) via executor_for(inverter_id).dispatch(cmd_name, payload).
  5. Persist the execution state via event_store.async_set_execution_state()
     EVERY tick — regardless of action — so activation guards and hysteresis
     bookkeeping survive across ticks.

A failed dispatch does NOT persist the decision's new state.  The prior state
is kept, with ``lastCommandError`` set and a retry scheduled
(``dispatchRetryAt``, backing off from 1 to 15 min), so the transition runs
again and the app can show why it has not.  The first failure logs at ERROR;
its retries log at DEBUG.

An activation keeps the prior values its ACKs report in the execution state,
and the restore writes exactly those back: the slot table a set_battery_charge
read (``prevSlots``, which ``restore_battery_charge`` writes back, as the cloud
does), and the work mode, TOU master bit and solar-sell bit that set_work_mode,
set_grid_charge_toggle and set_solar_sell read (``prevWorkMode``,
``prevGridChargeEnabled``, ``prevSolarSell``, the firmware's ACK keys). A value
already held is never replaced, and an ACK from a command that failed partway
still counts, so a retry that reads the event's own write back cannot pose as
the owner's setting. With no captured value the restore writes nothing and
reports ``prev_read_failed``.

Cancelling an active event from the app (DELETE, or a PUT that disables it)
must undo it at the inverter: the scheduler only evaluates events it still
has, so a plain delete left the charge running to its slot's end.
``request_removal`` marks the event ``pendingRemoval`` (``delete`` or
``disable``) and ``settle_removal`` runs the event's restore, the same one the
window end runs, then deletes or idles it. A failed restore keeps the mark,
with ``lastCommandError`` and the usual backoff, and every tick retries it. The
events list the app reads hides an event pending deletion.

Per-event try/except: a failure in one event's evaluation or dispatch is logged
and skipped; the remaining events in the same tick are still processed.

GATED: this coroutine is spawned ONLY when cloud_ingest_enabled=False (pure
island mode).  With cloud-sync ON the cloud engine handles calendar events;
double-fire must not occur.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .event_evaluator import evaluate_event, restore_decision

_LOGGER = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 15

# Retry a failed dispatch after 1 min, doubling up to 15 min. The tick is 15 s,
# and a write that failed once (a payload the recipe refuses, a busy bus) is
# not worth hammering the inverter with four times a minute.
_RETRY_FIRST_S = 60
_RETRY_MAX_S = 900
_RETRY_KEYS = ("dispatchFailures", "dispatchRetryAt")

# Execution-state key marking an event the app cancelled while it was active:
# "delete" or "disable". Set by request_removal, cleared by settle_removal.
REMOVAL_KEY = "pendingRemoval"
REMOVAL_DELETE = "delete"
REMOVAL_DISABLE = "disable"

# Event ids whose restore is running, so the view's immediate attempt and a
# tick never write the same restore at once.
_settling: set[str] = set()


@dataclass(frozen=True)
class _Failure:
    command: str
    reason: str
    error: BaseException | None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _reason_for(exc: BaseException) -> str:
    """Map an executor exception to a reason code the app already words.

    The codes are the board failure reasons the app translates
    (priority_profile_copy.dart); an unknown one reads as the generic text.
    """
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        return "bad_payload"
    if isinstance(exc, NotImplementedError):
        return "unsupported_field"
    text = str(exc)
    if text.startswith("prior_read_failed"):
        return "prev_read_failed"
    if text.startswith(("verify_failed", "verify_read_failed")):
        return "readback_mismatch"
    return "write_failed"


async def _dispatch_all(executor: object, commands: list) -> tuple[_Failure | None, list]:
    """Dispatch every command.

    Returns ``(first failure or None, [(command, result), ...])`` for the
    commands that landed. A failed command does not stop the rest: they are
    independent writes, and a retry re-sends the whole list, which is
    idempotent. An empty list has nothing to fail.
    """
    if not commands:
        return None, []
    if executor is None:
        return _Failure(commands[0][0], "inverter_not_found", None), []
    failure: _Failure | None = None
    results: list = []
    for cmd_name, payload in commands:
        try:
            results.append((cmd_name, await executor.dispatch(cmd_name, payload)))
        except Exception as exc:  # noqa: BLE001
            if failure is None:
                failure = _Failure(cmd_name, _reason_for(exc), exc)
    return failure, results


def _valid_work_mode(value: object) -> int | None:
    # Deye work modes are 0-2. Anything else is a corrupt read, which the API
    # also refuses to trust; the restore then reports it rather than guess.
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value in (0, 1, 2) else None


def _bit(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return bool(value)
    return None


# (command, ACK key, execution-state key, parse): the prior values an
# activation keeps for its restore. The execution-state keys are the cloud's.
_PRIOR_CAPTURES = (
    ("set_battery_charge", "prevSlots", "prevSlots", lambda v: v or None),
    ("set_work_mode", "prevWorkMode", "previousWorkMode", _valid_work_mode),
    ("set_grid_charge_toggle", "prevGridChargeEnabled", "previousTimerEnabled", _bit),
    ("set_solar_sell", "prevSolarSell", "maintenancePriorSolarSell", _bit),
)


def capture_priors(state: dict, results: list) -> None:
    """Keep the prior values the activation's ACKs report, for the restore.

    WriteExecutor reads each register before it writes it and reports the
    value (``prevSlots`` for the slot table, ``prev<Field>`` otherwise). A
    value already held is kept: a later write while active, or a retry after a
    partial failure, reads the event's own setting back.
    """
    for name, result in results:
        if not isinstance(result, dict):
            continue
        for command, ack_key, state_key, parse in _PRIOR_CAPTURES:
            if name != command or state.get(state_key) is not None:
                continue
            value = parse(result.get(ack_key))
            if value is not None:
                state[state_key] = value


def _failed_state(exec_state: dict, failure: _Failure, now_utc: datetime) -> dict:
    """The PRIOR state plus the failure, so the transition is retried.

    The decision's new state is dropped: an activation that never reached the
    inverter must not read as ``active``, and a restore that failed must keep
    the event ``active`` so the next tick tries it again.  ``lastCommandError``
    has the shape the cloud writes and the app already shows on the event.
    """
    failures = int(exec_state.get("dispatchFailures") or 0) + 1
    delay_s = min(_RETRY_FIRST_S * 2 ** (failures - 1), _RETRY_MAX_S)
    state = copy.deepcopy(exec_state)
    state["lastCommandError"] = {
        "command": failure.command,
        "reason": failure.reason,
        "at": now_utc.isoformat(),
    }
    state["dispatchFailures"] = failures
    state["dispatchRetryAt"] = (now_utc + timedelta(seconds=delay_s)).isoformat()
    return state


def _log_failure(event_id: object, inverter_id: object, failure: _Failure, failures: int) -> None:
    """Log the first failure of a run at ERROR with its traceback; retries at DEBUG."""
    if failures == 1:
        _LOGGER.error(
            "Event %s: %s for inverter %s failed (%s); retrying with backoff",
            event_id,
            failure.command,
            inverter_id,
            failure.reason,
            exc_info=failure.error,
        )
    else:
        _LOGGER.debug(
            "Event %s: %s retry %d failed (%s)",
            event_id,
            failure.command,
            failures,
            failure.reason,
        )


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
    # 1. List events; finish any cancel the app asked for first
    all_events = await event_store.async_list_events()
    for event in all_events:
        if (event.get("executionState") or {}).get(REMOVAL_KEY):
            try:
                await _settle(event_store, executor_for, event, now_utc)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Event %s: cancel restore failed", event.get("id"))
    enabled_events = [
        e
        for e in all_events
        if e.get("enabled", True) and not (e.get("executionState") or {}).get(REMOVAL_KEY)
    ]
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

            if decision.action not in ("activate", "update", "deactivate"):
                # Not a transition: a stale retry schedule has nothing left to retry.
                state = dict(decision.new_state)
                for key in _RETRY_KEYS:
                    state.pop(key, None)
                await event_store.async_set_execution_state(event_id, state)
                continue

            retry_at = _parse_iso(exec_state.get("dispatchRetryAt"))
            if retry_at is not None and now_utc < retry_at:
                continue  # backing off after a failed dispatch; state unchanged

            failure, results = await _dispatch_all(executor_for(inverter_id), decision.commands)
            if failure is None:
                state = dict(decision.new_state)
                for key in _RETRY_KEYS:
                    state.pop(key, None)
            else:
                state = _failed_state(exec_state, failure, now_utc)
                _log_failure(event_id, inverter_id, failure, state["dispatchFailures"])
            if decision.action != "deactivate":
                # A restore's ACK reads the event's own setting: never a prior.
                capture_priors(state, results)

            # Persist EVERY tick — guard survival + hysteresis bookkeeping
            await event_store.async_set_execution_state(event_id, state)

        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Event %s evaluation/dispatch failed; skipping to next event",
                event_id,
            )


async def request_removal(event_store: object, event_id: str, removal: str) -> str:
    """Record that the app deleted or disabled *event_id*.

    Returns ``"missing"`` for an unknown event, ``"done"`` when nothing is
    running at the inverter (an inactive event is deleted at once, and a
    disable needs no restore), and ``"pending"`` when the event is active: it
    is marked ``pendingRemoval`` and ``settle_removal`` must run its restore.
    The mark resets any backoff, so the restore is tried at once.
    """
    event = await event_store.async_get_event(event_id)
    if event is None:
        return "missing"
    state = dict(event.get("executionState") or {})
    if state.get("status") != "active":
        if removal == REMOVAL_DELETE:
            await event_store.async_delete_event(event_id)
        return "done"
    state[REMOVAL_KEY] = removal
    for key in _RETRY_KEYS:
        state.pop(key, None)
    await event_store.async_set_execution_state(event_id, state)
    return "pending"


async def settle_removal(
    event_store: object,
    executor_for: object,
    event_id: str,
    now_utc: datetime | None = None,
) -> None:
    """Run the restore of a cancelled event, then delete or idle it."""
    event = await event_store.async_get_event(event_id)
    if event is None:
        return
    await _settle(event_store, executor_for, event, now_utc or datetime.now(UTC))


async def _settle(
    event_store: object, executor_for: object, event: dict, now_utc: datetime
) -> None:
    event_id = event.get("id")
    exec_state = event.get("executionState") or {}
    removal = exec_state.get(REMOVAL_KEY)
    if not removal or event_id in _settling:
        return
    retry_at = _parse_iso(exec_state.get("dispatchRetryAt"))
    if retry_at is not None and now_utc < retry_at:
        return
    _settling.add(event_id)
    try:
        commands, done_state = restore_decision(event, exec_state, now_utc)
        inverter_id = event.get("inverterId")
        failure, _ = await _dispatch_all(executor_for(inverter_id), commands)
        if failure is not None:
            state = _failed_state(exec_state, failure, now_utc)
            _log_failure(event_id, inverter_id, failure, state["dispatchFailures"])
            await event_store.async_set_execution_state(event_id, state)
            return
        if removal == REMOVAL_DELETE:
            await event_store.async_delete_event(event_id)
            _LOGGER.info("Event %s: cancelled, restored and deleted", event_id)
            return
        for key in (REMOVAL_KEY, *_RETRY_KEYS):
            done_state.pop(key, None)
        await event_store.async_set_execution_state(event_id, done_state)
        _LOGGER.info("Event %s: disabled and restored", event_id)
    finally:
        _settling.discard(event_id)


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
    interval_s   : Tick cadence in seconds (default 15).

                   The tick interval bounds activation latency — with a 1-min
                   sustain gate the effective minimum activation latency is
                   ~1-2× interval_s.  Lower interval_s (e.g. 15 s, the
                   default) for responsive gen-force/outage handling.  This is
                   the knob to tune if local automations feel sluggish.

    GATED: spawned ONLY when cloud_ingest_enabled=False.  With cloud-sync ON
    the cloud engine handles events; double-fire must not occur.
    """
    _LOGGER.info("Island event scheduler loop started (interval=%ss, tz=%s)", interval_s, tz)

    while not hass.is_stopping:
        try:
            await _tick(store, event_store, executor_for, tz, datetime.now(UTC))
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Island event scheduler tick failed unexpectedly; will retry next interval"
            )

        await asyncio.sleep(interval_s)

    _LOGGER.info("Island event scheduler loop stopped")
