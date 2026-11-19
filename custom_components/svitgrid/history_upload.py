"""The way back from an island: send the history Home Assistant held to the
cloud (svitgrid D-2103).

While the cloud copy is off, readings stay in the local store. When it comes
back on, `ReadingStore.note_cloud_copy` fixes the boundary (`until`, the start
of the 5-minute bucket the return falls in) and this module uploads, per
inverter, through the cloud's history import (svitgrid D-2101, D-2102):

  1. 5-minute buckets, folded from the raw rows the way reading-core seals
     `readings_5min`, for the last 7 days (`POST .../history-import`);
  2. hourly totals from `readings_hourly`, for hours between the cloud's
     hourly retention (90 days) and the 5-minute one
     (`POST .../history-import/totals`, `hours`);
  3. daily totals from `readings_daily`, for local days older than that
     (the same route, `days`).

Counted once. The routes create a slot only when it is empty, so a slot the
cloud holds is skipped. The windows do not overlap: an hour inside the 7 days
goes up as buckets, an older one as an hourly total, and the one hour on the
seam waits until the cloud takes it hourly. Live ingest sends only readings
from `until` on; the store marks the earlier ones `skipped` for the sender.

The upload runs in its own task, so live readings never wait for it. Each step
sends one request and stores its cursor, so a restart resumes where it
stopped. It ends for good once every span is done, or 30 days after the
return, when the cloud no longer takes most of it anyway.

The fold is a port of `apps/mobile/lib/core/island/bucket_fold.dart` (the
phone's port of `packages/reading-core/src/readings-5min-accumulator.ts`), and
the inbound field names follow `packages/reading-core/src/normalize-reading.ts`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .local_time import local_day_of

_LOGGER = logging.getLogger(__name__)

# ── the cloud's windows (services/api/src/services/retention.ts) ────────
FIVE_MIN_RETENTION = timedelta(days=7)  # READINGS_5MIN_TTL_DAYS
HOURLY_RETENTION = timedelta(days=90)  # READINGS_HOURLY_TTL_DAYS
# The cloud measures its windows when the request arrives, a moment after this
# add-on measured them. These margins keep every slot sent inside them.
_EDGE_MARGIN = timedelta(minutes=10)

BUCKETS_PER_REQUEST = 288  # HISTORY_IMPORT_MAX_BUCKETS
HOURS_PER_REQUEST = 336  # HISTORY_IMPORT_MAX_HOURS
DAYS_PER_REQUEST = 366  # HISTORY_IMPORT_MAX_DAYS

# How long after the return the upload keeps trying. By then the cloud takes
# none of the 5-minute buckets and only part of the hours.
GIVE_UP_AFTER = timedelta(days=30)

PACE_S = 2  # between requests
BLOCKED_RETRY_S = 3600  # 401, 402, 403, 409: the owner or the cloud must act
_BACKOFF_BASE_S = 10
_BACKOFF_CAP_S = 300

_BUCKET = timedelta(minutes=5)
_HOUR = timedelta(hours=1)

# ── reading-core's bucket fields ────────────────────────────────────────
BUCKET_AVG_FIELDS = (
    "pvPower", "pvPower1", "pvPower2", "pvPower3", "pvPower4", "pvPower5", "pvPower6",
    "pvVoltage1", "pvCurrent1", "pvVoltage2", "pvCurrent2",
    "pvVoltage3", "pvCurrent3", "pvVoltage4", "pvCurrent4",
    "pvVoltage5", "pvCurrent5", "pvVoltage6", "pvCurrent6",
    "batteryPower", "batteryCurrent", "batterySoc", "batterySocEstimated", "batteryVoltage",
    "batteryTemperature", "batteryPower2", "batterySoc2", "batteryVoltage2",
    "gridPower", "loadPower", "upsLoadPower", "gridVoltage", "throughInverterLoad",
    "generatorPower",
)  # fmt: skip
BUCKET_MAX_FIELDS = (
    "dailyPvEnergy", "dailyImportEnergy", "dailyExportEnergy", "dailyLoadEnergy",
    "dailyGeneratorEnergy",
)  # fmt: skip
BUCKET_ARRAY_FIELDS = (
    "phaseVoltages", "phaseLoads", "phaseGridPowers", "phaseGridCurrents", "phaseLoadVoltages",
)  # fmt: skip

# `MAX_INTER_SAMPLE_ATTRIBUTION_SEC` in derive-grid-present.ts.
_MAX_INTER_SAMPLE_ATTRIBUTION_S = 60

# ── normalize-reading.ts ────────────────────────────────────────────────
_PV_STRING_ALIASES = {
    **{f"pv{n}Power": f"pvPower{n}" for n in range(1, 5)},
    **{f"pv{n}Voltage": f"pvVoltage{n}" for n in range(1, 7)},
    **{f"pv{n}Current": f"pvCurrent{n}" for n in range(1, 7)},
}
_DAILY_ENERGY_ALIASES = {
    "dailyGridImportEnergy": "dailyImportEnergy",
    "dailyGridExportEnergy": "dailyExportEnergy",
}
_PER_PHASE_GROUPS = (
    ("gridVoltageL", "phaseVoltages"),
    ("loadPowerL", "phaseLoads"),
    ("gridPowerL", "phaseGridPowers"),
    ("loadVoltageL", "phaseLoadVoltages"),
)


def _num(v: Any) -> bool:
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and v == v
        and abs(v) != float("inf")
    )


def _aliases(out: dict, aliases: dict[str, str]) -> None:
    for alias, canonical in aliases.items():
        if alias in out and _num(out[alias]) and out.get(canonical) is None:
            out[canonical] = out.pop(alias)


def normalize_inbound(payload: dict[str, Any]) -> dict[str, Any]:
    """The add-on's payload under the cloud's field names, as the ingest
    endpoints normalize it (`normalizeInboundReading`). Never mutates."""
    out = dict(payload)
    _aliases(out, _PV_STRING_ALIASES)
    total = out.get("totalPvPower")
    if _num(total) and total >= 0 and out.get("pvPower") in (None, 0):
        out["pvPower"] = out.pop("totalPvPower")
    _aliases(out, _DAILY_ENERGY_ALIASES)
    for prefix, canonical in _PER_PHASE_GROUPS:
        if out.get(canonical) is not None:
            continue
        values = []
        for phase in (1, 2, 3):
            v = out.get(f"{prefix}{phase}")
            if not _num(v):
                break
            values.append(v)
        if values:
            out[canonical] = values
            for phase in range(1, len(values) + 1):
                del out[f"{prefix}{phase}"]
    return out


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC)


def _iso_ms(dt: datetime) -> str:
    """Dart's `toIso8601String()` for a UTC instant: what the phone sends."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_s(dt: datetime) -> str:
    """The store's key shape, `YYYY-MM-DDTHH:MM:SSZ`."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _floor(dt: datetime, step: timedelta) -> datetime:
    s = int(step.total_seconds())
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % s, UTC)


def _ceil(dt: datetime, step: timedelta) -> datetime:
    f = _floor(dt, step)
    return f if f == dt else f + step


def fold_bucket(bucket_start: datetime, samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold `samples`, payloads ordered by `timestamp`, into the bucket document
    reading-core's `sealToBucketDoc` writes (`foldBucket` in bucket_fold.dart)."""
    sample_count = 0
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    maxes: dict[str, float] = {}
    arrays: dict[str, list[float]] = {}
    seen: set[datetime] = set()
    relay = grid_present = last_grid_present = None
    grid_down_s: float = 0
    first_soc = last_soc = None
    last_ts: datetime | None = None

    for s in samples:
        ts = _parse(s["timestamp"])
        if ts in seen:
            continue
        seen.add(ts)
        first = sample_count == 0
        n = sample_count
        sample_count += 1

        for f in BUCKET_AVG_FIELDS:
            v = s.get(f)
            if _num(v):
                sums[f] = sums.get(f, 0) + v
                counts[f] = counts.get(f, 0) + 1
        for f in BUCKET_MAX_FIELDS:
            v = s.get(f)
            if _num(v) and (f not in maxes or v > maxes[f]):
                maxes[f] = v
        for f in BUCKET_ARRAY_FIELDS:
            v = s.get(f)
            if not isinstance(v, list) or not all(_num(x) for x in v):
                continue
            cur = arrays.get(f)
            if cur is None:
                arrays[f] = list(v)
                continue
            # reading-core divides by the bucket's sample count before this
            # sample, not by how many samples carried the array. Kept for parity.
            arrays[f] = [
                *((cur[i] * n + v[i]) / (n + 1) if i < len(v) else cur[i] for i in range(len(cur))),
                *v[len(cur) :],
            ]

        incoming_relay = s.get("gridRelayClosed")
        incoming_grid = s.get("gridPresent") if isinstance(s.get("gridPresent"), bool) else None
        soc = s.get("batterySoc") if _num(s.get("batterySoc")) else None
        if first:
            relay = incoming_relay if isinstance(incoming_relay, bool) else None
            grid_present = last_grid_present = incoming_grid
            first_soc = last_soc = soc
            last_ts = ts
            continue
        if relay is False or incoming_relay is False:
            relay = False
        elif incoming_relay is True:
            relay = True
        if grid_present is False or incoming_grid is False:
            grid_present = False
        elif incoming_grid is True:
            grid_present = True
        # A down interval is charged to the state at its start, capped per gap.
        if last_grid_present is False:
            delta = (ts - last_ts).total_seconds()
            grid_down_s += min(_MAX_INTER_SAMPLE_ATTRIBUTION_S, max(0, delta))
        if ts >= last_ts:
            last_ts = ts
            last_grid_present = incoming_grid
            if soc is not None:
                last_soc = soc

    doc: dict[str, Any] = {"timestamp": _iso_ms(bucket_start), "sampleCount": sample_count}
    for f in BUCKET_AVG_FIELDS:
        if counts.get(f):
            doc[f] = sums[f] / counts[f]
    for f in BUCKET_MAX_FIELDS:
        if f in maxes:
            doc[f] = maxes[f]
    for f in BUCKET_ARRAY_FIELDS:
        if f in arrays:
            doc[f] = list(arrays[f])
    doc["gridPresent"] = grid_present
    doc["gridDownSeconds"] = grid_down_s
    doc["lastSampleTimestamp"] = _iso_ms(last_ts) if last_ts else None
    doc["lastSampleGridPresent"] = last_grid_present
    doc["gridRelayClosed"] = relay
    if first_soc is not None:
        doc["firstSampleSoc"] = first_soc
    if last_soc is not None:
        doc["lastSampleSoc"] = last_soc
    return doc


