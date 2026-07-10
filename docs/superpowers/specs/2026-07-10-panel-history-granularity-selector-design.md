# HA Panel — History Chart Granularity Selector

**Date:** 2026-07-10
**Repo:** `home-assistant-svitgrid`
**Branch:** `feat/panel-granularity`
**Status:** Design approved, pending spec review

## Goal

Replace the branded SvitGrid HA panel's rolling-window range chips
(`7d / 30d / 90d / 365d`) in the history section with the mobile dashboard's
**granularity model**: a `Day / Month / Year / All-time` selector where the bar
granularity follows the selected period, plus a prev/next + tappable-date
navigator (hidden for All-time).

This mirrors the mobile app's
`apps/mobile/lib/features/dashboard/presentation/widgets/chart_period_selector.dart`
and `core/providers/chart_period_provider.dart` behavior:

| Period | Bar granularity |
|--------|-----------------|
| Day | hourly (24 bars) |
| Month | daily (28–31 bars) |
| Year | monthly (12 bars) |
| All-time | yearly (N bars) |

## Non-goals

- No backend / Python / store changes. All data is reachable through existing
  HTTP endpoints; monthly/yearly buckets are rolled up **client-side** in the
  panel JS.
- Sources/Trends do **not** gain hourly semantics (see Modes below).
- No change to the "TODAY" tile row or any other panel section.

## Current state (what's being replaced)

`custom_components/svitgrid/panel_assets/svitgrid-panel.js` history section:

- **Range chips** `7d / 30d / 90d / 365d` → drive `_histRangeDays`; the chart
  always renders **daily** buckets, only the window width changes.
- **Mode chips** `Energy / Sources / Trends`.
- **Metric chips** `Generated / Consumed / Imported / Exported / Batt charged /
  Batt discharged / Losses` (energy field keys `dailyPvEnergy`,
  `dailyLoadEnergy`, `dailyGridImportEnergy`, `dailyGridExportEnergy`,
  `dailyBatteryChargeEnergy`, `dailyBatteryDischargeEnergy`, `dailyLossesEnergy`).
- **Tap-a-day-bar → hourly drill-down** (`_intradayDay`, `granularity=hourly`
  fetch, "Hourly profile" sheet).

## Data sources (all already exist)

HTTP: `custom_components/svitgrid/http_views.py` `SvitgridHistoryView`
(`/api/svitgrid/history`):

- `?inverter_id=&granularity=hourly&day=YYYY-MM-DD` → `{ hours: [...] }` (24 rows)
- `?inverter_id=&start=YYYY-MM-DD&end=YYYY-MM-DD` → `{ days: [...] }` (daily rows)

Retention (`const.py`): `readings_daily` is **never pruned** (permanent local
archive per `reading_store.py:797`), `readings_hourly` = ~2 years,
`readings_raw`/5-min = 14 days. So Year/All-time rollups from daily data are
always available for the device's full local history.

### Fetch strategy per period

| Period | Endpoint call | Post-processing |
|--------|---------------|-----------------|
| Day | `granularity=hourly&day=<anchor>` | render 24 hourly bars directly |
| Month | `start=<anchor YYYY-MM-01>&end=<month last day>` | render daily bars |
| Year | `start=<YYYY-01-01>&end=min(YYYY-12-31, today)` | roll up daily → 12 monthly buckets in JS |
| All-time | `start=2024-01-01&end=<today>` | roll up daily → yearly buckets; **drop years with zero total** |

The daily→month and daily→year rollups sum each metric's energy field into the
`YYYY-MM` / `YYYY` bucket key. Reuse the panel's existing per-metric
`sumPresent`/`sum` accumulation logic.

**All-time floor:** hard-coded `2024-01-01` lower bound. After rollup, years
whose total (across all metrics) is zero are omitted so the chart never shows
empty leading years. (No backend "earliest day" helper — keeps this panel-only.)

## UI: selector & navigation

Mirrors `chart_period_selector.dart`.

