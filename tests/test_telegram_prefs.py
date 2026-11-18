"""What one island Telegram chat wants to receive (svitgrid D-2150)."""

from datetime import UTC, datetime

from custom_components.svitgrid.telegram import prefs

KYIV = "Europe/Kyiv"
# 2026-10-30 21:30 UTC is 23:30 in Kyiv (EET, UTC+2).
LATE = datetime(2026, 10, 30, 21, 30, tzinfo=UTC)
NOON = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


def test_defaults_are_the_clouds():
    assert prefs.merged(None) == {
        "mutedAlerts": [],
        "dailyReport": False,
        "dailyReportTime": "08:00",
        "weeklyReport": False,
        "quietHoursEnabled": False,
        "quietHoursStart": "22:00",
        "quietHoursEnd": "07:00",
    }


def test_a_bad_stored_value_reads_as_its_default():
    got = prefs.merged({"dailyReport": "yes", "dailyReportTime": "9:00", "mutedAlerts": [1, "x"]})
    assert got["dailyReport"] is False
    assert got["dailyReportTime"] == "08:00"
    assert got["mutedAlerts"] == ["x"]


def test_patch_validation():
    assert prefs.validate_patch({"dailyReport": True, "dailyReportTime": "09:00"}) == {
        "dailyReport": True,
        "dailyReportTime": "09:00",
    }
    assert prefs.validate_patch({"mutedAlerts": ["low_battery", "low_battery"]}) == {
        "mutedAlerts": ["low_battery"]
    }
    for bad in (
        {},
        None,
        {"dailyReportTime": "24:00"},
        {"quietHoursEnabled": 1},
        {"mutedAlerts": "low_battery"},
        {"mutedAlerts": ["Low Battery"]},
        {"promotionsEnabled": False},
    ):
        assert prefs.validate_patch(bad) is None, bad


def test_a_muted_kind_is_not_wanted():
    p = prefs.merged({"mutedAlerts": ["battery_full", "offline"]})
    assert not prefs.wants(p, "battery_full", NOON, KYIV)
    assert prefs.wants(p, "low_battery", NOON, KYIV)
    # The liveness kinds are the cloud's `offline` type.
    assert not prefs.wants(p, "offline_board", NOON, KYIV)
    assert not prefs.wants(p, "offline_board_recovered", NOON, KYIV)


def test_quiet_hours_hold_back_all_but_urgent_kinds():
    p = prefs.merged({"quietHoursEnabled": True})
    assert not prefs.wants(p, "battery_full", LATE, KYIV)
    assert not prefs.wants(p, "grid_restored", LATE, KYIV)
    assert not prefs.wants(p, "calendar_event_started", LATE, KYIV)
    assert prefs.wants(p, "outage", LATE, KYIV)
    assert prefs.wants(p, "offline_board", LATE, KYIV)
    assert prefs.wants(p, "battery_full", NOON, KYIV)
    # The window runs on the station's clock: 21:30 UTC is 23:30 in Kyiv.
    assert prefs.wants(p, "battery_full", LATE, "UTC")


def test_a_daytime_quiet_window():
    p = prefs.merged(
        {"quietHoursEnabled": True, "quietHoursStart": "11:00", "quietHoursEnd": "13:00"}
    )
    assert prefs.in_quiet_hours(p, NOON, KYIV)  # 12:00 in Kyiv
    assert not prefs.in_quiet_hours(p, datetime(2026, 10, 30, 11, 30, tzinfo=UTC), KYIV)
