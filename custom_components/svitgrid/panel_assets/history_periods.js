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

// The Day chart's x-axis index (0-23) for an intraday bucket.
//
// The bucket's `hour` field is the UTC bucket key; `localHour` is the
// household-local wall-clock hour the server resolved it to. Always plot
// `localHour` -- deriving the index by slicing the UTC string (what this
// replaces) shifted every non-UTC household's curve by their offset, drawing
// a Kyiv household's 08:00 solar peak at 05:00.
//
// The UTC slice survives only as a fallback for a panel talking to an older
// server that does not send `localHour` yet: degrading to the old (wrong for
// non-UTC) behaviour beats rendering an empty chart.
//
// Returns null for anything unusable so the caller can skip the bucket.
export function hourIndexOf(bucket) {
  if (!bucket || typeof bucket !== "object") return null;

  const local = bucket.localHour;
  if (typeof local === "number" && Number.isInteger(local)) {
    return local >= 0 && local <= 23 ? local : null;
  }

  const hour = bucket.hour;
  if (typeof hour !== "string") return null;
  const utcHour = parseInt(hour.slice(11, 13), 10);
  if (!Number.isInteger(utcHour) || utcHour < 0 || utcHour > 23) return null;
  return utcHour;
}

// Collapse a single inverter's intraday buckets onto local hour indices,
// averaging any hour that appears more than once.
//
// On a DST fall-back day the local wall clock repeats an hour (Europe/Kyiv
// 2026-10-25: 04:00 -> 03:00), so two distinct UTC buckets resolve to the same
// localHour. The chart accumulates per hour index in order to SUM across
// inverters, so feeding it both buckets would render that hour at 2x once a
// year. Collapsing per inverter first keeps the cross-inverter sum intact.
//
// Averaging (not summing, not last-wins) is right because these are power
// AVERAGES over the bucket. The opposite rule applies to cumulative counters
// -- see hourly_energy.py's to_local_hour_rows, where later-wins is correct
// precisely because summing counters would fabricate energy.
//
// Returns [{hourIndex, avgs}] with one entry per distinct local hour.
export function collapseHourBuckets(buckets) {
  const sums = new Map(); // hourIndex -> {metric: {total, count}}

  for (const bucket of buckets || []) {
    const hourIndex = hourIndexOf(bucket);
    if (hourIndex === null) continue;
    const avgs = (bucket && bucket.avgs) || {};
    if (!sums.has(hourIndex)) sums.set(hourIndex, {});
    const acc = sums.get(hourIndex);
    for (const [metric, value] of Object.entries(avgs)) {
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      // Count per metric, not per bucket: a metric missing from one of the
      // repeated buckets must not be averaged toward zero.
      if (!acc[metric]) acc[metric] = { total: 0, count: 0 };
      acc[metric].total += value;
      acc[metric].count += 1;
    }
  }

  const out = [];
  for (const [hourIndex, acc] of sums) {
    const avgs = {};
    for (const [metric, { total, count }] of Object.entries(acc)) {
      avgs[metric] = total / count;
    }
    out.push({ hourIndex, avgs });
  }
  out.sort((a, b) => a.hourIndex - b.hourIndex);
  return out;
}

export function periodLabel(anchorStr, period) {
  if (period === "day") return anchorStr;
  if (period === "month") return anchorStr.slice(0, 7);
  if (period === "year") return anchorStr.slice(0, 4);
  return "";
}
