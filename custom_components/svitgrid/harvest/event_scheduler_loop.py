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

Events of one inverter can overlap on a register. An event that starts while
another one holds the register takes that event's captured value as the
owner's, since its own read sees the other event's write. An event that ends
while another one still holds the register writes that event's value back, not
the owner's (event_evaluator._holder).

An activation that fails partway leaves the writes that landed, with their
priors kept. When the event stops trying to activate (window end, condition
gone, a cancel), the evaluator puts back exactly those writes.

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

from . import priority_profile as pp
from .event_evaluator import (
    NEEDS_CLOUD,
    SKIPPED_REASON_KEY,
    evaluate_event,
    has_landed_writes,
    needs_cloud,
    needs_settings,
    restore_decision,
)

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
    if isinstance(exc, PermissionError) and str(exc) == "unsigned_write":
        # An edge box refuses a write without the phone's signature: the
        # event was stored before 0.26.0 kept it (EdgeBoxExecutor).
        return "unsigned_write"
    text = str(exc)
    if text.startswith("prior_read_failed"):
        return "prev_read_failed"
    if text.startswith(("verify_failed", "verify_read_failed")):
        return "readback_mismatch"
    return "write_failed"


async def _dispatch_all(
    executor: object, commands: list, signing: dict | None = None
) -> tuple[_Failure | None, list]:
    """Dispatch every command.

    Returns ``(first failure or None, [(command, result), ...])`` for the
    commands that landed. A failed command does not stop the rest: they are
    independent writes, and a retry re-sends the whole list, which is
    idempotent. An empty list has nothing to fail.

    *signing* is the event's stored phone signature. Only an executor that
    relays the command to a device which checks it (an edge box) gets it, as
    in SvitgridCommandsView; every other executor is called as before.
    `type(...) is bool`, because a mock's attribute is truthy.
    """
    if not commands:
        return None, []
    if executor is None:
        return _Failure(commands[0][0], "inverter_not_found", None), []
    relays = getattr(executor, "relays_signature", None)
    kwargs = {"signing": signing} if type(relays) is bool and relays else {}
    failure: _Failure | None = None
    results: list = []
    for cmd_name, payload in commands:
        try:
            results.append((cmd_name, await executor.dispatch(cmd_name, payload, **kwargs)))
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


