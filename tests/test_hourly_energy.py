"""Tests for custom_components.svitgrid.hourly_energy.per_hour_deltas.

Spec: docs/superpowers/plans/2026-07-02-island-hourly-energy.md, Task 1.

Reset-handling rules mirror the established pattern in the main svitgrid
repo's services/api/src/services/derived-daily.ts (`firstPostResetIndex` /
`counterDropIndices` / `counterNullDropIndices`): a drop in a cumulative
counter mid-day is treated as a fresh post-reset start rather than producing
a negative/nonsensical delta, and a meaningful->null transition is a gap
that does not itself reset the running "previous" state.
"""

from custom_components.svitgrid.hourly_energy import per_hour_deltas


def _row(local_date, hour, import_cum=None, export_cum=None):
    return {
        "local_date": local_date,
        "hour": hour,
        "import_cum": import_cum,
        "export_cum": export_cum,
    }


def _import_by_hour(rows):
    return {r["hour"]: r["importKwh"] for r in rows}


def _export_by_hour(rows):
    return {r["hour"]: r["exportKwh"] for r in rows}


def test_monotonic_day_import():
    hours = [
        _row("2026-07-02", 0, import_cum=1.0),
        _row("2026-07-02", 1, import_cum=2.5),
        _row("2026-07-02", 2, import_cum=4.0),
    ]
    out = per_hour_deltas(hours)
    by_hour = _import_by_hour(out)
    assert by_hour[0] == 1.0
    assert by_hour[1] == 1.5
    assert by_hour[2] == 1.5


def test_first_hour_of_day_is_its_own_value():
    hours = [_row("2026-07-02", 0, import_cum=1.0)]
    out = per_hour_deltas(hours)
    assert len(out) == 1
    assert out[0]["local_date"] == "2026-07-02"
    assert out[0]["hour"] == 0
    assert out[0]["importKwh"] == 1.0


def test_gap_none_hour_differences_against_last_present():
    hours = [
        _row("2026-07-02", 0, import_cum=1.0),
        _row("2026-07-02", 1, import_cum=None),
        _row("2026-07-02", 2, import_cum=4.0),
    ]
    out = per_hour_deltas(hours)
    by_hour = _import_by_hour(out)
    # hour 1 was a gap: no importKwh contribution emitted for it
    assert by_hour.get(1) in (None, 0.0)
    # hour 2 differences against hour 0's present cum (1.0), not the gap
    assert by_hour[2] == 3.0
    assert by_hour[0] == 1.0


def test_mid_day_drop_is_fresh_post_reset_start():
    hours = [
        _row("2026-07-02", 0, import_cum=5.0),
        _row("2026-07-02", 1, import_cum=1.0),
    ]
    out = per_hour_deltas(hours)
    by_hour = _import_by_hour(out)
    assert by_hour[0] == 5.0
    # NOT -4.0 -- treated as a fresh post-reset start: max(0, 1.0)
    assert by_hour[1] == 1.0


def test_new_day_resets_previous():
    hours = [
        _row("2026-07-01", 23, import_cum=10.0),
        _row("2026-07-02", 0, import_cum=2.0),
    ]
    out = per_hour_deltas(hours)
    day_b_hour_0 = next(
        r for r in out if r["local_date"] == "2026-07-02" and r["hour"] == 0
    )
    # NOT -8.0 -- new local_date always resets the running previous
    assert day_b_hour_0["importKwh"] == 2.0


def test_never_negative_stress_case():
    hours = [
        _row("2026-07-01", 22, import_cum=8.0, export_cum=3.0),
        _row("2026-07-01", 23, import_cum=10.0, export_cum=6.0),
        _row("2026-07-02", 0, import_cum=2.0, export_cum=0.5),
        _row("2026-07-02", 1, import_cum=None, export_cum=None),
        _row("2026-07-02", 2, import_cum=1.5, export_cum=9.0),
        _row("2026-07-02", 3, import_cum=0.5, export_cum=2.0),
        _row("2026-07-02", 4, import_cum=6.0, export_cum=1.0),
    ]
    out = per_hour_deltas(hours)
    for r in out:
        assert r["importKwh"] >= 0
        assert r["exportKwh"] >= 0


def test_export_mirrors_import_monotonic_day():
    hours = [
        _row("2026-07-02", 0, export_cum=0.5),
        _row("2026-07-02", 1, export_cum=1.5),
        _row("2026-07-02", 2, export_cum=3.0),
    ]
    out = per_hour_deltas(hours)
    by_hour = _export_by_hour(out)
    assert by_hour[0] == 0.5
    assert by_hour[1] == 1.0
    assert by_hour[2] == 1.5


def test_export_mirrors_import_mid_day_drop():
    hours = [
        _row("2026-07-02", 0, export_cum=7.0),
        _row("2026-07-02", 1, export_cum=2.0),
    ]
    out = per_hour_deltas(hours)
    by_hour = _export_by_hour(out)
    assert by_hour[0] == 7.0
    # fresh post-reset start, NOT -5.0
    assert by_hour[1] == 2.0


def test_import_and_export_tracked_independently_within_same_row():
    # import present, export absent on the same hour, then export present later
    hours = [
        _row("2026-07-02", 0, import_cum=1.0, export_cum=None),
        _row("2026-07-02", 1, import_cum=2.0, export_cum=5.0),
    ]
    out = per_hour_deltas(hours)
    by_hour_import = _import_by_hour(out)
    by_hour_export = _export_by_hour(out)
    assert by_hour_import[0] == 1.0
    assert by_hour_import[1] == 1.0
    # export's first present hour is hour 1 -> its own value, not differenced
    assert by_hour_export[1] == 5.0
