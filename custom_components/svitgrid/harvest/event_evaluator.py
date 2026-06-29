"""Local port of the cloud's isEventInWindow (evaluator.ts:385-427).

Parity with the TypeScript is the contract — a divergence means island events
fire at different times than the cloud would.

TS mapping:
  parseTimeToMinutes  → parse_time_to_minutes
  getLocalParts       → datetime.astimezone(ZoneInfo(tz)) + manual weekday remap
  isEventInWindow     → is_event_in_window
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def parse_time_to_minutes(hhmm: str) -> int:
    """Convert "HH:mm" to minutes since midnight.

    Mirrors TS: const [h, m] = time.split(':').map(Number); return h * 60 + m;
    """
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_event_in_window(schedule: dict, now_utc: datetime, tz: str) -> bool:
    """Return True when now_utc falls within the event's scheduled window.

    Faithful port of TS isEventInWindow (evaluator.ts:385-427).

    Local-parts resolution (mirrors TS getLocalParts via Intl.DateTimeFormat):
      - Convert now_utc → local wall-clock via zoneinfo.ZoneInfo(tz).
      - weekday: 0=Sun..6=Sat (matches TS weekdayMap Sun:0 Mon:1 … Sat:6).
        Python datetime.weekday() is 0=Mon..6=Sun, so we remap:
        local_weekday = (python_weekday + 1) % 7

    Date bounds (TS lines 391-392):
      - startDate present → localDate >= startDate (ISO string compare)
      - endDate   present → localDate <= endDate

    Recurrence (TS lines 394-412):
      - 'none'          → only on startDate
      - 'daily'         → always active within date range
      - 'weekly'/'custom' → only when local weekday is in weekdays[];
                           d % 7 collapses both 0=Sun (legacy) and 1=Mon Dart
                           conventions onto 0=Sun..6=Sat so the compare is
                           convention-agnostic (TS comment lines 405-408).

    Time window (TS lines 415-426):
      - Normal: startMinutes <= nowMinutes < endMinutes
      - Overnight wrap (endMinutes <= startMinutes, e.g. 23:00-07:00):
        nowMinutes >= startMinutes OR nowMinutes < endMinutes
    """
    # ── Resolve local parts ────────────────────────────────────────────────────
    local = now_utc.astimezone(ZoneInfo(tz))
    local_date_str = local.strftime("%Y-%m-%d")
    local_hours = local.hour
    local_minutes = local.minute
    # Remap Python Mon=0..Sun=6 → Sun=0..Sat=6 (matches TS weekdayMap)
    local_weekday = (local.weekday() + 1) % 7

    # ── Date bounds ────────────────────────────────────────────────────────────
    start_date = schedule.get("startDate")
    end_date = schedule.get("endDate")
    if start_date and local_date_str < start_date:
        return False
    if end_date and local_date_str > end_date:
        return False

    # ── Recurrence ────────────────────────────────────────────────────────────
    recurrence = schedule.get("recurrence", "daily")
    if recurrence == "none":
        # Only fires on its startDate
        if start_date and start_date != local_date_str:
            return False
    elif recurrence == "daily":
        pass  # Always active within date range
    elif recurrence in ("weekly", "custom"):
        weekdays = schedule.get("weekdays") or []
        if weekdays and not any(d % 7 == local_weekday for d in weekdays):
            return False

    # ── Time window ───────────────────────────────────────────────────────────
    now_minutes = local_hours * 60 + local_minutes
    start_minutes = parse_time_to_minutes(schedule["startTime"])
    end_minutes = parse_time_to_minutes(schedule["endTime"])

    if end_minutes <= start_minutes:
        # Overnight wrap: [start, 24:00) ∪ [00:00, end)
        # e.g. start=23:00 end=07:00 → active at 23:30 (>=1380) or 02:00 (<420)
        return now_minutes >= start_minutes or now_minutes < end_minutes

    return now_minutes >= start_minutes and now_minutes < end_minutes
