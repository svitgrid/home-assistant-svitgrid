# HA Panel — History Chart Granularity Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the HA panel history section's rolling-window range chips (`7d/30d/90d/365d`) with the mobile dashboard's granularity model — a `Day / Month / Year / All-time` selector where bar granularity follows the period (hourly/daily/monthly/yearly), with prev/next + date-picker navigation.

**Architecture:** The panel (`custom_components/svitgrid/panel_assets/svitgrid-panel.js`) is a single dependency-free IIFE web component served as one static file with content-hash cache-busting. We extract the *pure* date/bucketing logic into a sibling ES module (`history_periods.js`) that the panel imports and Node unit-tests directly. Monthly/yearly aggregation is achieved by changing the **grouping key** in the existing `_loadHistory` accumulation loop (`bucketKeyFor(day, period)` → `day` | `YYYY-MM` | `YYYY`), so energy sums, source sums, and the trends weighted-mean accumulator all roll up naturally. The Day period reuses the existing hourly fetch+render. No backend/store changes.

**Tech Stack:** Vanilla ES-module JavaScript (browser + Node 22 `--test`), Python 3.12 / Home Assistant integration (`panel.py` static-path registration), pytest.

## Global Constraints

- **Panel-only change.** No Python store/HTTP-route/schema changes except registering the one new static asset in `panel.py`. All data comes from existing `/api/svitgrid/history` endpoints.
- **Single browser bundle stays a served static file.** The panel imports exactly one new sibling module; that module MUST be registered as a static path or the browser 404s the import.
- **`readings_daily` is the permanent local archive** (never pruned); `readings_hourly` ~2yr; `readings_raw`/5-min = 14 days. Year/All-time roll up from daily data.
- **Data contract is unchanged.** `history?granularity=hourly&day=YYYY-MM-DD` → `{hours:[{hour, avgs:{pvPower,loadPower,batteryPower,gridPower}}]}`; `history?start=&end=` → `{days:[{day, energy:{...}, avgs:{...}, sample_count}]}`.
- **Sign conventions (display-only, do not change):** `batteryPower < 0` = discharging; `gridPower > 0` = importing.
- **All-time floor:** hard-coded `2024-01-01` lower bound for the fetch. Years with no daily rows never produce a bucket, so no empty leading years appear.
- **Default period = `day`** (matches mobile). Bucket keys sort correctly as plain strings (`YYYY`, `YYYY-MM`, `YYYY-MM-DD` are all lexicographically monotonic).

---

### Task 1: Pure period/bucket helper module + Node tests

**Files:**
- Create: `custom_components/svitgrid/panel_assets/history_periods.js`
- Create: `custom_components/svitgrid/panel_assets/package.json`
- Test: `tests/js/history_periods.test.mjs`

**Interfaces:**
- Produces (all pure, no DOM):
  - `PERIODS: string[]` = `["day","month","year","all"]`
  - `bucketKeyFor(dayStr: string, period: string): string` — `"2026-07-10"` → day: `"2026-07-10"`, year: `"2026-07"`, all: `"2026"` (month period → same as day). Non-`day`/`all`/`year` returns `dayStr`.
  - `stepAnchor(anchorStr: string, period: string, dir: 1|-1): string` — returns new `YYYY-MM-DD` anchor moved one day/month/year; `all` returns `anchorStr` unchanged.
  - `canGoForward(anchorStr: string, period: string, todayStr: string): boolean` — false when the anchor's period already contains `todayStr` (or is in the future); `all` → false.
  - `periodFetchSpec(anchorStr: string, period: string, todayStr: string): object` — `day` → `{granularity:"hourly", day: anchorStr}`; `month` → `{start:"YYYY-MM-01", end:"YYYY-MM-<lastday>"}`; `year` → `{start:"YYYY-01-01", end: min("YYYY-12-31", todayStr)}`; `all` → `{start:"2024-01-01", end: todayStr}`.
  - `periodLabel(anchorStr: string, period: string): string` — navigator label: day → `"2026-07-10"`, month → `"2026-07"`, year → `"2026"`, all → `""`.

- [ ] **Step 1: Write the failing test file**