def _word(lo: int, hi: int):
    def parse(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return int(value) if value == int(value) and lo <= value <= hi else None

    return parse


# (ACK key, execution-state key, parse): the prior values an activation keeps
# for its restore. WriteExecutor names each ``prev<Field>`` after the payload
# field it wrote, so a key means the same register whichever command reported
# it: set_solar_sell with a work mode also reports ``prevWorkMode``. The
# execution-state keys are the cloud's (event_evaluator.COMMAND_PRIOR_KEYS).
_PRIOR_CAPTURES = (
    ("prevSlots", "prevSlots", lambda v: v or None),
    ("prevWorkMode", "previousWorkMode", _valid_work_mode),
    ("prevGridChargeEnabled", "previousTimerEnabled", _bit),
    ("prevSolarSell", "maintenancePriorSolarSell", _bit),
    ("prevSellPowerCapW", "previousSellPowerCapW", _word(0, 0xFFFF)),
    ("prevOn", "previousGenForce", _bit),
    ("prevGenForce", "previousGenForce", _bit),
    ("prevGenPortMode", "previousGenPortMode", _word(0, 2)),
    ("prevEnergyPattern", "previousEnergyPattern", _word(0, 1)),
)


def _handed_over(state_key: str, others) -> object | None:
    """The owner's value another event of the inverter already holds.

    An event that starts while another one has written the same register
    reads that event's write, not the owner's setting. The other event's
    prior is the owner's value, so it is taken instead, as the API's
    activateSellToGrid takes previousWorkMode from an active sell event.
    """
    for other in others or ():
        value = (other.get("executionState") or {}).get(state_key)
        if value is not None:
            return copy.deepcopy(value)
    return None


def capture_priors(state: dict, results: list, others=()) -> None:
    """Keep the prior values the activation's ACKs report, for the restore.

    WriteExecutor reads each register before it writes it and reports the
    value (``prevSlots`` for the slot table, ``prev<Field>`` otherwise). A
    value already held is kept: a later write while active, or a retry after a
    partial failure, reads the event's own setting back. A value another event
    in *others* holds is the owner's and wins over the read.
    """
    for name, result in results:
        if not isinstance(result, dict):
            continue
        if name == pp.APPLY_COMMAND:
            _capture_priority_ack(state, result)
            continue
        for ack_key, state_key, parse in _PRIOR_CAPTURES:
            if state.get(state_key) is not None:
                continue
            value = parse(result.get(ack_key))
            if value is None:
                continue
            handed = _handed_over(state_key, others)
            state[state_key] = value if handed is None else handed


def _capture_priority_ack(state: dict, result: dict) -> None:
    """Keep what an apply_priority_profile ack reports (buildPriorityProfileAckPatch).

    The box reads the owner's output and charger priority before it writes and
    returns them as ``prev``; they are kept only when nothing is held, since a
    repeated apply reads the profile's own write. The queued command's id
    (EdgeBoxExecutor's ``commandId``) always moves: the box now holds this
    apply, and the restore names it.
    """
    command_id = result.get("commandId")
    if isinstance(command_id, str) and command_id:
        state[pp.APPLY_CMD_ID_KEY] = command_id
    if pp.is_held(state.get(pp.PREV_KEY)):
        return
    prev = pp.parse_prev(result.get("prev"))
    if prev is not None:
        state[pp.PREV_KEY] = prev


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
    inverter_for: object = None,
    settings_for: object = None,
) -> None:
    """Single scheduler tick.

    Parameters
    ----------
    store        : ReadingStore — provides live_snapshot().
    event_store  : IslandEventStore — lists events + persists execution states.
    executor_for : callable(inverter_id: str) -> executor | None.
    tz           : IANA timezone string.
    now_utc      : Current UTC time (injectable for testing).
    inverter_for : callable(inverter_id: str) -> dict | None, the brand of an
                   inverter on an adopted edge box (brand_commands.edge_inverter);
                   None evaluates every inverter as one the integration writes.
    settings_for : async callable(inverter_id: str) -> dict | None, the settings
                   an adopted edge box posted for the inverter, which a PI30 or
                   MUST threshold rule reads the owner's voltages from.
    """
    # 1. List events; finish any cancel the app asked for first
    all_events = await event_store.async_list_events()
    for event in all_events:
        if (event.get("executionState") or {}).get(REMOVAL_KEY):
            try:
                await _settle(event_store, executor_for, event, now_utc, all_events)
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

    # Every event's latest state, so an event sees what the ones before it in
    # this tick wrote (overlap handoff and hand-back, see event_evaluator).
    latest = {e.get("id"): e for e in all_events}

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
            # Whether the event needs the cloud does not depend on a reading,
            # so the app learns it before the first one arrives.
            await _sync_skip_reason(event_store, latest, event, exec_state)
            continue

        others = _others(latest, event)
        # Only an inverter on an adopted edge box has a brand to pass; every
        # other one is evaluated with the same call as before.
        inverter = inverter_for(inverter_id) if inverter_for is not None else None
        brand = {"inverter": inverter} if inverter is not None else {}
        if inverter is not None and settings_for is not None and needs_settings(event):
            brand["settings"] = await settings_for(inverter_id)
        try:
            decision = evaluate_event(
                event,
                reading_entry["payload"],
                exec_state,
                now_utc,
                tz,
                others,
                **brand,
            )

            if decision.action not in ("activate", "update", "deactivate"):
                # Not a transition: a stale retry schedule has nothing left to retry.
                state = dict(decision.new_state)
                for key in _RETRY_KEYS:
                    state.pop(key, None)
                await _persist(event_store, latest, event, state)
                continue

            retry_at = _parse_iso(exec_state.get("dispatchRetryAt"))
            if retry_at is not None and now_utc < retry_at:
                continue  # backing off after a failed dispatch; state unchanged

            failure, results = await _dispatch_all(
                executor_for(inverter_id), decision.commands, event.get("signing")
            )
            if failure is None:
                state = dict(decision.new_state)
                for key in _RETRY_KEYS:
                    state.pop(key, None)
            else:
                state = _failed_state(exec_state, failure, now_utc)
                _log_failure(event_id, inverter_id, failure, state["dispatchFailures"])
            if decision.action != "deactivate":
                # A restore's ACK reads the event's own setting: never a prior.
                capture_priors(state, results, others)

            # Persist EVERY tick — guard survival + hysteresis bookkeeping
            await _persist(event_store, latest, event, state)

        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Event %s evaluation/dispatch failed; skipping to next event",
                event_id,
            )


