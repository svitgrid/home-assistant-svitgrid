"""Priority-profile apply, renew and restore on an edge island (svitgrid#1254).

A PI30, MUST, LuxPower SNA, SMG II or SRNE inverter has no time-of-use table.
A calendar mode or a rule switches its output and charger priority instead:
``apply_priority_profile`` at the start, ``restore_priority_profile`` with the
owner's own values at the end. The box reads those values before it writes and
returns them in the apply's ack as ``prev``; the scheduler keeps them on the
event as ``priorityProfilePrev``, with the apply's command id as
``priorityApplyCmdId``, and the restore sends both back.

TS mapping (services/api/src/calendar/):
  PRIORITY_REVERT_MARGIN_S   → PRIORITY_REVERT_MARGIN_S   (priority-profile-dispatch.ts)
  revertAtFor                → revert_at_for
  occurrenceBounds           → occurrence_bounds
  priorityApplyWindow        → priority_apply_window
  priorityApplyPayload       → priority_apply_payload
  priorityRestorePayload     → priority_restore_payload
  decidePriorityProfile      → decide_priority_profile
  customPriorityRevertAt     → custom_priority_revert_at    (custom-priority-profile.ts)
  customPriorityRenewalDue   → custom_priority_renewal_due
  customPriorityApplyPayload → custom_priority_apply_payload
  parsePrev                  → parse_prev                   (priority-profile-snapshot.ts)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .brand_commands import profile_for_mode

APPLY_COMMAND = "apply_priority_profile"
RESTORE_COMMAND = "restore_priority_profile"

# The board reverts on its own this long after the window ends.
PRIORITY_REVERT_MARGIN_S = 600

# Maintenance fill counts as complete at this SoC, as the Deye path does.
MAINTENANCE_PRIORITY_TARGET_SOC = 99

# A rule's apply holds for at most six hours; the rule renews it once less
# than 30 minutes of the hold remain.
CUSTOM_PRIORITY_HOLD_MS = 6 * 60 * 60_000
CUSTOM_PRIORITY_RENEW_BEFORE_MS = 30 * 60_000

# The modes that run through a priority profile on a capable inverter.
PRIORITY_PROFILE_MODES = frozenset({"battery_charge", "battery_maintenance_charge", "use_battery"})

# The profiles each protocol offers (priority-profile.ts PROFILES_BY_PROTOCOL).
# An SNA has no battery_first.
PROFILES_BY_PROTOCOL = {
    "pi30": ("grid_charge", "battery_first"),
    "smg2": ("grid_charge", "battery_first"),
    "srne": ("grid_charge", "battery_first"),
    "luxpower": ("grid_charge", "grid_first"),
    "must": ("grid_charge", "battery_first"),
}

# Execution-state keys, the cloud's (CalendarEventDoc.executionState).
PREV_KEY = "priorityProfilePrev"
APPLY_CMD_ID_KEY = "priorityApplyCmdId"
REVERT_AT_KEY = "priorityRevertAt"
CONTROLLED_KEY = "priorityProfileControlled"
TARGET_REACHED_AT_KEY = "priorityTargetReachedAt"


def is_held(prev: object) -> bool:
    return isinstance(prev, dict)


def parse_prev(raw: object) -> dict | None:
    """The owner's values from an apply ack's ``result.prev``, or None."""
    if not isinstance(raw, dict):
        return None
    protocol = raw.get("protocol")
    out_p = raw.get("outputPriority")
    chg_p = raw.get("chargerPriority")
    if not isinstance(protocol, str):
        return None
    for value in (out_p, chg_p):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
    return {"protocol": protocol, "outputPriority": out_p, "chargerPriority": chg_p}


def revert_at_for(window_end_ms: float) -> int:
    """Epoch seconds at which the board reverts by itself."""
    return int(window_end_ms // 1000) + PRIORITY_REVERT_MARGIN_S


def _minutes_of(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _wall_clock_ms(day, hhmm: str, zone: ZoneInfo) -> float:
    h, m = hhmm.split(":")
    local = datetime(day.year, day.month, day.day, int(h), int(m), tzinfo=zone)
    return local.timestamp() * 1000


def occurrence_bounds(schedule: dict, now_utc: datetime, tz: str) -> tuple[float, float] | None:
    """``(start_ms, end_ms)`` of the occurrence that contains *now_utc*.

    An overnight window (end at or before start) ends on the next local day.
    None for a rule with no window.
    """
    start, end = schedule.get("startTime"), schedule.get("endTime")
    if not start or not end:
        return None
    zone = ZoneInfo(tz)
    local = now_utc.astimezone(zone)
    now_min = local.hour * 60 + local.minute
    start_min, end_min = _minutes_of(start), _minutes_of(end)
    start_day = end_day = local.date()
    if end_min <= start_min:
        if now_min >= start_min:
            end_day = start_day + timedelta(days=1)
        else:
            start_day = start_day - timedelta(days=1)
    return _wall_clock_ms(start_day, start, zone), _wall_clock_ms(end_day, end, zone)


def _hhmm(ms: float, tz: str) -> str:
    local = datetime.fromtimestamp(ms / 1000, ZoneInfo(tz))
    return f"{local.hour:02d}:{local.minute:02d}"


def priority_apply_window(start_ms: float, end_ms: float, tz: str) -> dict:
    """The window a LuxPower SNA writes into its window 3, local wall-clock.

    A start equal to its end is never sent: 12 hours or more is a whole day
    (00:00-23:59), a shorter one ends a minute after it starts.
    """
    start, end = _hhmm(start_ms, tz), _hhmm(end_ms, tz)
    if start != end:
        return {"start": start, "end": end}
    if end_ms - start_ms >= 12 * 3600_000:
        return {"start": "00:00", "end": "23:59"}
    return {"start": start, "end": _hhmm(start_ms + 60_000, tz)}


def priority_apply_payload(
    profile: str,
    revert_at: int,
    protocol: str | None,
    window: tuple[float, float, str],
) -> dict:
    """The ``apply_priority_profile`` payload; ``window`` for an SNA only.

    ``inverterId`` is left to the executor, which adds it to every command.
    """
    payload: dict = {"profile": profile, "revertAt": revert_at}
    if protocol == "luxpower":
        payload["window"] = priority_apply_window(*window)
    return payload


def priority_restore_payload(prev: dict, state: dict) -> dict:
    """The ``restore_priority_profile`` payload. ``cmdId`` names the apply whose
    prev this restores; left out when no apply id is recorded."""
    cmd_id = state.get(APPLY_CMD_ID_KEY)
    if isinstance(cmd_id, str) and cmd_id:
        return {"prev": prev, "cmdId": cmd_id}
    return {"prev": prev}


def decide_priority_profile(
    mode: str,
    in_window: bool,
    window_end_ms: float,
    soc: float | None,
    target_soc: float | None,
    prev: object,
    target_reached_at: float | None,
    occurrence_start_ms: float,
) -> dict:
    """Port of decidePriorityProfile: ``{"kind": "apply"|"restore"|"idle", ...}``."""
    held = is_held(prev)
    if not in_window:
        return {"kind": "restore", "reason": "window_end"} if held else {"kind": "idle"}
    profile = profile_for_mode(mode)
    if not profile:
        return {"kind": "idle"}
    # No re-arm in the same occurrence once the target was reached.
    if target_reached_at is not None and target_reached_at >= occurrence_start_ms:
        return {"kind": "restore", "reason": "target_reached"} if held else {"kind": "idle"}
    uses_soc = mode != "use_battery"
    if uses_soc and soc is not None and target_soc is not None and soc >= target_soc:
        if held:
            return {"kind": "restore", "reason": "target_reached"}
        return {"kind": "idle", "targetReached": True}
    return {"kind": "apply", "profile": profile, "revertAt": revert_at_for(window_end_ms)}


def custom_priority_revert_at(schedule: dict, now_utc: datetime, tz: str) -> int:
    """The window end or six hours from now, whichever comes first, plus the margin."""
    now_ms = now_utc.timestamp() * 1000
    cap_ms = now_ms + CUSTOM_PRIORITY_HOLD_MS
    bounds = occurrence_bounds(schedule, now_utc, tz)
    end_ms = min(bounds[1], cap_ms) if bounds and bounds[1] > now_ms else cap_ms
    return revert_at_for(end_ms)


def custom_priority_renewal_due(current_revert_at_s: object, next_revert_at_s: int, now_ms: float):
    """Less than 30 minutes of the current hold remain, and the next apply would
    hold longer. Near window end the next apply ends at the same moment, so the
    rule leaves the board to revert."""
    if isinstance(current_revert_at_s, bool) or not isinstance(current_revert_at_s, int | float):
        return False
    held_until_ms = (current_revert_at_s - PRIORITY_REVERT_MARGIN_S) * 1000
    return (
        held_until_ms - now_ms < CUSTOM_PRIORITY_RENEW_BEFORE_MS
        and next_revert_at_s > current_revert_at_s
    )


def custom_priority_apply_payload(
    profile: str,
    revert_at: int,
    protocol: str | None,
    now_utc: datetime,
    tz: str,
) -> dict:
    """A rule's apply payload. An SNA's window runs from now to the end of the hold."""
    end_ms = (revert_at - PRIORITY_REVERT_MARGIN_S) * 1000
    return priority_apply_payload(
        profile, revert_at, protocol, (now_utc.timestamp() * 1000, end_ms, tz)
    )