Create `tests/js/history_periods.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PERIODS,
  bucketKeyFor,
  stepAnchor,
  canGoForward,
  periodFetchSpec,
  periodLabel,
} from "../../custom_components/svitgrid/panel_assets/history_periods.js";

test("PERIODS order", () => {
  assert.deepEqual(PERIODS, ["day", "month", "year", "all"]);
});

test("bucketKeyFor buckets by period", () => {
  assert.equal(bucketKeyFor("2026-07-10", "month"), "2026-07-10");
  assert.equal(bucketKeyFor("2026-07-10", "year"), "2026-07");
  assert.equal(bucketKeyFor("2026-07-10", "all"), "2026");
  assert.equal(bucketKeyFor("2026-01-01", "year"), "2026-01");
});

test("stepAnchor day/month/year across boundaries", () => {
  assert.equal(stepAnchor("2026-07-10", "day", -1), "2026-07-09");
  assert.equal(stepAnchor("2026-07-01", "day", -1), "2026-06-30");
  assert.equal(stepAnchor("2026-01-31", "month", -1), "2025-12-31"); // clamp: Dec has 31
  assert.equal(stepAnchor("2026-03-31", "month", -1), "2026-02-28"); // Feb clamp
  assert.equal(stepAnchor("2024-03-31", "month", -1), "2024-02-29"); // leap Feb
  assert.equal(stepAnchor("2026-12-15", "month", 1), "2027-01-15");
  assert.equal(stepAnchor("2026-07-10", "year", 1), "2027-07-10");
  assert.equal(stepAnchor("2026-07-10", "all", -1), "2026-07-10"); // no-op
});

test("canGoForward false when period contains today", () => {
  const today = "2026-07-10";
  assert.equal(canGoForward("2026-07-10", "day", today), false);
  assert.equal(canGoForward("2026-07-09", "day", today), true);
  assert.equal(canGoForward("2026-07-01", "month", today), false); // July contains today
  assert.equal(canGoForward("2026-06-30", "month", today), true);  // June is past
  assert.equal(canGoForward("2026-01-01", "year", today), false);  // 2026 contains today
  assert.equal(canGoForward("2025-01-01", "year", today), true);
  assert.equal(canGoForward("2026-07-10", "all", today), false);
});

test("periodFetchSpec shapes", () => {
  const today = "2026-07-10";
  assert.deepEqual(periodFetchSpec("2026-07-10", "day", today), {
    granularity: "hourly",
    day: "2026-07-10",
  });
  assert.deepEqual(periodFetchSpec("2026-07-15", "month", today), {
    start: "2026-07-01",
    end: "2026-07-31",
  });
  assert.deepEqual(periodFetchSpec("2026-02-05", "month", today), {
    start: "2026-02-01",
    end: "2026-02-28",
  });
  // Current year clamps end to today
  assert.deepEqual(periodFetchSpec("2026-03-01", "year", today), {
    start: "2026-01-01",
    end: "2026-07-10",
  });
  // Past year uses full 12-31
  assert.deepEqual(periodFetchSpec("2025-03-01", "year", today), {
    start: "2025-01-01",
    end: "2025-12-31",
  });
  assert.deepEqual(periodFetchSpec("2026-07-10", "all", today), {
    start: "2024-01-01",
    end: "2026-07-10",
  });
});

test("periodLabel formats", () => {
  assert.equal(periodLabel("2026-07-10", "day"), "2026-07-10");
  assert.equal(periodLabel("2026-07-10", "month"), "2026-07");
  assert.equal(periodLabel("2026-07-10", "year"), "2026");
  assert.equal(periodLabel("2026-07-10", "all"), "");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests/js/history_periods.test.mjs`
Expected: FAIL — `Cannot find module '.../history_periods.js'`.

- [ ] **Step 3: Create the package.json (Node ESM resolution)**

Create `custom_components/svitgrid/panel_assets/package.json`:

```json
{
  "type": "module"
}
```

(Makes Node load sibling `.js` files as ES modules. Not served to the browser — only the two explicitly registered static paths are exposed — and does not affect Python.)

- [ ] **Step 4: Implement the helper module**

Create `custom_components/svitgrid/panel_assets/history_periods.js`:

```js
// Pure period/bucket helpers for the Svitgrid panel history chart.
// No DOM — safe to import under Node (`node --test`) and in the browser module.
// Dates are handled as YYYY-MM-DD strings via UTC math to avoid TZ drift.

export const PERIODS = ["day", "month", "year", "all"];

const ALL_TIME_FLOOR = "2024-01-01";

function parse(dayStr) {
  const [y, m, d] = dayStr.split("-").map((n) => parseInt(n, 10));
  return { y, m, d };
}

function pad(n, w) {
  return String(n).padStart(w, "0");
}

function fmt(y, m, d) {
  return pad(y, 4) + "-" + pad(m, 2) + "-" + pad(d, 2);
}

function lastDayOfMonth(y, m) {
  // m is 1-12; day 0 of next month = last day of this month.
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

export function bucketKeyFor(dayStr, period) {
  if (period === "all") return dayStr.slice(0, 4); // YYYY
  if (period === "year") return dayStr.slice(0, 7); // YYYY-MM
  return dayStr; // day + month periods bucket per calendar day
}

export function stepAnchor(anchorStr, period, dir) {
  const { y, m, d } = parse(anchorStr);
  if (period === "day") {
    const dt = new Date(Date.UTC(y, m - 1, d + dir));
    return fmt(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());
  }
  if (period === "month") {
    const ny = y + Math.floor((m - 1 + dir) / 12);
    const nm = ((((m - 1 + dir) % 12) + 12) % 12) + 1;
    const nd = Math.min(d, lastDayOfMonth(ny, nm));
    return fmt(ny, nm, nd);
  }
  if (period === "year") {
    const ny = y + dir;
    const nd = Math.min(d, lastDayOfMonth(ny, m));
    return fmt(ny, m, nd);
  }
  return anchorStr; // all-time: not steppable
}

export function canGoForward(anchorStr, period, todayStr) {
  if (period === "all") return false;
  // The next period's anchor must not be beyond the period containing today.
  const next = stepAnchor(anchorStr, period, 1);
  if (period === "day") return next <= todayStr;
  if (period === "month") return next.slice(0, 7) <= todayStr.slice(0, 7);
  if (period === "year") return next.slice(0, 4) <= todayStr.slice(0, 4);
  return false;
}

export function periodFetchSpec(anchorStr, period, todayStr) {
  const { y, m } = parse(anchorStr);
  if (period === "day") {
    return { granularity: "hourly", day: anchorStr };
  }
  if (period === "month") {
    return {
      start: fmt(y, m, 1),
      end: fmt(y, m, lastDayOfMonth(y, m)),
    };
  }
  if (period === "year") {
    const end = fmt(y, 12, 31);
    return { start: fmt(y, 1, 1), end: end > todayStr ? todayStr : end };
  }
  return { start: ALL_TIME_FLOOR, end: todayStr };
}

export function periodLabel(anchorStr, period) {
  if (period === "day") return anchorStr;
  if (period === "month") return anchorStr.slice(0, 7);
  if (period === "year") return anchorStr.slice(0, 4);
  return "";
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --test tests/js/history_periods.test.mjs`
Expected: PASS — all assertions green.

- [ ] **Step 6: Commit**

```bash
git add custom_components/svitgrid/panel_assets/history_periods.js \
        custom_components/svitgrid/panel_assets/package.json \
        tests/js/history_periods.test.mjs
git commit -m "feat(panel): pure period/bucket helpers + node tests"
```

---

### Task 2: Serve the helper module + wire the import

**Files:**
- Modify: `custom_components/svitgrid/panel.py` (register second static path)
- Modify: `custom_components/svitgrid/panel_assets/svitgrid-panel.js:19` (add top-level import above the IIFE)
- Test: `tests/test_panel.py` (assert both static paths registered)

**Interfaces:**
- Consumes: `history_periods.js` exports from Task 1.
- Produces: helper functions available inside the panel IIFE closure as `PERIODS`, `bucketKeyFor`, `stepAnchor`, `canGoForward`, `periodFetchSpec`, `periodLabel`.

- [ ] **Step 1: Update the panel-registration test to expect both static paths**

In `tests/test_panel.py`, replace the body of `test_register_panel_serves_module_and_registers` assertions that inspect the static-path call with:

```python
    # both static paths registered in one call: the panel module + the helper
    sp_calls = hass.http.async_register_static_paths.await_args.args[0]
    urls = {c.url_path for c in sp_calls}
    assert "/svitgrid_panel/svitgrid-panel.js" in urls
    assert "/svitgrid_panel/history_periods.js" in urls
    panel_cfg = next(c for c in sp_calls if c.url_path.endswith("svitgrid-panel.js"))
    assert panel_cfg.path.endswith("panel_assets/svitgrid-panel.js")
    helper_cfg = next(c for c in sp_calls if c.url_path.endswith("history_periods.js"))
    assert helper_cfg.path.endswith("panel_assets/history_periods.js")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_panel.py::test_register_panel_serves_module_and_registers -q`
