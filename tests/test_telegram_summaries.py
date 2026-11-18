"""Daily and weekly island Telegram reports (svitgrid D-2152)."""

from datetime import UTC, datetime

from custom_components.svitgrid.telegram import copy, prefs
from custom_components.svitgrid.telegram.summaries import due_summaries, summary_totals

KYIV = "Europe/Kyiv"
ON = prefs.merged({"dailyReport": True, "weeklyReport": True, "dailyReportTime": "09:00"})


def _due(now, stamps=None, p=ON):
    return [
        (d.kind, d.period, d.from_day, d.to_day) for d in due_summaries(p, stamps or {}, now, KYIV)
    ]


def test_daily_waits_for_the_report_time_on_the_stations_clock():
    # Wednesday 2026-10-28: 06:59 UTC is 08:59 in Kyiv, 07:00 UTC is 09:00.
    assert _due(datetime(2026, 10, 28, 6, 59, tzinfo=UTC)) == [
        ("weekly_summary", "2026-10-19", "2026-10-19", "2026-10-25")
    ]
    assert _due(datetime(2026, 10, 28, 7, 0, tzinfo=UTC))[0] == (
        "daily_summary",
        "2026-10-27",
        "2026-10-27",
        "2026-10-27",
    )


def test_a_sent_period_is_not_due_again():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert _due(now, {"daily": "2026-10-27", "weekly": "2026-10-19"}) == []


def test_weekly_waits_for_monday_08_00():
    # Monday 2026-10-26: 05:59 UTC is 07:59 in Kyiv.
    monday_early = datetime(2026, 10, 26, 5, 59, tzinfo=UTC)
    assert [k for k, *_ in _due(monday_early, {"daily": "2026-10-25"})] == []
    monday = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)
    assert _due(monday, {"daily": "2026-10-25"}) == [
        ("weekly_summary", "2026-10-19", "2026-10-19", "2026-10-25")
    ]


def test_reports_off_are_never_due():
    off = prefs.merged({})
    assert _due(datetime(2026, 10, 28, 12, 0, tzinfo=UTC), p=off) == []


def _row(pv=None, load=None, imp=None, exp=None, peak_pv=None):
    energy = {
        k: v
        for k, v in (
            ("dailyPvEnergy", pv),
            ("dailyLoadEnergy", load),
            ("dailyGridImportEnergy", imp),
            ("dailyGridExportEnergy", exp),
        )
        if v is not None
    }
    peaks = {} if peak_pv is None else {"pvPower": peak_pv}
    return {"energy": energy, "peaks": peaks}


def test_totals_sum_every_inverter_and_day():
    totals = summary_totals(
        [
            [_row(10.0, 8.0, 1.0, 2.0, 3000), _row(5.0, 4.0, 0.5, 1.0, 2000)],
            [_row(2.5, 1.0, 0.0, 0.0, 900)],
        ]
    )
    assert totals == {"pv": 17.5, "load": 13.0, "import": 1.5, "export": 3.0}


def test_a_pv_counter_with_no_pv_power_counts_zero():
    """The cloud's phantom-PV gate: a day whose PV peak is 0 produced nothing."""
    assert summary_totals([[_row(4.0, 3.0, peak_pv=0)]])["pv"] == 0


def test_no_energy_at_all_is_no_report_never_a_zero():
    assert summary_totals([]) is None
    assert summary_totals([[{"energy": {}, "peaks": {}}]]) is None


def test_summary_copy():
    totals = {"pv": 12.34, "load": 10.0, "import": 1.0, "export": 3.0}
    assert copy.render_summary("daily_summary", "uk", totals) == (
        "<b>Щоденний звіт</b>\nЗа вчора: 12.3 кВт·год згенеровано, 10.0 кВт·год спожито"
    )
    weekly = copy.render_summary("weekly_summary", "en", totals)
    assert weekly.startswith("<b>Weekly Energy Summary</b>\nThis week: 12.3 kWh generated")
    assert weekly.endswith("Self-consumption: 76%")
