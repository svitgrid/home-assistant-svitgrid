import { test } from "node:test";
import assert from "node:assert/strict";
import {
  PERIODS,
  bucketKeyFor,
  stepAnchor,
  canGoForward,
  periodFetchSpec,
  periodLabel,
  hourIndexOf,
  collapseHourBuckets,
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

// ── hourIndexOf ──────────────────────────────────────────────────────
// The Day chart's x-axis is the HOUSEHOLD-LOCAL wall clock. The server now
// stamps each bucket with `localHour`; the panel must plot that, not the UTC
// hour it used to slice out of the timestamp. For a Kyiv (UTC+3) household
// that slice drew the whole solar curve three hours early -- an 08:00 local
// peak landed at 05:00.

test("hourIndexOf prefers the server's localHour", () => {
  assert.equal(hourIndexOf({ hour: "2026-07-15T05:00:00Z", localHour: 8 }), 8);
});

test("hourIndexOf accepts localHour 0 (not treated as missing)", () => {
  assert.equal(hourIndexOf({ hour: "2026-07-14T21:00:00Z", localHour: 0 }), 0);
});

test("hourIndexOf falls back to the UTC hour when localHour is absent", () => {
  // An older server that predates localHour degrades to the previous
  // behaviour rather than dropping every bucket.
  assert.equal(hourIndexOf({ hour: "2026-07-15T05:00:00Z" }), 5);
});

test("hourIndexOf rejects out-of-range and malformed buckets", () => {
  assert.equal(hourIndexOf({ hour: "2026-07-15T05:00:00Z", localHour: 24 }), null);
  assert.equal(hourIndexOf({ hour: "2026-07-15T05:00:00Z", localHour: -1 }), null);
  assert.equal(hourIndexOf({ hour: "garbage" }), null);
  assert.equal(hourIndexOf({}), null);
  assert.equal(hourIndexOf(null), null);
});

test("hourIndexOf ignores a non-numeric localHour and falls back", () => {
  assert.equal(hourIndexOf({ hour: "2026-07-15T05:00:00Z", localHour: "8" }), 5);
});

// ── collapseHourBuckets ──────────────────────────────────────────────
// On a DST fall-back day the local clock repeats an hour, so TWO distinct UTC
// buckets carry the same localHour. The Day chart accumulates per hour index
// (to sum across inverters), so without collapsing them first that repeated
// hour renders as a 2x spike once a year.
//
// These are POWER AVERAGES, not cumulative counters -- the repeated hour is
// averaged, not summed and not last-wins. (Contrast hourly_energy.py's
// to_local_hour_rows, where later-wins is correct precisely because those ARE
// cumulative counters.)

test("collapseHourBuckets passes distinct hours through untouched", () => {
  const out = collapseHourBuckets([
    { hour: "2026-07-15T05:00:00Z", localHour: 8, avgs: { pvPower: 1900 } },
    { hour: "2026-07-15T06:00:00Z", localHour: 9, avgs: { pvPower: 2100 } },
  ]);
  assert.deepEqual(out, [
    { hourIndex: 8, avgs: { pvPower: 1900 } },
    { hourIndex: 9, avgs: { pvPower: 2100 } },
  ]);
});

test("collapseHourBuckets averages a repeated DST fall-back hour", () => {
  // Europe/Kyiv 2026-10-25: 00:00Z and 01:00Z are both local hour 3.
  const out = collapseHourBuckets([
    { hour: "2026-10-25T00:00:00Z", localHour: 3, avgs: { loadPower: 600 } },
    { hour: "2026-10-25T01:00:00Z", localHour: 3, avgs: { loadPower: 800 } },
  ]);
  assert.deepEqual(out, [{ hourIndex: 3, avgs: { loadPower: 700 } }]);
});

test("collapseHourBuckets averages each metric independently", () => {
  const out = collapseHourBuckets([
    { localHour: 3, avgs: { pvPower: 100, loadPower: 600, batteryPower: -200 } },
    { localHour: 3, avgs: { pvPower: 300, loadPower: 800, batteryPower: 200 } },
  ]);
  assert.deepEqual(out, [
    { hourIndex: 3, avgs: { pvPower: 200, loadPower: 700, batteryPower: 0 } },
  ]);
});

test("collapseHourBuckets averages over only the buckets that carry a metric", () => {
  // A metric absent from one of the repeated buckets must not be diluted
  // toward zero by counting the bucket that lacks it.
  const out = collapseHourBuckets([
    { localHour: 3, avgs: { pvPower: 100 } },
    { localHour: 3, avgs: { loadPower: 800 } },
  ]);
  assert.deepEqual(out, [{ hourIndex: 3, avgs: { pvPower: 100, loadPower: 800 } }]);
});

test("collapseHourBuckets skips unusable buckets", () => {
  const out = collapseHourBuckets([
    { hour: "garbage" },
    { localHour: 8, avgs: { pvPower: 1900 } },
    null,
  ]);
  assert.deepEqual(out, [{ hourIndex: 8, avgs: { pvPower: 1900 } }]);
});

test("collapseHourBuckets ignores non-numeric metric values", () => {
  const out = collapseHourBuckets([
    { localHour: 8, avgs: { pvPower: 1900, batterySoc: null } },
  ]);
  assert.deepEqual(out, [{ hourIndex: 8, avgs: { pvPower: 1900 } }]);
});