Expected: FAIL — only one static path registered; `history_periods.js` not in urls.

- [ ] **Step 3: Register the helper static path in `panel.py`**

Add near the other module constants:

```python
_HELPER_URL = "/svitgrid_panel/history_periods.js"


def _helper_path() -> str:
    return os.path.join(os.path.dirname(__file__), "panel_assets", "history_periods.js")
```

Replace the single-entry `async_register_static_paths` call with both entries (helper served with `cache_headers=False` so the panel's un-hashed relative import never serves stale after an update):

```python
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(_MODULE_URL, _module_path(), True),
                StaticPathConfig(_HELPER_URL, _helper_path(), False),
            ]
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_panel.py -q`
Expected: PASS (all panel tests).

- [ ] **Step 5: Add the top-level import to the panel module**

In `custom_components/svitgrid/panel_assets/svitgrid-panel.js`, insert immediately after the leading block comment and BEFORE `(function () {` (line 19):

```js
import {
  PERIODS,
  bucketKeyFor,
  stepAnchor,
  canGoForward,
  periodFetchSpec,
  periodLabel,
} from "./history_periods.js";

(function () {
```

- [ ] **Step 6: Syntax-check the panel module**

Run: `node --check custom_components/svitgrid/panel_assets/svitgrid-panel.js`
Expected: no output (exit 0). (Node parses the static import fine; the IIFE body's browser globals aren't evaluated by `--check`.)

- [ ] **Step 7: Commit**

```bash
git add custom_components/svitgrid/panel.py custom_components/svitgrid/panel_assets/svitgrid-panel.js tests/test_panel.py
git commit -m "feat(panel): serve history_periods module and import it"
```

---

### Task 3: Period state + `_loadHistory` bucketing + day delegation

**Files:**
- Modify: `custom_components/svitgrid/panel_assets/svitgrid-panel.js` (state init ~918-935; `_histSectionTitle` ~1089-1114; `_loadHistory` ~2188-2320)

**Interfaces:**
- Consumes: `bucketKeyFor`, `periodFetchSpec`, `periodLabel` from Task 1; existing `_loadIntradayForDay` (renamed in Task 5 — for this task keep calling the existing `_loadIntraday`, which Task 5 tidies).
- Produces: instance fields `this._period` (`"day"|"month"|"year"|"all"`), `this._periodAnchor` (`YYYY-MM-DD`); `_loadHistory` groups by `bucketKeyFor` and delegates the `day` period to the hourly renderer.

- [ ] **Step 1: Replace history range state**

In the state-init block (~line 924), replace:

```js
      this._histRangeDays = 30;            // 7 | 30 | 90 | 365
```

with:

```js
      this._period = "day";                // "day" | "month" | "year" | "all"
      this._periodAnchor = this._dateStr(new Date()); // YYYY-MM-DD anchor
```

Remove the two intraday-drill fields (`this._intradayDay`, `this._intradayReq`) from the init block and instead add a single stale-guard token for the hourly (day) fetch:

```js
      this._dayReq = 0;                    // stale-guard for the Day (hourly) fetch
```

- [ ] **Step 2: Update `_histSectionTitle` to use period + anchor**

Replace the three `" — last " + this._histRangeDays + " days"` suffixes (~lines 1092, 1102, 1114) with a period-aware suffix helper. Add this method near `_histSectionTitle`:

```js
    _periodSuffix() {
      switch (this._period) {
        case "day":   return " — " + this._localDate(this._periodAnchor);
        case "month": return " — " + this._monthLabel(this._periodAnchor); // e.g. "July 2026"
        case "year":  return " — " + this._periodAnchor.slice(0, 4);
        case "all":   return " — all time";
        default:      return "";
      }
    }
```

and use `this._periodSuffix()` in place of each `" — last N days"` concatenation. Add the `_monthLabel` helper near `_localDate`:

```js
    _monthLabel(dayStr) {
      // "2026-07-10" -> "July 2026" (browser locale month name).
      const [y, m] = dayStr.split("-").map((n) => parseInt(n, 10));
      const d = new Date(y, m - 1, 1);
      return d.toLocaleString(undefined, { month: "long" }) + " " + y;
    }
```

- [ ] **Step 3: Delegate the Day period and re-key the accumulation in `_loadHistory`**

At the top of `_loadHistory` (after `if (!this._historySec) return true;`), delete the old `if (this._intradayDay) return true;` guard and add day-period delegation:

```js
        if (this._period === "day") {
          return await this._loadDayHourly(this._periodAnchor);
        }
```

Replace the fixed-window range computation:

```js
        const today = new Date();
        const startDate = new Date(today);
        startDate.setDate(today.getDate() - (this._histRangeDays - 1));
        const endStr = this._dateStr(today);
        const startStr = this._dateStr(startDate);
```

with a period-driven fetch range:

```js
        const spec = periodFetchSpec(
          this._periodAnchor,
          this._period,
          this._dateStr(new Date())
        );
        const startStr = spec.start;
        const endStr = spec.end;
```

In the per-inverter day loop, replace every use of the raw `day` grouping key with the bucketed key. Change:

```js
          for (const d of days) {
            const day = d.day;
            if (typeof day !== "string") continue;
```

to:

```js
          for (const d of days) {
            if (typeof d.day !== "string") continue;
            const day = bucketKeyFor(d.day, this._period);
```

(The rest of the loop — `byDay.get(day)` / `byDay.set(day, ...)` for energy, sources, and trends — is unchanged and now aggregates into month/year buckets automatically.)

- [ ] **Step 4: Update the three cache keys to include period + anchor**

Replace `this._histRangeDays` in the three `newKey` builders (~2282, 2293, 2304) with `this._period + "|" + this._periodAnchor`. Example for the energy branch:

```js
          const newKey = this._period + "|" + this._periodAnchor + "|" + metric + "|" + dataFingerprint;
```

(Apply the analogous change to the `sources` and `trends` newKey strings.)

- [ ] **Step 5: Add `_loadDayHourly` (thin wrapper over the existing hourly fetch)**

Add a method that reuses the existing hourly fetch+render. For this task, implement it by calling the existing `_loadIntraday`:

```js
    async _loadDayHourly(day) {
      await this._loadIntraday(day);
      return true;
    }
```

(Task 5 folds `_loadIntraday`/`_renderIntraday` into this path and removes the back button; keeping the wrapper now lets Task 3 be tested independently.)

- [ ] **Step 6: Verify no Python breakage and panel parses**

Run: `python -m pytest tests/test_panel.py tests/test_http_views.py -q`
Expected: PASS.
Run: `node --check custom_components/svitgrid/panel_assets/svitgrid-panel.js`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add custom_components/svitgrid/panel_assets/svitgrid-panel.js
git commit -m "feat(panel): period-driven history bucketing + day delegation"
```

---

### Task 4: Control bar — period tabs + navigator replacing range chips

**Files:**
- Modify: `custom_components/svitgrid/panel_assets/svitgrid-panel.js` (`_appendHistControls` ~2322-2440; string table `STR` ~55-140; CSS `.hist-controls` block ~468)

**Interfaces:**
- Consumes: `PERIODS`, `stepAnchor`, `canGoForward`, `periodLabel` from Task 1; `this._period`, `this._periodAnchor` from Task 3.
- Produces: the rendered controls emit period/anchor changes that call `this._loadHistory()`.

- [ ] **Step 1: Add the period/navigation strings to `STR`**

In the `STR` object add:

```js
    periodDay: "Day",
    periodMonth: "Month",
    periodYear: "Year",
    periodAll: "All-time",
    periodPrev: "Previous period",
    periodNext: "Next period",
    periodAriaTabs: "Select history period",
```

- [ ] **Step 2: Replace the range-chip group with the period tab row**

In `_appendHistControls`, delete the entire "Range chips: 7 / 30 / 90 / 365" block (the `rangeGroup` … `controls.appendChild(rangeGroup);` section, ~lines 2360-2386) and insert in its place a period tab group:

```js
      // Period tabs: Day | Month | Year | All-time
      const periodGroup = document.createElement("div");
      periodGroup.className = "hist-chip-group";
      periodGroup.setAttribute("role", "group");
      periodGroup.setAttribute("aria-label", STR.periodAriaTabs);
      const periodLabels = {
        day: STR.periodDay, month: STR.periodMonth,
        year: STR.periodYear, all: STR.periodAll,
      };
      for (const p of PERIODS) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "hist-chip";
        btn.textContent = periodLabels[p];
        btn.setAttribute("aria-pressed", p === this._period ? "true" : "false");
        btn.addEventListener("click", () => {
          if (this._period === p) return;
          this._period = p;
          // Switching to Day hides Sources/Trends → fall back to Energy.
          if (p === "day" && this._histMode !== "energy") this._histMode = "energy";
          this._histKey = null;
          this._lastHistoryFetch = 0;
          this._rerenderHistoryControls();
          this._loadHistory();
        });
        periodGroup.appendChild(btn);
      }
      controls.appendChild(periodGroup);