def _bucket_json(doc: dict[str, Any]) -> dict[str, Any]:
    """A folded bucket in the route's shape: `ts` names the bucket."""
    out = {k: v for k, v in doc.items() if k != "timestamp"}
    return {"ts": doc["timestamp"], **out}


def fold_buckets(
    payloads: list[dict[str, Any]], lo: datetime, hi: datetime
) -> list[dict[str, Any]]:
    """The raw `payloads` with `lo <= timestamp < hi`, folded per 5-minute bucket."""
    by_bucket: dict[datetime, list[dict]] = {}
    for p in payloads:
        try:
            t = _parse(p["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= t < hi:
            by_bucket.setdefault(_floor(t, _BUCKET), []).append(normalize_inbound(p))
    out = []
    for start in sorted(by_bucket):
        samples = sorted(by_bucket[start], key=lambda s: _parse(s["timestamp"]))
        out.append(_bucket_json(fold_bucket(start, samples)))
    return out


# ── hourly and daily totals ─────────────────────────────────────────────

# The cloud's hourly averages by the field each averages.
_HOUR_AVG_FIELDS = {
    "pvPower": "avgPvPower",
    "gridPower": "avgGridPower",
    "loadPower": "avgLoadPower",
    "batteryPower": "avgBatteryPower",
    "batterySoc": "avgBatterySoc",
    "generatorPower": "avgGeneratorPower",
}
# The cloud's hourly counter maxima by the add-on's counter name.
_HOUR_COUNTER_FIELDS = {
    "dailyPvEnergy": "dailyPvEnergyMaxSeen",
    "dailyGridImportEnergy": "dailyImportEnergyMaxSeen",
    "dailyGridExportEnergy": "dailyExportEnergyMaxSeen",
    "dailyLoadEnergy": "dailyLoadEnergyMaxSeen",
    "dailyGeneratorEnergy": "dailyGeneratorEnergyMaxSeen",
}


def cloud_hour(row: dict[str, Any]) -> dict[str, Any] | None:
    """One `readings_hourly` row in the cloud's `readings_hourly` shape, as the
    totals route takes it (`foldCloudHour` in bucket_fold.dart, from an hour
    the add-on already folded).

    Energy is the hour's average power held for the hour. A field the row
    lacks stays absent, never 0. SOC at the hour's edges is unknown and sent
    as such. Returns None when the row carries no power: the route skips a
    total with no energy.
    """
    avgs = row.get("avgs") or {}
    energy = row.get("energy") or {}
    avg = {cloud: avgs[f] for f, cloud in _HOUR_AVG_FIELDS.items() if _num(avgs.get(f))}
    if not avg:
        return None

    def pos(v):
        return v / 1000 if v > 0 else 0

    def neg(v):
        return -v / 1000 if v < 0 else 0

    out: dict[str, Any] = {"ts": _iso_ms(_parse(row["hour_start"]))}
    n = row.get("sample_count")
    if isinstance(n, int) and n >= 1:
        out["sampleCount"] = n
    out.update(avg)
    for field, energy_key, fn in (
        ("avgPvPower", "pvEnergy", pos),
        ("avgGridPower", "importEnergy", pos),
        ("avgGridPower", "exportEnergy", neg),
        ("avgLoadPower", "loadEnergy", pos),
        ("avgBatteryPower", "batteryChargeEnergy", pos),
        ("avgBatteryPower", "batteryDischargeEnergy", neg),
        ("avgGeneratorPower", "generatorEnergy", pos),
    ):
        if field in avg:
            out[energy_key] = fn(avg[field])
    phases = []
    for phase in (1, 2, 3):
        v = avgs.get(f"gridVoltageL{phase}")
        if not _num(v):
            break
        phases.append(v)
    if phases:
        out["phaseVoltages"] = phases
    for f, cloud in _HOUR_COUNTER_FIELDS.items():
        if _num(energy.get(f)):
            out[cloud] = energy[f]
    return out


def cloud_day(day: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """One `readings_daily` row as the cloud's sealed `daily/{date}` document,
    in the totals route's shape (`cloudDayTotals` in bucket_fold.dart). The
    battery counters are the inverter's own day totals, so they stand in for
    the cloud's integrated battery energy. None when the day has no energy: a
    day the add-on holds no counters for is skipped, never sent as 0."""
    energy = row.get("energy") or {}
    peaks = row.get("peaks") or {}
    names = {
        "dailyPvEnergy": "dailyPvEnergy",
        "dailyGridImportEnergy": "dailyImportEnergy",
        "dailyGridExportEnergy": "dailyExportEnergy",
        "dailyLoadEnergy": "dailyLoadEnergy",
        "dailyGeneratorEnergy": "dailyGeneratorEnergy",
        "dailyBatteryChargeEnergy": "batteryChargeEnergyIntegrated",
        "dailyBatteryDischargeEnergy": "batteryDischargeEnergyIntegrated",
    }
    totals = {cloud: energy[f] for f, cloud in names.items() if _num(energy.get(f))}
    if not totals:
        return None
    out: dict[str, Any] = {"date": day, **totals}
    if _num(peaks.get("pvPower")):
        out["peakPvPower"] = peaks["pvPower"]
    if _num(peaks.get("loadPower")):
        out["peakLoadPower"] = peaks["loadPower"]
    return out


# ── the upload ──────────────────────────────────────────────────────────


def _finished(mark: Any) -> bool:
    return isinstance(mark, str) and (mark == "done" or mark.startswith("refused"))


class HistoryUploader:
    """Sends one device's island history, one request per `step`.

    `step` returns `(result, wait_s)`:
      "sent"     a request went up; call again after `wait_s`
      "wait"     the next slot is not yet in the cloud's window
      "retry"    a transient failure; the same request goes again
      "blocked"  the cloud refused the caller (401, 402, 403, 409)
      "done"     nothing is left, for good
    """

    def __init__(
        self,
        *,
        store,
        api_client,
        api_key: str,
        household_id: str,
        device_id: str,
        inverter_ids: list[str],
        tz_name: str | None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._api = api_client
        self._api_key = api_key
        self._household_id = household_id
        self._device_id = device_id
        self._inverter_ids = list(inverter_ids)
        self._tz = tz_name
        self._now = now or (lambda: datetime.now(UTC))
        self._failures = 0

    async def step(self) -> tuple[str, float]:
        state = await self._store.history_state(self._device_id)
        if not state or state.get("status") != "uploading":
            return "done", 0
        now = self._now()
        if now - _parse(state["returnAt"]) > GIVE_UP_AFTER:
            _LOGGER.warning("island history upload given up 30 days after the return")
            return await self._finish(state, "expired")
        inverters = state.setdefault("inverters", {})
        for inv in self._inverter_ids:
            progress = inverters.setdefault(inv, {})
            for phase in ("buckets", "hours", "days"):
                # A phase returns None when it moved its cursor without a
                # request, or finished; it runs again until it is finished.
                while not _finished(progress.get(phase)):
                    result = await getattr(self, f"_{phase}")(state, inv, progress, now)
                    if result is not None:
                        return result
        return await self._finish(state, "complete")

    async def _finish(self, state: dict, reason: str) -> tuple[str, float]:
        state["status"] = "done"
        state["reason"] = reason
        await self._store.set_history_state(self._device_id, state)
        _LOGGER.info("island history upload finished (%s)", reason)
        return "done", 0

    async def _save(self, state: dict) -> None:
        await self._store.set_history_state(self._device_id, state)

    # 1. 5-minute buckets ------------------------------------------------
    async def _buckets(self, state, inv, progress, now):
        until = _parse(state["until"])
        # From the first whole hour inside the cloud's 5-minute window, so the
        # hour below it goes up whole as an hourly total. It only moves later:
        # after a long stop, the hours phase covers what fell out.
        bucket_from = _ceil(now - FIVE_MIN_RETENTION + _EDGE_MARGIN, _HOUR)
        if progress.get("bucketFrom"):
            bucket_from = max(bucket_from, _parse(progress["bucketFrom"]))
        progress["bucketFrom"] = _iso_s(bucket_from)
        lo = bucket_from
        if progress.get("bucketCursor"):
            lo = max(lo, _parse(progress["bucketCursor"]))
        if lo >= until:
            progress["buckets"] = "done"
            await self._save(state)
            return None
        first = await self._store.first_raw_ts(inv, _iso_s(lo), _iso_s(until))
        if first is None:
            progress["buckets"] = "done"
            await self._save(state)
            return None
        lo = max(lo, _floor(_parse(first), _BUCKET))
        hi = min(until, lo + BUCKETS_PER_REQUEST * _BUCKET)
        # One second either side: the store's `ts` strings may carry fractions.
        payloads = await self._store.raw_payloads(
            inv, _iso_s(lo - timedelta(seconds=1)), _iso_s(hi + timedelta(seconds=1))
        )
        buckets = fold_buckets(payloads, lo, hi)
        if buckets:
            outcome = await self._send(
                state, progress, "buckets",
                lambda: self._api.import_history(self._api_key, self._household_id, inv, buckets),
            )  # fmt: skip
            if outcome is not None:
                return outcome
        progress["bucketCursor"] = _iso_s(hi)
        await self._save(state)
        return ("sent", PACE_S) if buckets else None

    # 2. hourly totals ---------------------------------------------------
    async def _hours(self, state, inv, progress, now):
        until = _parse(state["until"])
        hours_from = _ceil(now - HOURLY_RETENTION, _HOUR) + _HOUR
        bucket_from = _parse(progress["bucketFrom"]) if progress.get("bucketFrom") else until
        target = min(bucket_from, _ceil(until, _HOUR))
        # An hour is the cloud's to take hourly once it ended 7 days ago.
        eligible_to = min(target, _floor(now - FIVE_MIN_RETENTION, _HOUR))
        lo = hours_from
        if progress.get("hourCursor"):
            lo = max(lo, _parse(progress["hourCursor"]))
        if lo >= target:
            progress["hours"] = "done"
            await self._save(state)
            return None
        if lo >= eligible_to:
            first = await self._store.first_hour(inv, _iso_s(lo), _iso_s(target))
            if first is None:
                progress["hours"] = "done"
                await self._save(state)
                return None
            ready_at = _parse(first) + _HOUR + FIVE_MIN_RETENTION + _EDGE_MARGIN
            return "wait", max(1.0, (ready_at - now).total_seconds())
        first = await self._store.first_hour(inv, _iso_s(lo), _iso_s(eligible_to))
        if first is None:
            progress["hourCursor"] = _iso_s(eligible_to)
            await self._save(state)
            return await self._hours(state, inv, progress, now)
        lo = max(lo, _parse(first))
        hi = min(eligible_to, lo + HOURS_PER_REQUEST * _HOUR)
        rows = await self._store.hourly_rows(inv, _iso_s(lo), _iso_s(hi))
        hours = [h for h in (cloud_hour(r) for r in rows) if h is not None]
        if hours:
            outcome = await self._send(
                state, progress, "hours",
                lambda: self._api.import_history_totals(
                    self._api_key, self._household_id, inv, hours=hours
                ),
            )  # fmt: skip
            if outcome is not None:
                return outcome
        progress["hourCursor"] = _iso_s(hi)
        await self._save(state)
        return ("sent", PACE_S) if hours else await self._hours(state, inv, progress, now)

    # 3. daily totals ----------------------------------------------------
    async def _days(self, state, inv, progress, now):
        # The local days before the first day the hourly retention reaches.
        cutoff = local_day_of(_iso_s(now - HOURLY_RETENTION), self._tz)
        rows = await self._store.daily_rows(
            inv, progress.get("dayCursor"), cutoff, DAYS_PER_REQUEST
        )
        if not rows:
            progress["days"] = "done"
            await self._save(state)
            return None
        days = [d for d in (cloud_day(r["day"], r) for r in rows) if d is not None]
        if days:
            outcome = await self._send(
                state, progress, "days",
                lambda: self._api.import_history_totals(
                    self._api_key, self._household_id, inv, days=days
                ),
            )  # fmt: skip
            if outcome is not None:
                return outcome
        progress["dayCursor"] = rows[-1]["day"]
        await self._save(state)
        return ("sent", PACE_S) if days else None

    async def _send(self, state, progress, phase, call) -> tuple[str, float] | None:
        """Send one request. None once the cloud took it, or once it refused
        this inverter's span for good (400, 404, 413: the span is left out);
        otherwise the step's result."""
        try:
            status, body = await call()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("island history upload: %s request failed: %s", phase, exc)
            status, body = None, None
        if status is not None and 200 <= status < 300:
            self._failures = 0
            return None
        if status in (400, 404, 413):
            _LOGGER.warning(
                "island history upload: the cloud refused the %s (%s %s); left out",
                phase,
                status,
                body,
            )
            progress[phase] = f"refused {status}"
            await self._save(state)
            return None
        if status in (401, 402, 403, 409):
            _LOGGER.info("island history upload held: %s %s", status, body)
            return "blocked", BLOCKED_RETRY_S
        # 5xx, 429, a dropped connection.
        self._failures += 1
        delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (self._failures - 1))
        return "retry", delay


async def run_history_upload(
    uploader: HistoryUploader,
    *,
    sleep=asyncio.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Step `uploader` until it is done. Never raises: the live sender and
    every other loop keep running whatever happens here."""
    while not should_stop():
        try:
            result, wait_s = await uploader.step()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("island history upload step failed")
            result, wait_s = "retry", BLOCKED_RETRY_S
        if result == "done":
            return
        await sleep(wait_s)
