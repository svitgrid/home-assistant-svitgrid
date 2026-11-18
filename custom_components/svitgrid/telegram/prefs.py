"""What one island Telegram chat wants to receive (svitgrid D-2150).

The names and defaults are the cloud's (`notification-preferences-read.ts`),
so a move back to the cloud can carry them: `mutedAlerts` lists the alert
types the chat turned off, `dailyReport` and `weeklyReport` turn on the energy
reports, `dailyReportTime` is when the daily one goes out, and quiet hours
hold back every type that is not urgent, as the cloud's `sendAlertToHousehold`
does: it drops them, it does not send them later.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS: dict[str, Any] = {
    "mutedAlerts": [],
    "dailyReport": False,
    "dailyReportTime": "08:00",
    "weeklyReport": False,
    "quietHoursEnabled": False,
    "quietHoursStart": "22:00",
    "quietHoursEnd": "07:00",
}

_BOOLS = ("dailyReport", "weeklyReport", "quietHoursEnabled")
_TIMES = ("dailyReportTime", "quietHoursStart", "quietHoursEnd")
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_SLUG_RE = re.compile(r"[a-z0-9_]{1,60}")
_MAX_MUTED = 50

# The cloud's alert type for each island message kind. The liveness kinds are
# the cloud's `offline` type under another copy.
ALERT_TYPE = {
    "offline_board": "offline",
    "offline_outage": "offline",
    "offline_board_recovered": "offline",
}

# The types the cloud sends through quiet hours (`urgentTypes` in
# functions/src/notifications.ts).
URGENT_TYPES = frozenset(
    {
        "outage",
        "error",
        "offline",
        "island_silent",
        "inverter_unreachable",
        "inverter_unreachable_logger",
        "inverter_unreachable_inverter",
        "inverter_unreachable_config",
    }
)


def merged(stored: Any) -> dict[str, Any]:
    """The stored values over the defaults. A stored value of the wrong type
    reads as its default."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    if not isinstance(stored, dict):
        return out
    muted = stored.get("mutedAlerts")
    if isinstance(muted, list):
        out["mutedAlerts"] = [m for m in muted if isinstance(m, str)]
    for key in _BOOLS:
        if isinstance(stored.get(key), bool):
            out[key] = stored[key]
    for key in _TIMES:
        value = stored.get(key)
        if isinstance(value, str) and _TIME_RE.fullmatch(value):
            out[key] = value
    return out


def validate_patch(body: Any) -> dict[str, Any] | None:
    """The fields of a `PATCH /me/prefs` body, or None when any is unknown or bad."""
    if not isinstance(body, dict) or not body:
        return None
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "mutedAlerts":
            if (
                not isinstance(value, list)
                or len(value) > _MAX_MUTED
                or not all(isinstance(v, str) and _SLUG_RE.fullmatch(v) for v in value)
            ):
                return None
            out[key] = sorted(set(value), key=value.index)
        elif key in _BOOLS:
            if not isinstance(value, bool):
                return None
            out[key] = value
        elif key in _TIMES:
            if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
                return None
            out[key] = value
        else:
            return None
    return out


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _local_minutes(now: datetime, tz: str | None) -> int:
    try:
        zone = ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = None
    local = now.astimezone(zone) if zone is not None else now
    return local.hour * 60 + local.minute


def in_quiet_hours(prefs: dict[str, Any], now: datetime, tz: str | None) -> bool:
    """`isInQuietHours`: the station's local clock inside the chat's window."""
    if prefs.get("quietHoursEnabled") is not True:
        return False
    start = _minutes(prefs["quietHoursStart"])
    end = _minutes(prefs["quietHoursEnd"])
    current = _local_minutes(now, tz)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def wants(prefs: dict[str, Any], kind: str, now: datetime, tz: str | None) -> bool:
    """Whether a chat with `prefs` gets an alert of `kind` now."""
    alert_type = ALERT_TYPE.get(kind, kind)
    if alert_type in prefs.get("mutedAlerts", ()):
        return False
    return alert_type in URGENT_TYPES or not in_quiet_hours(prefs, now, tz)