```

- [ ] **Step 3: Append the navigator row (hidden for all-time)**

Immediately after `controls.appendChild(periodGroup);` add:

```js
      if (this._period !== "all") {
        const nav = document.createElement("div");
        nav.className = "hist-nav";

        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "hist-nav-btn";
        prev.textContent = "‹";
        prev.setAttribute("aria-label", STR.periodPrev);
        prev.addEventListener("click", () => {
          this._periodAnchor = stepAnchor(this._periodAnchor, this._period, -1);
          this._histKey = null;
          this._lastHistoryFetch = 0;
          this._rerenderHistoryControls();
          this._loadHistory();
        });
        nav.appendChild(prev);

        const label = document.createElement("button");
        label.type = "button";
        label.className = "hist-nav-label";
        label.textContent = periodLabel(this._periodAnchor, this._period);
        label.addEventListener("click", () => this._openPeriodPicker());
        nav.appendChild(label);

        const next = document.createElement("button");
        next.type = "button";
        next.className = "hist-nav-btn";
        next.textContent = "›";
        next.setAttribute("aria-label", STR.periodNext);
        const fwd = canGoForward(this._periodAnchor, this._period, this._dateStr(new Date()));
        next.disabled = !fwd;
        next.style.opacity = fwd ? "1" : "0.3";
        next.addEventListener("click", () => {
          if (!canGoForward(this._periodAnchor, this._period, this._dateStr(new Date()))) return;
          this._periodAnchor = stepAnchor(this._periodAnchor, this._period, 1);
          this._histKey = null;
          this._lastHistoryFetch = 0;
          this._rerenderHistoryControls();
          this._loadHistory();
        });
        nav.appendChild(next);

        controls.appendChild(nav);
      }
