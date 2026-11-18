"""Telegram message copy for island stations.

The strings come from ``copy/island_telegram_copy.json``, vendored from the
monorepo by ``scripts/sync-island-telegram-fixtures.sh``. Output is Telegram
HTML, so every substituted value goes through ``html.escape``.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from importlib import resources
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Kind = Literal[
    "outage",
    "grid_restored",
    "low_battery",
    "critical_battery",
    "battery_full",
    "offline",
    "offline_board",
    "offline_outage",
    "offline_board_recovered",
    "calendar_event_started",
    "calendar_event_ended",
    "test",
    "linked",
]

_FIXTURE = json.loads(
    resources.files(__package__)
    .joinpath("copy/island_telegram_copy.json")
    .read_text(encoding="utf-8")
)
_COPY: dict[str, dict[str, str]] = _FIXTURE["copy"]
_FALLBACK: str = _FIXTURE["fallback"]


def _strings(language: str | None) -> dict[str, str]:
    """Exact code, then the part before '-' or '_', then the fallback."""
    if language:
        code = language.strip().lower()
        if code in _COPY:
            return _COPY[code]
        base = code.replace("_", "-").split("-", 1)[0]
        if base in _COPY:
            return _COPY[base]
    return _COPY[_FALLBACK]


def open_button(language: str | None) -> str:
    return _strings(language)["open_button"]


def _zone(tz: str | None):
    if tz:
        try:
            return ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return UTC


def _time_line(event_at: datetime, tz: str | None, now: datetime | None) -> str:
    zone = _zone(tz)
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=UTC)
    local = event_at.astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    prefix = "" if local.date() == today else local.strftime("%d.%m ")
    return f"\n🕑 {prefix}{local.strftime('%H:%M')}"


def render(
    kind: Kind,
    language: str | None,
    *,
    station: str,
    soc: int | None = None,
    event_at: datetime | None = None,
    tz: str | None = None,
    now: datetime | None = None,
    minutes_ago: int | None = None,
) -> str:
    """Return the Telegram HTML for one message."""
    strings = _strings(language)
    values = {
        "inverterName": html.escape(station),
        "batterySoc": "" if soc is None else html.escape(str(soc)),
        "minutesAgo": "" if minutes_ago is None else html.escape(str(minutes_ago)),
    }

    def fill(key: str) -> str:
        text = strings[key]
        for name, value in values.items():
            text = text.replace("{" + name + "}", value)
        return text

    if kind == "linked":
        text = fill("linked")
    else:
        text = f"<b>{fill(kind + '_title')}</b>\n{fill(kind + '_body')}"
    if event_at is not None:
        text += _time_line(event_at, tz, now)
    return text


def render_rule_notice(
    kind: str,
    language: str | None,
    *,
    name: str | None,
    rule: bool,
    end_time: str | None,
) -> str:
    """The Telegram HTML for a rule's start or stop notice (svitgrid D-2151).

    The cloud's Telegram headline names the event when it has a name. An
    unnamed rule takes the cloud's rule title, and any other unnamed event the
    catalogue's title. A scheduled event's start carries its end time, the
    cloud's «until» fact.
    """
    strings = _strings(language)
    named = (name or "").strip()
    if named:
        title = strings[kind + "_named"].replace("{eventName}", html.escape(named))
    elif rule:
        title = strings["calendar_rule_title"]
    else:
        title = strings[kind + "_title"]
    text = f"<b>{title}</b>\n{strings[kind + '_body']}"
    if kind == "calendar_event_started" and not rule and end_time:
        text += f"\n{strings['calendar_until']}: {html.escape(end_time)}"
    return text
