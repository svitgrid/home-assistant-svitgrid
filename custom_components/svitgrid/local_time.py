"""Pure UTC <-> household-local calendar conversions.

The reading store is UTC all the way down: ``readings_raw.ts`` and
``readings_hourly.hour_start`` are absolute instants, and that is correct --
an instant has no timezone. But every "day" and "hour" a *user* sees (the
Day chart's 0:00-23:00 axis, a Month chart's day bars, "today's" summary) is
a HOUSEHOLD-LOCAL calendar day/hour. This module is the single, pure
conversion point between the two; nothing else should slice ISO strings to
derive a local date or hour.

Why it exists: the Day chart used to pass the panel's local anchor date
straight into a UTC window (``day + "T00:00:00Z"``) and then plot each
bucket at its UTC hour. For a Kyiv (UTC+3) household that served local
03:00 Jul 15 .. 03:00 Jul 16 and drew the whole curve three hours early --
the solar peak landed at 05:00 instead of 08:00.

DST is handled by construction, not by adding a fixed offset: a local day is
23, 24, or 25 hours long depending on the transition, and every window here
is computed as [local midnight, NEXT local midnight) so the true length
falls out naturally.

No I/O; pure functions only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")

_ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


def _zone(tz_name: str | None) -> ZoneInfo:
    """Resolve an IANA name to a ZoneInfo, degrading to UTC.

    ``hass.config.time_zone`` is user-controlled and travels through config
    imports and restores, so an unknown or empty value must never raise --
    the panel degrading to UTC bucketing (the pre-fix behaviour) is far
    better than the history endpoint 500ing.
    """
    if not tz_name:
        return UTC
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _parse_utc(ts: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp, tolerating sub-second precision.

    Readings carry values like ``2026-06-24T23:59:59.743Z``.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_ISO_UTC)


def local_day_window(day: str, tz_name: str | None) -> tuple[str, str]:
    """UTC half-open window ``[start, end)`` covering local calendar date *day*.

    Args:
        day: a household-local calendar date, ``YYYY-MM-DD``.
        tz_name: IANA timezone name (typically ``hass.config.time_zone``).
            Unknown/empty degrades to UTC.

    Returns:
        ``(start_utc_iso, end_utc_iso)`` -- both ``YYYY-MM-DDTHH:MM:SSZ``.
        The end bound is EXCLUSIVE and equals the next local midnight, so
        sub-second timestamps in the day's last second are included and the
        window is exactly as long as the local day really is (23/24/25h).

    Raises:
        ValueError: if *day* is not ``YYYY-MM-DD``.
    """
    zone = _zone(tz_name)
    parsed = datetime.strptime(day, "%Y-%m-%d")

    start_local = datetime(parsed.year, parsed.month, parsed.day, tzinfo=zone)
    # Add a day to the DATE, then re-attach the zone, so the offset is
    # resolved fresh on the far side of any DST transition. Adding
    # timedelta(days=1) to an aware datetime would carry the OLD offset
    # forward and land an hour off on transition days.
    next_date = parsed + timedelta(days=1)
    end_local = datetime(next_date.year, next_date.month, next_date.day, tzinfo=zone)

    return _fmt_utc(start_local), _fmt_utc(end_local)


def local_day_of(ts: str, tz_name: str | None) -> str:
    """The household-local calendar date (``YYYY-MM-DD``) containing UTC *ts*.

    The local-day counterpart of the store's ``_day_of`` UTC prefix slice.
    """
    return _parse_utc(ts).astimezone(_zone(tz_name)).strftime("%Y-%m-%d")


def local_hour_index(ts: str, tz_name: str | None) -> int | None:
    """The household-local wall-clock hour (0-23) of UTC *ts*.

    This is the x-axis index for the Day chart. Returns ``None`` for a
    timestamp that cannot be parsed, so callers can skip the bucket rather
    than plot it at a bogus hour.
    """
    try:
        return _parse_utc(ts).astimezone(_zone(tz_name)).hour
    except ValueError:
        return None