```

- [ ] **Step 4: Hide Sources/Trends mode chips on the Day period**

In the mode-switcher loop, skip the non-energy modes when the period is `day`. Change the `for (const mo of modeOptions)` header to:

```js
      for (const mo of modeOptions) {
        if (this._period === "day" && mo.key !== "energy") continue;
```

- [ ] **Step 5: Add `_rerenderHistoryControls` and `_openPeriodPicker`**

The controls are rebuilt as part of `_loadHistory` → the render functions call `_appendHistControls`. Add a light helper that forces a controls+chart refresh (title + re-fetch already handled by `_loadHistory`; this exists so navigator/tab handlers can update button state immediately):

```js
    _rerenderHistoryControls() {
      if (this._histSec) this._histSec.textContent = this._histSectionTitle();
      // The subsequent _loadHistory() rebuilds controls via the render path.
    }

    _openPeriodPicker() {
      const input = document.createElement("input");
      input.type = this._period === "month" ? "month" : this._period === "year" ? "number" : "date";
      if (this._period === "year") { input.min = "2024"; input.max = String(new Date().getFullYear()); }
      input.max = input.max || this._dateStr(new Date());
      input.value =
        this._period === "month" ? this._periodAnchor.slice(0, 7)
        : this._period === "year" ? this._periodAnchor.slice(0, 4)
        : this._periodAnchor;
      input.style.position = "fixed";
      input.style.left = "-9999px";
      this.shadowRoot.appendChild(input);
      const commit = () => {
        const v = input.value;
        if (v) {
          this._periodAnchor =
            this._period === "month" ? v + "-01"
            : this._period === "year" ? v + "-01-01"
            : v;
          this._histKey = null;
          this._lastHistoryFetch = 0;
          this._loadHistory();
        }
        input.remove();
      };
      input.addEventListener("change", commit);
      input.addEventListener("blur", () => input.remove());
      input.showPicker ? input.showPicker() : input.focus();
    }
```

- [ ] **Step 6: Add navigator CSS**

In the `.hist-controls` style area (~line 468), add:

```css
    .hist-nav { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); }
    .hist-nav-btn {
      background: none; border: none; color: var(--sg-text); cursor: pointer;
      font-size: 18px; line-height: 1; padding: 2px 6px; border-radius: 6px;
    }
    .hist-nav-btn:disabled { cursor: default; }
    .hist-nav-label {
      background: none; border: none; color: var(--sg-text); cursor: pointer;
      font-size: 13px; font-weight: 500; padding: 2px 4px;
    }
