"""Tests for the pure UTC<->household-local time helpers.

The store buckets everything by UTC (``ts[:10]`` / ``ts[:13]``), but every
user-facing "day" and "hour" in the panel is a HOUSEHOLD-LOCAL calendar
day/hour. These helpers are the single conversion point between the two.

The bug they exist to prevent: a Kyiv (UTC+3) household asking for the Day
chart of 2026-07-15 was served the UTC window 00:00Z..24:00Z, i.e. LOCAL
03:00 Jul 15 .. 03:00 Jul 16 -- the first three local hours missing, three
hours of the next morning folded onto the right edge -- and each bucket was
then plotted at its UTC hour, shifting the whole solar curve 3h earlier.
"""

import pytest

from custom_components.svitgrid.local_time import (
    local_day_of,
    local_day_window,
    local_hour_index,
)

KYIV = "Europe/Kyiv"


# --------------------------------------------------------------------- #
# local_day_window
# --------------------------------------------------------------------- #


def test_window_for_utc_plus_3_starts_at_local_midnight():
    """Kyiv is UTC+3 in July: local 2026-07-15 00:00 == 2026-07-14T21:00Z."""
    start, end = local_day_window("2026-07-15", KYIV)
    assert start == "2026-07-14T21:00:00Z"
    assert end == "2026-07-15T21:00:00Z"


def test_window_is_half_open_exclusive_end():
    """End is the NEXT local midnight, exclusive -- readings carry sub-second
    ts, so an inclusive '23:59:59' bound would drop the last second."""
    start, end = local_day_window("2026-07-15", KYIV)
    _, next_end = local_day_window("2026-07-16", KYIV)
    assert end == local_day_window("2026-07-16", KYIV)[0]
    assert next_end > end
    assert start < end


def test_window_for_utc_is_plain_midnight_to_midnight():
    """UTC households keep exactly the old behaviour."""
    assert local_day_window("2026-07-15", "UTC") == (
        "2026-07-15T00:00:00Z",
        "2026-07-16T00:00:00Z",
    )


def test_window_spans_25_hours_on_dst_fall_back():
    """Europe/Kyiv falls back 2026-10-25: that local day is 25 hours long.
    A fixed +3 offset would silently drop the extra hour."""
    start, end = local_day_window("2026-10-25", KYIV)
    assert start == "2026-10-24T21:00:00Z"
    assert end == "2026-10-25T22:00:00Z"  # 25h later, offset now +2


def test_window_spans_23_hours_on_dst_spring_forward():
    """Europe/Kyiv springs forward 2026-03-29: that local day is 23 hours."""
    start, end = local_day_window("2026-03-29", KYIV)
    assert start == "2026-03-28T22:00:00Z"  # offset +2 before the switch
    assert end == "2026-03-29T21:00:00Z"  # 23h later, offset now +3


def test_window_rejects_malformed_day():
    with pytest.raises(ValueError):
        local_day_window("15-07-2026", KYIV)


def test_window_falls_back_to_utc_on_unknown_timezone():
    """An unknown tz must degrade to UTC, never raise -- hass.config.time_zone
    is user-controlled and a bad value must not take the panel down."""
    assert local_day_window("2026-07-15", "Mars/Olympus_Mons") == (
        "2026-07-15T00:00:00Z",
        "2026-07-16T00:00:00Z",
    )


# --------------------------------------------------------------------- #
# local_day_of
# --------------------------------------------------------------------- #


def test_local_day_of_shifts_late_utc_evening_into_next_local_day():
    """22:00Z on Jul 14 is already 01:00 local on Jul 15 in Kyiv."""
    assert local_day_of("2026-07-14T22:00:00Z", KYIV) == "2026-07-15"


def test_local_day_of_keeps_same_day_midafternoon():
    assert local_day_of("2026-07-15T12:00:00Z", KYIV) == "2026-07-15"


def test_local_day_of_handles_subsecond_timestamps():
    assert local_day_of("2026-07-14T23:59:59.743Z", KYIV) == "2026-07-15"


def test_local_day_of_utc_is_plain_prefix():
    assert local_day_of("2026-07-15T00:30:00Z", "UTC") == "2026-07-15"


# --------------------------------------------------------------------- #
# local_hour_index
# --------------------------------------------------------------------- #


def test_local_hour_index_is_the_wall_clock_hour():
    """The reported bug in one assertion: the 05:00Z solar bucket belongs at
    08:00 on a Kyiv household's chart, not at 05:00."""
    assert local_hour_index("2026-07-15T05:00:00Z", KYIV) == 8


def test_local_hour_index_wraps_across_midnight():
    assert local_hour_index("2026-07-14T22:00:00Z", KYIV) == 1


def test_local_hour_index_utc_is_plain_slice():
    assert local_hour_index("2026-07-15T05:00:00Z", "UTC") == 5


def test_local_hour_index_returns_none_for_malformed_timestamp():
    assert local_hour_index("not-a-timestamp", KYIV) is None
