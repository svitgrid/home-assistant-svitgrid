"""Tests for is_event_in_window — faithful port of TS isEventInWindow (evaluator.ts:385-427).

Each test cites the TS rule it encodes. All `now_utc` values are fixed datetimes with
UTC tzinfo; expected local times are derived by hand from UTC+3 (EEST, Europe/Kyiv).
"""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.svitgrid.harvest.event_evaluator import (
    is_event_in_window,
    parse_time_to_minutes,
)

TZ = "Europe/Kyiv"  # UTC+3 EEST in summer 2026


# ── parse_time_to_minutes ──────────────────────────────────────────────────────


def test_parse_time_midnight():
    # TS: h * 60 + m
    assert parse_time_to_minutes("00:00") == 0


def test_parse_time_noon():
    assert parse_time_to_minutes("12:00") == 720


def test_parse_time_23_00():
    assert parse_time_to_minutes("23:00") == 1380


def test_parse_time_07_30():
    assert parse_time_to_minutes("07:30") == 450


# ── daily recurrence, time-window check ───────────────────────────────────────


def test_daily_inside_window():
    """TS rule: daily → always active in range; startMinutes<=now<endMinutes → True.
    09:00 UTC = 12:00 Kyiv (UTC+3); window 10:00-15:00 → in.
    """
    now_utc = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_daily_outside_window_after():
    """TS rule: nowMinutes (960) >= endMinutes (900) and not wrap → False.
    13:00 UTC = 16:00 Kyiv; window 10:00-15:00 → out.
    """
    now_utc = datetime(2026, 6, 25, 13, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_daily_outside_window_before():
    """nowMinutes (540) < startMinutes (600) → False.
    06:00 UTC = 09:00 Kyiv; window 10:00-15:00 → out.
    """
    now_utc = datetime(2026, 6, 25, 6, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_daily_exactly_at_start():
    """Boundary: nowMinutes == startMinutes → included (>=).
    07:00 UTC = 10:00 Kyiv; window 10:00-15:00.
    """
    now_utc = datetime(2026, 6, 25, 7, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_daily_exactly_at_end():
    """Boundary: nowMinutes == endMinutes → excluded (<).
    12:00 UTC = 15:00 Kyiv; window 10:00-15:00.
    """
    now_utc = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


# ── overnight wrap ─────────────────────────────────────────────────────────────


def test_overnight_inside_after_midnight():
    """TS rule: endMinutes<=startMinutes → wrap; nowMinutes<endMinutes → True.
    21:30 UTC = 00:30 Kyiv (next day); window 23:00-07:00 → in (00:30 < 07:00).
    """
    now_utc = datetime(2026, 6, 25, 21, 30, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "23:00",
        "endTime": "07:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_overnight_inside_before_midnight():
    """TS rule: wrap; nowMinutes>=startMinutes → True.
    21:00 UTC = 00:00+3 = ... wait, 21:00 UTC = 00:00 Kyiv next day.
    Use 20:30 UTC = 23:30 Kyiv; 23:30 >= 23:00 → True.
    """
    now_utc = datetime(2026, 6, 25, 20, 30, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "23:00",
        "endTime": "07:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_overnight_outside_midday():
    """TS rule: wrap; nowMinutes (480) not >=1380 and not <420 → False.
    05:00 UTC = 08:00 Kyiv; window 23:00-07:00 → out (08:00 not in wrap band).
    """
    now_utc = datetime(2026, 6, 25, 5, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "23:00",
        "endTime": "07:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_overnight_exactly_at_start_included():
    """Boundary wrap: nowMinutes==startMinutes (1380) → included.
    20:00 UTC = 23:00 Kyiv; window 23:00-07:00.
    """
    now_utc = datetime(2026, 6, 25, 20, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "23:00",
        "endTime": "07:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_overnight_exactly_at_end_excluded():
    """Boundary wrap: nowMinutes==endMinutes (420) → excluded.
    04:00 UTC = 07:00 Kyiv; window 23:00-07:00.
    """
    now_utc = datetime(2026, 6, 25, 4, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startTime": "23:00",
        "endTime": "07:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


# ── weekly recurrence + weekday convention ─────────────────────────────────────


def test_weekly_on_listed_weekday_in_window():
    """TS rule: weekly/custom → weekdays[].some(d => d%7 === local.weekday).
    weekdays=[1,3,5] uses Dart 1=Mon convention; 1%7=1 matches Mon local.weekday.
    2026-06-29 is Monday; 09:00 UTC = 12:00 Kyiv → in window 10:00-15:00.
    """
    now_utc = datetime(2026, 6, 29, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "weekly",
        "weekdays": [1, 3, 5],  # Dart: Mon=1, Wed=3, Fri=5
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_weekly_on_unlisted_weekday():
    """TS rule: none of weekdays[] match → False.
    2026-06-30 is Tuesday; local.weekday=2; [1,3,5] no 2 → False.
    """
    now_utc = datetime(2026, 6, 30, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "weekly",
        "weekdays": [1, 3, 5],
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_weekly_dart_sunday_7_collapses_to_0():
    """TS rule: d%7 collapses Dart Sun=7 onto 0 (same as legacy 0=Sun).
    2026-06-28 is Sunday; local.weekday=0; weekdays=[7]; 7%7=0==0 → True.
    """
    now_utc = datetime(2026, 6, 28, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "weekly",
        "weekdays": [7],  # Dart Sun=7; 7%7=0
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_custom_recurrence_same_as_weekly():
    """TS rule: 'custom' branch identical to 'weekly'."""
    now_utc = datetime(2026, 6, 29, 9, 0, 0, tzinfo=UTC)  # Monday
    schedule = {
        "recurrence": "custom",
        "weekdays": [1],
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


# ── none recurrence ────────────────────────────────────────────────────────────


def test_none_recurrence_fires_on_start_date():
    """TS rule: recurrence=none → only fires when localDate==startDate.
    09:00 UTC = 12:00 Kyiv on 2026-06-25; startDate=2026-06-25 → True.
    """
    now_utc = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "none",
        "startDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_none_recurrence_silent_next_day():
    """TS rule: recurrence=none; localDate != startDate → False.
    09:00 UTC = 12:00 Kyiv on 2026-06-26; startDate=2026-06-25 → False.
    """
    now_utc = datetime(2026, 6, 26, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "none",
        "startDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_none_recurrence_silent_previous_day():
    """TS rule: startDate bound also applies; localDate < startDate → False."""
    now_utc = datetime(2026, 6, 24, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "none",
        "startDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


# ── startDate / endDate bounds ─────────────────────────────────────────────────


def test_start_date_bound_before_start():
    """TS rule: startDate set, localDate < startDate → False.
    09:00 UTC = 12:00 Kyiv on 2026-06-24; startDate=2026-06-25.
    """
    now_utc = datetime(2026, 6, 24, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_start_date_bound_on_start_date():
    """TS rule: localDate == startDate → date bound passes.
    09:00 UTC = 12:00 Kyiv on 2026-06-25; startDate=2026-06-25 → True.
    """
    now_utc = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_end_date_bound_after_end():
    """TS rule: endDate set, localDate > endDate → False.
    09:00 UTC = 12:00 Kyiv on 2026-06-26; endDate=2026-06-25.
    """
    now_utc = datetime(2026, 6, 26, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "endDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False


def test_end_date_bound_on_end_date():
    """TS rule: localDate == endDate → date bound passes.
    09:00 UTC = 12:00 Kyiv on 2026-06-25; endDate=2026-06-25 → True.
    """
    now_utc = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "endDate": "2026-06-25",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_both_date_bounds_in_range():
    """TS rule: both startDate+endDate present; localDate inside range → passes bounds."""
    now_utc = datetime(2026, 6, 25, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startDate": "2026-06-20",
        "endDate": "2026-06-30",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is True


def test_both_date_bounds_out_of_range():
    """TS rule: both startDate+endDate present; localDate outside range → False."""
    now_utc = datetime(2026, 7, 5, 9, 0, 0, tzinfo=UTC)
    schedule = {
        "recurrence": "daily",
        "startDate": "2026-06-20",
        "endDate": "2026-06-30",
        "startTime": "10:00",
        "endTime": "15:00",
    }
    assert is_event_in_window(schedule, now_utc, TZ) is False