```

- [ ] **Step 7: Verify parse + Python tests**

Run: `node --check custom_components/svitgrid/panel_assets/svitgrid-panel.js`
Expected: exit 0.
Run: `python -m pytest tests/test_panel.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add custom_components/svitgrid/panel_assets/svitgrid-panel.js
git commit -m "feat(panel): period tabs + prev/next/date-picker navigator"
```

---

### Task 5: Bucket-aware axis labels + remove tap-to-drill / back button

**Files:**
- Modify: `custom_components/svitgrid/panel_assets/svitgrid-panel.js` (`_renderHistory` ~2466-2600; `_renderHistorySources` ~2610-2780; `_renderHistoryTrends` ~3143-3330; `_renderIntraday` ~2883; intraday tap handlers ~2547, 2693)

**Interfaces:**
- Consumes: `this._period`, `this._periodAnchor`.
- Produces: x-axis/tooltip labels adapt per period; the Day period renders the hourly profile with no back button; day-bar tap no longer drills.

- [ ] **Step 1: Add `_bucketLabel`**

Add near `_localDate`:

```js
    _bucketLabel(key) {
      // key is a bucket string whose shape depends on the active period:
      //   month period -> "YYYY-MM-DD" (day label), year -> "YYYY-MM" (month), all -> "YYYY".
      if (this._period === "year") {
        const [y, m] = key.split("-").map((n) => parseInt(n, 10));
        return new Date(y, m - 1, 1).toLocaleString(undefined, { month: "short" });
      }
      if (this._period === "all") return key; // "YYYY"
      return this._localDate(key); // month period → day label
    }
```

- [ ] **Step 2: Use `_bucketLabel` for daily-render x-axis + tooltips**

In `_renderHistory`, `_renderHistorySources`, and `_renderHistoryTrends`, replace every x-axis tick and tooltip use of `this._localDate(<seriesItem>.day)` with `this._bucketLabel(<seriesItem>.day)`. Concretely, the call sites at ~2563, ~2598, ~2713, ~2768, ~3287, ~3324 change from `this._localDate(...)` to `this._bucketLabel(...)`.

- [ ] **Step 3: Remove the day-bar tap-to-drill handlers**

In `_renderHistory` (~2547) and `_renderHistorySources` (~2693), the bars are wrapped in a `<button class="bar-col-btn">` whose click calls `this._intradayDay = s.day; this._loadIntraday(s.day);`. Remove the click handler and the `aria-label` drill text; keep the bar element non-interactive (a plain `div` column). Replace the `btn` creation + listener with the previously-inner bar/cap markup appended directly to `col`. Delete the now-unused `STR.intradayAriaBar`.

- [ ] **Step 4: Remove the intraday back button and fold into the day view**

In `_renderIntraday` (~2883), delete the "Back button" block (the `back` element and its click listener, ~2895-2910). The section title for the Day period comes from `_histSectionTitle()`/`_periodSuffix()` (Task 3), so replace the intraday-specific title line:

```js
      if (this._histSec) {
        const dateLabel = this._localDate(day);
        this._histSec.textContent = STR.intradayTitle + " — " + dateLabel;
      }
```

with:

```js
      if (this._histSec) this._histSec.textContent = this._histSectionTitle();
