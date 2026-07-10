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
