"""Daily and weekly energy reports for island Telegram chats (svitgrid D-2152).

The add-on twin of the phone station's `island_summaries.dart`, itself a port
of the cloud's `send-daily-summary.ts` and `send-weekly-summary.ts`, which skip
a local-only island because the cloud holds no readings for it.

A report is due once per period and chat: yesterday's after the chat's
`dailyReportTime`, last week's (Monday to Sunday) after Monday 08:00, the
cloud's weekly schedule. Both run on the station's clock. A period with no
energy sends nothing, never a 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKLY_MINUTE = 8 * 60


@dataclass(frozen=True)
class DueSummary:
    """One report due now. `period` is the stamp that marks it sent: the day
    for a daily report, the week's Monday for a weekly one."""

    kind: str
    period: str
    from_day: str
    to_day: str

    @property
    def stamp_key(self) -> str:
        return "daily" if self.kind == "daily_summary" else "weekly"


def _local(now: datetime, tz: str | None) -> datetime:
    try:
        zone = ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = None
    return now.astimezone(zone) if zone is not None else now


def _minute_of(hhmm: Any) -> int:
    try:
        hours, minutes = str(hhmm).split(":")
        h, m = int(hours), int(minutes)
    except ValueError:
        return 8 * 60
    return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else 8 * 60


def due_summaries(
    prefs: dict[str, Any], stamps: dict[str, Any], now: datetime, tz: str | None
) -> list[DueSummary]:
    """The reports due at `now` for a chat whose sent stamps are `stamps`."""
    local = _local(now, tz)
    today: date = local.date()
    minute = local.hour * 60 + local.minute
    due: list[DueSummary] = []
    if prefs.get("dailyReport") is True and minute >= _minute_of(prefs.get("dailyReportTime")):
        day = (today - timedelta(days=1)).isoformat()
        if stamps.get("daily") != day:
            due.append(DueSummary("daily_summary", day, day, day))
    weekday = today.isoweekday()  # Monday = 1
    if prefs.get("weeklyReport") is True and (weekday != 1 or minute >= WEEKLY_MINUTE):
        monday = today - timedelta(days=weekday - 1 + 7)
        start = monday.isoformat()
        if stamps.get("weekly") != start:
            due.append(
                DueSummary("weekly_summary", start, start, (monday + timedelta(days=6)).isoformat())
            )
    return due


def _kwh(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def summary_totals(rows_by_inverter: list[list[dict]]) -> dict[str, float] | None:
    """A household's energy over the period's sealed days, in kWh.

    One list of `readings_daily` rows per inverter. PV counts 0 on a day whose
    PV peak is 0, the cloud's phantom-PV gate (`trustedPvEnergyKwh`). None when
    no day carries any energy.
    """
    pv = load = imp = exp = 0.0
    found = False
    for rows in rows_by_inverter:
        for row in rows:
            energy = row.get("energy") if isinstance(row, dict) else None
            if not isinstance(energy, dict) or not energy:
                continue
            found = True
            peaks = row.get("peaks") if isinstance(row.get("peaks"), dict) else {}
            if peaks.get("pvPower") != 0:
                pv += _kwh(energy.get("dailyPvEnergy"))
            load += _kwh(energy.get("dailyLoadEnergy"))
            imp += _kwh(energy.get("dailyGridImportEnergy"))
            exp += _kwh(energy.get("dailyGridExportEnergy"))
    if not found:
        return None
    return {"pv": pv, "load": load, "import": imp, "export": exp}