```

Ensure `_renderIntraday` calls `this._appendHistControls(this._historySec)` (so the Day view still shows the period tabs + navigator). Add that call right after the `innerHTML = ""` reset if not already present.

- [ ] **Step 5: Verify parse + full Python suite**

Run: `node --check custom_components/svitgrid/panel_assets/svitgrid-panel.js`
Expected: exit 0.
Run: `python -m pytest tests/ -q`
Expected: PASS (note any pre-existing unrelated failures per repo norms — e.g. the known CI `StaticPathConfig` skip).

- [ ] **Step 6: Commit**

```bash
git add custom_components/svitgrid/panel_assets/svitgrid-panel.js
git commit -m "feat(panel): bucket-aware labels; day period replaces tap-to-drill"
```

---

### Task 6: Manual verification, CHANGELOG, version bump

**Files:**
- Modify: `custom_components/svitgrid/manifest.json` (version) and `config.yaml`/add-on version if the repo bumps there — match the repo's existing release convention.
- Modify: `CHANGELOG.md`

**Interfaces:** none (release wrap-up).

- [ ] **Step 1: Load the panel against a running HA add-on and walk every period**

Load the Svitgrid sidebar panel in a browser against a dev/local HA instance with the add-on installed (the user runs this; do not flash without explicit ask). Verify:
- Day → 24-hour hourly profile for the anchor day; Sources/Trends chips hidden; `›` disabled on today.
- Month → daily bars for the anchored month; `‹`/`›` step months; Sources/Trends work.
- Year → 12 monthly bars (x-axis month names); values equal the sum of that month's days.
- All-time → yearly bars; only years with data appear; navigator hidden.
- Tapping the date label opens the native date/month/year picker and jumps correctly.
- Metric chips re-bucket at every granularity; section title reads e.g. "Generated — July 2026" / "Generated — 2026" / "Generated — all time".

- [ ] **Step 2: Add the CHANGELOG entry**

Prepend to `CHANGELOG.md`:

```markdown
## <next version>

- Panel history chart: replaced the 7d/30d/90d/365d range chips with a
  Day / Month / Year / All-time granularity selector (hourly / daily / monthly /
  yearly bars) plus prev/next + date-picker navigation, matching the mobile
  dashboard. Sources/Trends remain for Month/Year/All-time and are hidden on Day.
```

- [ ] **Step 3: Bump the version**

Follow the repo's release convention (see recent `chore(release): 0.12.3` commit — bump `manifest.json` `version` and any `config.yaml`/`hacs.json` version the repo keys on). Use the next patch/minor.

- [ ] **Step 4: Run the full suite once more**

Run: `python -m pytest tests/ -q && node --test tests/js/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md custom_components/svitgrid/manifest.json
git commit -m "chore(release): panel history granularity selector"
```

---

## Self-Review

**Spec coverage:**
- Granularity selector Day/Month/Year/All-time → Task 4 (tabs) + Task 1 (`PERIODS`, `periodFetchSpec`, `bucketKeyFor`). ✓
- Prev/next + tappable date, hidden for all-time, `canGoForward` gating → Task 4 + Task 1 (`stepAnchor`, `canGoForward`). ✓
- Day=hourly / Month=daily / Year=monthly / All=yearly → Task 3 (delegation + bucketing). ✓
- Client-side monthly/yearly rollup from daily; empty years dropped → Task 3 (bucketKeyFor grouping; empty buckets never created). ✓
- All-time floor 2024-01-01 → Task 1 (`ALL_TIME_FLOOR`). ✓
- Keep Energy + metric chips at every granularity → Task 3/4 (metric chips untouched, bucketing metric-agnostic). ✓
- Sources/Trends kept for Month/Year/All, hidden on Day, fall back to Energy → Task 4 Step 2/4. ✓
- Remove range chips + `_histRangeDays` → Task 3 Step 1 + Task 4 Step 2. ✓
- Remove tap-to-drill hourly sheet + back button → Task 5 Step 3/4. ✓
- Dynamic section title per period → Task 3 Step 2 (`_periodSuffix`). ✓
- Panel-only, no backend/store change (only new static asset) → Task 2. ✓
- Tests for rollup/stepping/canGoForward/fetch-range → Task 1. ✓

**Placeholder scan:** No TBD/TODO; all code shown. ✓

**Type consistency:** Helper names (`PERIODS`, `bucketKeyFor`, `stepAnchor`, `canGoForward`, `periodFetchSpec`, `periodLabel`) match between Task 1 exports, the Task 2 import, and their Task 3/4/5 call sites. Instance fields `this._period` / `this._periodAnchor` / `this._dayReq` introduced in Task 3 and consumed consistently in Tasks 4/5. ✓

**Note on DOM testing:** The repo has no JS DOM test harness (panel DOM was historically verified by loading the add-on). Pure logic is unit-tested via `node --test` (Task 1); DOM wiring is verified manually (Task 6 Step 1) and guarded by `node --check` syntax checks + the Python `test_panel.py` serving tests.