- **Tab row** replacing the range chips: `Day · Month · Year · All-time`.
  Selected tab styled like the existing active chip.
- **Navigator row** below (hidden when `all`):
  `‹  <date label>  ›`
  - Date label: `YYYY-MM-DD` (Day) / `YYYY-MM` (Month) / `YYYY` (Year).
  - `‹` steps the anchor back one day/month/year.
  - `›` steps forward; **disabled** when the current period already contains
    today (no future data). Equivalent to mobile `canGoForward`.
  - Tapping the label opens a native picker to jump directly:
    `<input type="date">` for Day, `<input type="month">` for Month, and a
    year `<select>`/number input for Year.

### New panel state

Replace `_histRangeDays` with:

- `_period`: `"day" | "month" | "year" | "all"` (default `"day"` to match mobile).
- `_periodAnchor`: a `YYYY-MM-DD` string; the reference date for the selected
  period. Defaults to today.

Persist the same way current history UI state is persisted (localStorage key
alongside `_histMetric` / `_histMode`), if that mechanism exists; otherwise
in-memory (matches current `_histRangeDays` lifetime).

Helper functions (unit-tested):

- `stepAnchor(anchor, period, dir)` → new `YYYY-MM-DD`.
- `canGoForward(anchor, period, today)` → bool.
- `rollupDailyToMonthly(days)` / `rollupDailyToYearly(days)` → bucket arrays.
- `periodFetchRange(anchor, period)` → `{start, end}` or `{granularity, day}`.

## Modes & metrics

- **Energy** bar chart + **metric chips**: kept, work at **every** granularity.
  Under Day they render the hourly series for the chosen metric; under
  Month/Year/All they render daily/monthly/yearly bars.
- **Sources** and **Trends** mode chips: kept for **Month / Year / All-time**;
  **hidden when `_period === "day"`**. If the user is in Sources/Trends and
  switches to Day, mode falls back to Energy automatically.

## Removed

- Range chips `7d/30d/90d/365d` and all `_histRangeDays` logic.
- **Tap-a-day-bar → hourly drill-down sheet** (`_intradayDay`, `_intradayReq`,
  the intraday fetch + render, and the `granularity=hourly` drill call). The Day
  period is now the canonical hourly view, so the second path is redundant. The
  hourly *fetch + render* code is largely reused by the Day period; only the
  drill-down trigger/sheet wrapper is removed.

## Section title

Dynamic, driven by metric + period:

- Day: `<Metric> — <anchor DD Mon>` (e.g. "Generated — 10 Jul")
- Month: `<Metric> — <Month YYYY>` (e.g. "Generated — July 2026")
- Year: `<Metric> — <YYYY>` (e.g. "Generated — 2026")
- All-time: `<Metric> — all time`

(Sources/Trends keep their own title prefixes, unchanged, appended with the same
period suffix.)

## Testing

Repo `tests/` dir. Add JS-level coverage (the panel is vanilla JS; use the
existing panel test harness / a small DOM-free unit test for pure helpers):

- `rollupDailyToMonthly` — daily rows spanning a year → 12 correct monthly sums;
  missing days contribute 0; metric keys preserved.
- `rollupDailyToYearly` — multi-year daily rows → correct yearly sums;
  zero-total years dropped.
- `stepAnchor` — day/month/year stepping across month and year boundaries
  (e.g. 2026-01-31 −1 month, 2026-12 +1 month, leap-year Feb).
- `canGoForward` — false when anchor's period contains today; true for past
  periods; All-time not applicable.
- `periodFetchRange` — correct `{start,end}` for month (last-day-of-month) and
  year (clamped to today), correct `{granularity:'hourly', day}` for Day.

Run the existing test suite before and after; fix any panel tests that assert
on the removed range chips.

## Rollout

- Panel-only change → normal add-on version bump (patch/minor) + CHANGELOG entry.
- Ships via HACS like prior panel changes. No API deploy, no firmware.
