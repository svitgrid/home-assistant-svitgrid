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
