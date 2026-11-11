"""The hourly island status report (svitgrid D-1719; the API side is D-1715).

A true island (island mode on, cloud copy off) sends no readings to the cloud,
so the landing map and the fleet dashboard cannot count it. Once an hour the
add-on reports a small status for every inverter it holds to
`POST /api/v1/island-report`, with its cloud API key, which island mode does
not revoke:

- its own inverters as host `ha`,
- the inverters of an adopted edge box (edge island mode) as host `edge`.

Per inverter: the latest solar power, the solar energy of the last hour from
the local store, and the latest battery charge (or null). An inverter with no
reading in the last hour is left out, so a silent one is never counted active.

Every answer, refusals included, carries `{nextReportAfterSec, stop}`. The
add-on keeps it in the store's meta table so a restart neither reports early
nor forgets a stop. `stop` ends the loop until the next start, and the next
start checks again no sooner than a day after the last call. With the cloud
copy on the loop is never started (`async_setup_entry`): the readings already
count the station.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 3600
# With `stop`, check again no more than once a day.
STOP_RECHECK_S = 24 * 60 * 60
# Bounds on the server's interval, so a typo there can neither hammer the API
# nor silence the station for good. The API treats under 15 min as a typo too.
MIN_INTERVAL_S = 15 * 60
MAX_INTERVAL_S = 7 * 24 * 60 * 60
WINDOW_S = 3600


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _parse_ts(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, UTC).isoformat().replace("+00:00", "Z")


def pv_energy_kwh(baseline: dict | None, rows: list[tuple[float, dict]]) -> float:
    """Solar energy over *rows* (oldest first, `(epoch_s, payload)`), in kWh.

    From the daily counter `dailyPvEnergy` when the inverter reports one: the
    sum of its rises since *baseline*, the last reading before the window. A
    drop is the reset at local midnight, and the value after it counts from
    zero. Without a counter, solar power integrated over the samples.
    """
    prev = _num((baseline or {}).get("dailyPvEnergy"))
    total = 0.0
    seen_counter = False
    for _, payload in rows:
        cur = _num(payload.get("dailyPvEnergy"))
        if cur is None:
            continue
        seen_counter = True
        if prev is not None:
            total += cur - prev if cur >= prev else cur
        prev = cur
    if seen_counter:
        return max(0.0, total)

    joules = 0.0
    for (t0, p0), (t1, _) in zip(rows, rows[1:], strict=False):
        watts = _num(p0.get("pvPower"))
        if watts is not None and watts > 0 and t1 > t0:
            joules += watts * (t1 - t0)
    return joules / 3_600_000


async def build_report(
    store: Any, inverters: list[tuple[str, str]], *, now_s: float
) -> list[dict[str, Any]]:
    """The report's `inverters` list for `(inverter_id, host)` pairs."""
    since = _iso(now_s - WINDOW_S)
    out: list[dict[str, Any]] = []
    for inverter_id, host in inverters:
        baseline, raw = await store.island_report_rows(inverter_id, since)
        rows = [(t, p) for ts, p in raw if (t := _parse_ts(ts)) is not None]
        if not rows:
            continue
        latest = rows[-1][1]
        soc = _num(latest.get("batterySoc"))
        out.append(
            {
                "inverterId": inverter_id,
                "host": host,
                "pvPowerW": round(max(0.0, _num(latest.get("pvPower")) or 0.0)),
                "pvEnergyKwhHour": round(pv_energy_kwh(baseline, rows), 3),
                "batterySoc": None if soc is None else round(min(100.0, max(0.0, soc))),
            }
        )
    return out


def _interval(state: dict) -> float:
    v = _num(state.get("nextReportAfterSec"))
    v = DEFAULT_INTERVAL_S if v is None else min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, v))
    return max(v, STOP_RECHECK_S) if state.get("stop") is True else v


def seconds_until_due(state: dict | None, now_s: float) -> float:
    """Seconds until the next call. 0 on a first start."""
    if not state:
        return 0
    last = _num(state.get("lastAttemptAt"))
    if last is None:
        return 0
    interval = _interval(state)
    # A clock set back cannot push the next call past one interval.
    return min(interval, max(0.0, last + interval - now_s))


async def report_once(
    *,
    store: Any,
    api_client: Any,
    api_key: str,
    inverters: list[tuple[str, str]],
    now_s: float,
) -> dict[str, Any]:
    """Report once and return the state the next call is timed from.

    The cloud's directive is saved. With nothing to report the cloud is not
    called and nothing is saved, so a saved stop survives; the next try is
    an hour later.
    """
    entries = await build_report(store, inverters, now_s=now_s)
    if not entries:
        _LOGGER.debug("Island report: no inverter read in the last hour, nothing sent")
        return {"lastAttemptAt": now_s, "nextReportAfterSec": DEFAULT_INTERVAL_S, "stop": False}

    body = None
    try:
        status, body = await api_client.report_island_status(api_key, entries)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Island report failed: %s", err)
        status = None
    if isinstance(body, dict) and _num(body.get("nextReportAfterSec")) is not None:
        state = {
            "lastAttemptAt": now_s,
            "nextReportAfterSec": min(
                MAX_INTERVAL_S, max(MIN_INTERVAL_S, _num(body["nextReportAfterSec"]))
            ),
            "stop": body.get("stop") is True,
        }
        _LOGGER.debug(
            "Island report: status=%s accepted=%s next in %ss stop=%s",
            status,
            body.get("accepted"),
            state["nextReportAfterSec"],
            state["stop"],
        )
    else:
        # No directive (offline, 5xx, a body that is not JSON): try again in
        # an hour.
        state = {"lastAttemptAt": now_s, "nextReportAfterSec": DEFAULT_INTERVAL_S, "stop": False}
    await store.set_island_report_state(state)
    return state


async def run_island_report_loop(
    *,
    store: Any,
    api_client: Any,
    api_key: str,
    inverters: Callable[[], list[tuple[str, str]]],
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Report on the cloud's schedule until it says stop."""
    try:
        state = await store.get_island_report_state()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Island report: saved state unreadable, reporting now")
        state = None
    while True:
        await sleep(seconds_until_due(state, now()))
        try:
            state = await report_once(
                store=store,
                api_client=api_client,
                api_key=api_key,
                inverters=inverters(),
                now_s=now(),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Island report failed")
            state = {"lastAttemptAt": now(), "nextReportAfterSec": DEFAULT_INTERVAL_S}
        if state.get("stop") is True:
            _LOGGER.info(
                "Island report: the cloud asked this island to stop reporting; "
                "it checks again after the next start, no sooner than a day from now"
            )
            return