def _others(events: dict, event: dict) -> list:
    """The other events of *event*'s inverter."""
    return [
        e
        for e in events.values()
        if e.get("id") != event.get("id") and e.get("inverterId") == event.get("inverterId")
    ]


async def _sync_skip_reason(event_store: object, latest: dict, event: dict, state: dict) -> None:
    """Set or clear `skippedReason` without evaluating; write only on a change."""
    want = NEEDS_CLOUD if needs_cloud(event) else None
    if state.get(SKIPPED_REASON_KEY) == want:
        return
    new_state = {k: v for k, v in state.items() if k != SKIPPED_REASON_KEY}
    if want is not None:
        new_state[SKIPPED_REASON_KEY] = want
    await _persist(event_store, latest, event, new_state)


async def _persist(event_store: object, latest: dict, event: dict, state: dict) -> None:
    await event_store.async_set_execution_state(event.get("id"), state)
    latest[event.get("id")] = {**event, "executionState": state}


async def request_removal(event_store: object, event_id: str, removal: str) -> str:
    """Record that the app deleted or disabled *event_id*.

    Returns ``"missing"`` for an unknown event, ``"done"`` when nothing of the
    event is on the inverter (it is deleted at once, and a disable needs no
    restore), and ``"pending"`` when something is: the event is active, or its
    activation failed partway and left writes. It is then marked
    ``pendingRemoval`` and ``settle_removal`` must run its restore. The mark
    resets any backoff, so the restore is tried at once.
    """
    event = await event_store.async_get_event(event_id)
    if event is None:
        return "missing"
    state = dict(event.get("executionState") or {})
    if not has_landed_writes(state):
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
    events = await event_store.async_list_events()
    await _settle(event_store, executor_for, event, now_utc or datetime.now(UTC), events)


async def _settle(
    event_store: object, executor_for: object, event: dict, now_utc: datetime, events=()
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
        others = _others({e.get("id"): e for e in events}, event)
        commands, done_state = restore_decision(event, exec_state, now_utc, others=others)
        inverter_id = event.get("inverterId")
        failure, _ = await _dispatch_all(executor_for(inverter_id), commands, event.get("signing"))
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
    inverter_for: object = None,
    settings_for: object = None,
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
    inverter_for : callable(inverter_id: str) -> dict | None, see ``_tick``.
    settings_for : async callable(inverter_id: str) -> dict | None, see ``_tick``.

    GATED: spawned ONLY when cloud_ingest_enabled=False.  With cloud-sync ON
    the cloud engine handles events; double-fire must not occur.
    """
    _LOGGER.info("Island event scheduler loop started (interval=%ss, tz=%s)", interval_s, tz)

    while not hass.is_stopping:
        try:
            await _tick(
                store,
                event_store,
                executor_for,
                tz,
                datetime.now(UTC),
                inverter_for,
                settings_for,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Island event scheduler tick failed unexpectedly; will retry next interval"
            )

        await asyncio.sleep(interval_s)

    _LOGGER.info("Island event scheduler loop stopped")
