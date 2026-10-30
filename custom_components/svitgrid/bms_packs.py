"""The battery packs an adopted edge box reads, from its `bms` reports.

On an edge island the box sends its BMS report on the ingest batch's `bms`
(`BmsReport`, `services/api/src/models/schemas.ts`) to Home Assistant instead
of the cloud. It is kept here as the cloud keeps it:

- the box's latest report, its packs replaced wholesale on every report so a
  measurement the pack stopped reporting is gone, not stale
  (`bms-state-writer.ts`, `edgeDevices/{id}.bms`);
- a history row per new H2 cycle, for 14 days (`bmsHistory`);
- a 5-minute sample per pack read (`bms-pack-history.ts`, `bmsDays`), kept 8
  days;
- a summary per household-local day, sealed once the day is over
  (`bms-day-summary.ts`, the days of `bmsMonths`), for the battery report.

It also keeps each box's desired roster, the packs Home Assistant wants it to
read, with its epoch (`bmsRosters`, `bmsPacks`); `bms_roster.py` sends it to
the box (plan Task 45).

SQLite in `bms_packs.db` beside `zigbee_devices.db`, the
`edge_command_queue.py` pattern: sync `_*_sync` methods that open, operate and
close the connection, run in HA's executor pool.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .local_time import _zone

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()

PACK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_PACKS = 16

# `NAMING_RESIDUAL_FRACTION` (`bms-state-writer.ts`).
NAMING_RESIDUAL_FRACTION = 0.1
# `BMS_HISTORY_TTL_DAYS`.
HISTORY_TTL = timedelta(days=14)
# `BMS_DAYS_TTL_DAYS` + 1: a sample lives until its UTC day is 8 days old.
SAMPLE_TTL_DAYS = 8
# `SAME_READ_TOLERANCE_MS` (`bms-pack-history.ts`).
SAME_READ_TOLERANCE_MS = 30_000
SLOT_MS = 5 * 60_000
DAY_MS = 86_400_000
# `BMS_HISTORY_MAX_RANGE_MS` and the range that still carries every cell.
HISTORY_MAX_RANGE_MS = 8 * DAY_MS
HISTORY_CELLS_MAX_RANGE_MS = 36 * 3_600_000
# `bms-day-summary.ts`.
GAP_MS = 15 * 60_000
REST_CURRENT_MA = 1000
CAP_MIN_SWING = 40
# `BMS_REPORT_LIVE_DAYS_BACK` and `BMS_REPORT_MAX_RANGE_DAYS` (`bms-report.ts`).
REPORT_LIVE_DAYS_BACK = 7
REPORT_MAX_RANGE_DAYS = 366

# Every optional key of a stored pack (`MEASUREMENT_KEYS`).
_MEASUREMENT_KEYS = (
    "label", "adoptedAt", "name",
    "ageMs", "driver", "rssi", "attempts", "okAttempts", "state",
    "stage", "reason", "reasonText", "skippedRecord",
    "linkTimeoutMs", "linkItvlMs", "linkLatency", "peerUpdateReqs", "peerReqTimeoutMs",
    "linkMtu", "writeMode", "notifyLens", "notifyCount", "missedCmds",
    "driverAlternatives",
    "driverSource", "suggestedDriver", "readWith",
    "addrType",
    "packMv", "currentMa", "powerW", "soc", "soh",
    "remainingMah", "nominalMah", "cycleMah", "cycles", "runtimeS",
    "cellsMv", "cellMinMv", "cellMaxMv", "cellDeltaMv", "cellAvgMv",
    "cellMinIdx", "cellMaxIdx", "tempsC",
    "mosCharge", "mosDischarge", "balancer",
    "framesSeen", "framesBad", "bytesDropped", "truncated",
    "trail", "stageTally", "reasonTally", "seenAgoS",
    "silentAfterFail", "advHeard", "advConnectable",
)  # fmt: skip

# What a history row keeps of each pack (`HISTORY_PACK_KEYS`): no cells.
_HISTORY_PACK_KEYS = (
    "id", "result", "stage", "reason", "driver", "readWith", "rssi",
    "attempts", "okAttempts", "everRead", "ageMs",
    "linkItvlMs", "linkTimeoutMs", "peerUpdateReqs", "peerReqTimeoutMs",
    "framesSeen", "framesBad", "bytesDropped", "skippedRecord",
    "trail", "stageTally", "reasonTally", "seenAgoS",
    "silentAfterFail", "advHeard", "advConnectable",
    "soc", "packMv", "currentMa", "residualMv",
)  # fmt: skip


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _is_num(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _js_round(value: float) -> int:
    """`Math.round`: halves round up."""
    return math.floor(value + 0.5)


def _r1(value: float) -> float:
    return _js_round(value * 10) / 10


def _r3(value: float) -> float:
    return _js_round(value * 1000) / 1000


def valid_report(value) -> dict | None:
    """The report when its shape is one the cloud accepts, else None. The
    cloud drops the whole `bms` on a bad pack (`.catch(undefined)`) and keeps
    the readings beside it; so does this."""
    if not isinstance(value, dict):
        return None
    packs = value.get("packs")
    if not isinstance(packs, list) or len(packs) > MAX_PACKS:
        return None
    for pack in packs:
        if (
            not isinstance(pack, dict)
            or not isinstance(pack.get("id"), str)
            or not PACK_ID_RE.match(pack["id"])
            or not isinstance(pack.get("result"), str)
            or not isinstance(pack.get("everRead"), bool)
        ):
            return None
    cycles = value.get("cycles")
    if cycles is not None and (type(cycles) is not int or cycles < 0):
        return None
    return value


def resolve_result(pack: dict) -> tuple[str, int | None]:
    """`resolveBmsResult`: a pack whose cells sum too far from its voltage is
    downgraded to `selfcheck_failed`; never upgraded."""
    cells, pack_mv = pack.get("cellsMv"), pack.get("packMv")
    if not isinstance(cells, list) or not cells or not _is_num(pack_mv):
        return pack["result"], None
    residual = sum(cells) - pack_mv
    if pack_mv <= 0 or abs(residual) > pack_mv * NAMING_RESIDUAL_FRACTION:
        return "selfcheck_failed", residual
    return pack["result"], residual


def build_pack_doc(pack: dict) -> dict:
    """`buildPackDoc`: one pack, normalised. Absent measurements stay absent,
    and a null is not stored."""
    result, residual = resolve_result(pack)
    out: dict[str, Any] = {"id": pack["id"], "result": result, "everRead": pack["everRead"]}
    if residual is not None:
        out["residualMv"] = residual
    for key in _MEASUREMENT_KEYS:
        if pack.get(key) is not None:
            out[key] = pack[key]
    driver = pack.get("driver")
    if (
        "cycles" not in out
        and isinstance(driver, str)
        and driver.startswith("daly")
        and _is_num(pack.get("cycleMah"))
    ):
        out["cycles"] = pack["cycleMah"]
    if result == "ok" and (not pack.get("readWith") or pack.get("readWith") == driver):
        out.pop("suggestedDriver", None)
    return out


def new_roster_generation() -> str:
    """`newRosterGeneration`."""
    return secrets.token_hex(8)


def _desired_of(pack: dict) -> dict:
    """A reported pack as an entry of the desired roster."""
    return {
        "packId": pack["id"],
        "label": pack.get("label"),
        "driver": pack.get("driver"),
        "driverSource": "owner" if pack.get("driverSource") == "owner" else None,
        "addrType": pack.get("addrType") if pack.get("addrType") in (0, 1) else None,
        "adoptedAt": pack.get("adoptedAt") if _is_num(pack.get("adoptedAt")) else None,
    }


def _synced(packs: list[dict]) -> list[dict]:
    """The roster as `bms_sync` carries it: everything but the label."""
    return [{k: v for k, v in p.items() if k != "label"} for p in packs]


def _read_at_ms(pack: dict, received_ms: int) -> int:
    """`bmsReadAtMs`: when the pack was read."""
    age = pack.get("ageMs")
    return received_ms - (age if _is_num(age) else 0)


def build_sample(pack: dict, read_at_ms: int) -> dict | None:
    """`buildBmsSample`: the slot's sample of a good read, or None."""
    if pack.get("everRead") is not True or resolve_result(pack)[0] != "ok":
        return None
    sample: dict[str, Any] = {"at": read_at_ms // 1000, "src": "hub"}
    for key, field in (("v", "packMv"), ("i", "currentMa"), ("soc", "soc"), ("soh", "soh")):
        if pack.get(field) is not None:
            sample[key] = pack[field]
    if pack.get("remainingMah") is not None:
        sample["rem"] = pack["remainingMah"]
    if isinstance(pack.get("cellsMv"), list) and pack["cellsMv"]:
        sample["c"] = pack["cellsMv"]
    if isinstance(pack.get("tempsC"), list) and pack["tempsC"]:
        sample["t"] = pack["tempsC"]
    for key, field in (("mc", "mosCharge"), ("md", "mosDischarge"), ("b", "balancer")):
        if pack.get(field) is not None:
            sample[key] = pack[field]
    if pack.get("state") is not None:
        sample["st"] = pack["state"]
    driver = pack.get("driver") if isinstance(pack.get("driver"), str) else ""
    if _is_num(pack.get("cycles")):
        sample["cyc"] = pack["cycles"]
    if _is_num(pack.get("cycleMah")):
        if driver.startswith("daly"):
            sample.setdefault("cyc", pack["cycleMah"])
        elif driver.startswith("ant"):
            sample["thr"] = pack["cycleMah"]
    return sample if any(k in sample for k in ("v", "i", "soc", "c")) else None


def _is_new_read(previous: dict | None, pack: dict, received_ms: int) -> bool:
    """`isNewBmsRead`: the box re-sends one read on every ingest."""
    prev = next((p for p in (previous or {}).get("packs", []) if p.get("id") == pack["id"]), None)
    if prev is None or prev.get("result") != "ok":
        return True
    was = _read_at_ms(prev, previous["receivedAtMs"])
    return abs(_read_at_ms(pack, received_ms) - was) > SAME_READ_TOLERANCE_MS


def _slot_of(read_at_ms: int) -> tuple[str, str]:
    """`bmsSlotOf`: the UTC date and `HHMM` of the read's 5-minute slot."""
    start = datetime.fromtimestamp((read_at_ms // SLOT_MS) * SLOT_MS / 1000, UTC)
    return start.strftime("%Y-%m-%d"), start.strftime("%H%M")


# ── the day summary (`bms-day-summary.ts`, without `hourly`) ──────────────────


def local_day_range_ms(date: str, tz_name: str | None) -> tuple[int, int]:
    """The local day's `[start, end)` in epoch ms."""
    zone = _zone(tz_name)
    day = datetime.strptime(date, "%Y-%m-%d")
    nxt = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)
    return _ms(start), _ms(end)


def local_date(ms: int, tz_name: str | None) -> str:
    return datetime.fromtimestamp(ms / 1000, _zone(tz_name)).strftime("%Y-%m-%d")


def _shift_date(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _has_cells(s: dict) -> bool:
    c = s.get("c")
    return isinstance(c, list) and bool(c) and all(_is_num(x) for x in c)


def _spread(c: list) -> float:
    return max(c) - min(c)


def _per_index_min_max(arrays: list[list]) -> tuple[list, list]:
    length = max(len(a) for a in arrays)
    lo: list = [None] * length
    hi: list = [None] * length
    for a in arrays:
        for k, x in enumerate(a):
            if not _is_num(x):
                continue
            if lo[k] is None or x < lo[k]:
                lo[k] = x
            if hi[k] is None or x > hi[k]:
                hi[k] = x
    return lo, hi


def _median(xs: list) -> float:
    s = sorted(xs)
    m = len(s) >> 1
    return s[m] if len(s) % 2 else _js_round((s[m - 1] + s[m]) / 2)


def _mos_off_runs(all_s: list[dict], day: list[dict], counted) -> list[dict]:
    runs = []
    index = {id(s): k for k, s in enumerate(all_s)}

    def nxt(s):
        k = index[id(s)] + 1
        return all_s[k] if k < len(all_s) else None

    for key, which in (("mc", "charge"), ("md", "discharge")):
        run = None
        for k, s in enumerate(day):
            prev = day[k - 1] if k > 0 else None
            contiguous = prev is not None and (s["at"] - prev["at"]) * 1000 <= GAP_MS
            if s.get(key) == 0:
                if run is None or not contiguous:
                    if run is not None:
                        runs.append(
                            {"at": run[0], "which": which, "min": _js_round(run[1] / 60_000)}
                        )
                    run = [s["at"], 0]
                run[1] += counted(s, nxt(s))
            elif run is not None:
                runs.append({"at": run[0], "which": which, "min": _js_round(run[1] / 60_000)})
                run = None
        if run is not None:
            runs.append({"at": run[0], "which": which, "min": _js_round(run[1] / 60_000)})
    return sorted(runs, key=lambda r: (r["at"], r["which"]))


def _capacity_estimate(day: list[dict]) -> float | None:
    best = None
    run = None

    def close():
        nonlocal best, run
        if run is not None:
            swing = abs(run["end"] - run["start"])
            if swing >= CAP_MIN_SWING and run["ah"] > 0 and (best is None or swing > best[0]):
                best = (swing, _r1(run["ah"] / (swing / 100)))
        run = None

    for k in range(len(day) - 1):
        a, b = day[k], day[k + 1]
        ms = (b["at"] - a["at"]) * 1000
        if (
            not _is_num(a.get("soc"))
            or not _is_num(b.get("soc"))
            or not _is_num(a.get("i"))
            or ms <= 0
            or ms > GAP_MS
        ):
            close()
            continue
        step = (b["soc"] > a["soc"]) - (b["soc"] < a["soc"])
        if run is None or (step != 0 and run["dir"] != 0 and step != run["dir"]):
            close()
            run = {"dir": 0, "start": a["soc"], "end": a["soc"], "ah": 0.0}
        if run["dir"] == 0 and step != 0:
            run["dir"] = step
        run["end"] = b["soc"]
        ah = abs(a["i"]) * ms / 1000 / 3_600_000
        if run["dir"] == 0 or (run["dir"] == -1) == (a["i"] > 0):
            run["ah"] += ah
    close()
    return best[1] if best is not None else None


def build_day_summary(
    samples: list[dict], date: str, tz_name: str | None, nominal_ah: float | None = None
) -> dict | None:
    """`buildBmsDaySummary` for one pack's local day, without `hourly` (the
    report drops it). None for a day with no sample."""
    start_ms, end_ms = local_day_range_ms(date, tz_name)
    from_ms, to_ms = start_ms - GAP_MS, end_ms + GAP_MS
    all_s = sorted(
        (s for s in samples if _is_num(s.get("at")) and from_ms <= s["at"] * 1000 < to_ms),
        key=lambda s: s["at"],
    )
    day = [s for s in all_s if start_ms <= s["at"] * 1000 < end_ms]
    if not day:
        return None
    out: dict[str, Any] = {"n": len(day), "gaps": 0}

    socs = [s for s in day if _is_num(s.get("soc"))]
    if socs:
        out["socMin"] = min(s["soc"] for s in socs)
        out["socMax"] = max(s["soc"] for s in socs)
        out["socEnd"] = socs[-1]["soc"]
    vs = [s["v"] for s in day if _is_num(s.get("v"))]
    if vs:
        out["vMin"], out["vMax"] = min(vs), max(vs)
    currents = [s["i"] for s in day if _is_num(s.get("i"))]
    if currents:
        out["iMaxChg"] = max(0, *(-i for i in currents))
        out["iMaxDchg"] = max(0, *currents)
    temps = [s["t"] for s in day if isinstance(s.get("t"), list) and s["t"]]
    if temps:
        out["tMin"], out["tMax"] = _per_index_min_max(temps)

    celled = [s for s in day if _has_cells(s)]
    if celled:
        out["cMin"], out["cMax"] = _per_index_min_max([s["c"] for s in celled])
        widest = celled[0]
        for s in celled:
            if _spread(s["c"]) > _spread(widest["c"]):
                widest = s
        out["dMax"] = _spread(widest["c"])
        out["dMaxAt"] = widest["at"]
        out["cellLoMv"] = min(min(s["c"]) for s in celled)
        out["cellHiMv"] = max(max(s["c"]) for s in celled)
        rest = [s for s in celled if _is_num(s.get("i")) and abs(s["i"]) < REST_CURRENT_MA]
        if rest:
            out["dRest"] = _median([_spread(s["c"]) for s in rest])
        loaded = [s for s in celled if _is_num(s.get("i")) and abs(s["i"]) >= REST_CURRENT_MA]
        if loaded:
            counts: dict[int, int] = {}
            for s in loaded:
                lowest = s["c"].index(min(s["c"]))
                counts[lowest] = counts.get(lowest, 0) + 1
            best, best_count = -1, -1
            for k, n in sorted(counts.items()):
                if n > best_count:
                    best, best_count = k, n
            out["lowCell"] = best + 1

    sohs = [s for s in day if _is_num(s.get("soh"))]
    if sohs:
        out["soh"] = sohs[-1]["soh"]
    cycs = [s for s in day if _is_num(s.get("cyc"))]
    if cycs:
        out["cyc"] = cycs[-1]["cyc"]

    has_wh = any(_is_num(s.get("i")) and _is_num(s.get("v")) for s in day)
    has_bal = any(_is_num(s.get("b")) for s in day)
    has_mos = any(_is_num(s.get("mc")) or _is_num(s.get("md")) for s in day)
    chg_ah = dchg_ah = chg_wh = dchg_wh = 0.0
    bal_ms = mos_off_ms = 0

    def counted(a, b) -> int:
        if b is None:
            return 0
        dt = (b["at"] - a["at"]) * 1000
        if dt <= 0 or dt > GAP_MS:
            return 0
        return max(0, min(b["at"] * 1000, end_ms) - max(a["at"] * 1000, start_ms))

    for k in range(len(all_s) - 1):
        a, b = all_s[k], all_s[k + 1]
        a_ms, b_ms = a["at"] * 1000, b["at"] * 1000
        if b_ms <= start_ms or a_ms >= end_ms:
            continue
        if b_ms - a_ms > GAP_MS:
            out["gaps"] += 1
            continue
        ms = counted(a, b)
        if ms == 0:
            continue
        if _is_num(a.get("i")):
            ah = abs(a["i"]) * ms / 1000 / 3_600_000
            if a["i"] < 0:
                chg_ah += ah
            else:
                dchg_ah += ah
            if _is_num(a.get("v")):
                wh = abs(a["i"]) * a["v"] * ms / 1e6 / 3_600_000
                if a["i"] < 0:
                    chg_wh += wh
                else:
                    dchg_wh += wh
        if a.get("b") == 1:
            bal_ms += ms
        if a.get("mc") == 0 or a.get("md") == 0:
            mos_off_ms += ms

    if currents:
        out["chgAh"] = _r3(chg_ah)
        out["dchgAh"] = _r3(dchg_ah)
        if _is_num(nominal_ah) and nominal_ah > 0:
            out["eqCycles"] = _r3(dchg_ah / nominal_ah)
    if has_wh:
        out["chgWh"], out["dchgWh"] = _r1(chg_wh), _r1(dchg_wh)
    if has_bal:
        out["balMin"] = _js_round(bal_ms / 60_000)
    if has_mos:
        out["mosOffMin"] = _js_round(mos_off_ms / 60_000)
        out["mosOff"] = _mos_off_runs(all_s, day, counted)
    cap = _capacity_estimate(day)
    if cap is not None:
        out["capAh"] = cap
    return out


def nominal_ah_of(nominal_mah) -> float | None:
    """`nominalAhOf`."""
    return nominal_mah / 1000 if _is_num(nominal_mah) and nominal_mah > 0 else None


def report_range_error(from_date, to_date) -> str | None:
    """`bmsReportRangeError`."""
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    if (
        not isinstance(from_date, str)
        or not isinstance(to_date, str)
        or not pattern.match(from_date)
        or not pattern.match(to_date)
    ):
        return "from and to are required, as YYYY-MM-DD"
    try:
        f = datetime.strptime(from_date, "%Y-%m-%d")
        t = datetime.strptime(to_date, "%Y-%m-%d")
    except ValueError:
        return "from and to must be real dates"
    if t < f:
        return "from must not be after to"
    if (t - f).days > REPORT_MAX_RANGE_DAYS:
        return "range_too_long"
    return None


# ── storage ───────────────────────────────────────────────────────────────────


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if db_path not in _INITIALIZED:
        _create_schema(conn)
        _INITIALIZED.add(db_path)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS bms_state (
            box_id     TEXT PRIMARY KEY,
            state_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bms_history (
            box_id      TEXT NOT NULL,
            received_at TEXT NOT NULL,
            expire_at   TEXT NOT NULL,
            entry_json  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bms_history ON bms_history (box_id, received_at);
        CREATE TABLE IF NOT EXISTS bms_samples (
            pack_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            slot        TEXT NOT NULL,
            at          INTEGER NOT NULL,
            sample_json TEXT NOT NULL,
            PRIMARY KEY (pack_id, date, slot)
        );
        CREATE INDEX IF NOT EXISTS idx_bms_samples_at ON bms_samples (pack_id, at);
        CREATE TABLE IF NOT EXISTS bms_days (
            pack_id      TEXT NOT NULL,
            date         TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            PRIMARY KEY (pack_id, date)
        );
        CREATE TABLE IF NOT EXISTS bms_pack_seen (
            pack_id    TEXT PRIMARY KEY,
            box_id     TEXT NOT NULL,
            last_ok_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS bms_roster (
            box_id      TEXT PRIMARY KEY,
            roster_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bms_imported (
            pack_id     TEXT PRIMARY KEY,
            fields_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bms_import_pending (
            pack_id     TEXT PRIMARY KEY,
            box_id      TEXT NOT NULL,
            fields_json TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _imported(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM bms_imported").fetchall()
    return {r["pack_id"]: json.loads(r["fields_json"]) for r in rows}


def _samples_between(conn, pack_id: str, from_s: int, to_s: int) -> list[dict]:
    rows = conn.execute(
        "SELECT sample_json FROM bms_samples WHERE pack_id = ? AND at >= ? AND at <= ? ORDER BY at",
        (pack_id, from_s, to_s),
    ).fetchall()
    return [json.loads(r["sample_json"]) for r in rows]


class BmsPackStore:
    """SQLite-backed battery-pack reports of the adopted edge boxes.

    Pass *hass* to use HA's bounded ``async_add_executor_job`` pool; ``None``
    falls back to ``run_in_executor`` for tests.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass

    # ── sync core ─────────────────────────────────────────────────────────────

    def _state_sync(self, box_id: str | None) -> dict:
        conn = _connect(self._db_path)
        try:
            if box_id is None:
                rows = conn.execute("SELECT * FROM bms_state ORDER BY box_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bms_state WHERE box_id = ?", (box_id,)
                ).fetchall()
            return {r["box_id"]: json.loads(r["state_json"]) for r in rows}
        finally:
            conn.close()

    def _store_report_sync(
        self, box_id: str, report: dict, received_at: datetime, tz_name: str | None
    ) -> bool:
        """`writeBmsState` without the roster reconcile: the box's state, a
        history row for a new cycle, a sample per new read, and the sealed
        days before today. Returns whether a history row was recorded."""
        received_ms = _ms(received_at)
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT state_json FROM bms_state WHERE box_id = ?", (box_id,)
            ).fetchone()
            stored = json.loads(row["state_json"]) if row is not None else None
            packs = [build_pack_doc(p) for p in report["packs"]]
            now_iso = _iso(_utcnow())
            state: dict[str, Any] = {
                "packs": packs,
                "packCount": len(packs),
                "updatedAt": now_iso,
                "readingReceivedAt": _iso(received_at),
                "receivedAtMs": received_ms,
                "rosterEpoch": report.get("rosterEpoch"),
                "rosterErrors": report.get("rosterErrors") or [],
                "lastGoodAt": (stored or {}).get("lastGoodAt"),
            }
            # The cloud updates these by dot path: one a report omits keeps
            # its stored value.
            for key in ("cycles", "source", "lastScan"):
                if report.get(key) is not None:
                    state[key] = report[key]
                elif key in (stored or {}):
                    state[key] = stored[key]
            if any(p["result"] == "ok" for p in packs):
                state["lastGoodAt"] = now_iso
            conn.execute(
                "INSERT OR REPLACE INTO bms_state (box_id, state_json) VALUES (?, ?)",
                (box_id, json.dumps(state)),
            )
            self._apply_pending_imports(conn, box_id, {p["id"] for p in packs})

            record = report.get("cycles") is not None and (stored or {}).get(
                "cycles"
            ) != report.get("cycles")
            if record:
                entry = {
                    "receivedAt": _iso(received_at),
                    "cycles": report["cycles"],
                    "packs": [{k: p[k] for k in _HISTORY_PACK_KEYS if k in p} for p in packs],
                }
                conn.execute(
                    "INSERT INTO bms_history (box_id, received_at, expire_at, entry_json) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        box_id,
                        _iso(received_at),
                        _iso(received_at + HISTORY_TTL),
                        json.dumps(entry),
                    ),
                )

            touched: set[str] = set()
            for pack in report["packs"]:
                if resolve_result(pack)[0] == "ok":
                    conn.execute(
                        "INSERT INTO bms_pack_seen (pack_id, box_id, last_ok_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(pack_id) DO UPDATE SET box_id = excluded.box_id, "
                        "last_ok_at = excluded.last_ok_at",
                        (pack["id"], box_id, received_ms),
                    )
                if not _is_new_read(stored, pack, received_ms):
                    continue
                read_at = _read_at_ms(pack, received_ms)
                sample = build_sample(pack, read_at)
                if sample is None:
                    continue
                date, slot = _slot_of(read_at)
                conn.execute(
                    "INSERT OR REPLACE INTO bms_samples (pack_id, date, slot, at, sample_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (pack["id"], date, slot, sample["at"], json.dumps(sample)),
                )
                touched.add(pack["id"])

            for pack in packs:
                if pack["id"] in touched:
                    self._seal_days(conn, pack, received_ms, tz_name)
            conn.execute("DELETE FROM bms_history WHERE expire_at < ?", (now_iso,))
            oldest = datetime.fromtimestamp(received_ms / 1000, UTC) - timedelta(
                days=SAMPLE_TTL_DAYS
            )
            conn.execute("DELETE FROM bms_samples WHERE date < ?", (oldest.strftime("%Y-%m-%d"),))
            conn.commit()
            return record
        finally:
            conn.close()

    def _apply_pending_imports(self, conn, box_id: str, reported: set[str]) -> None:
        """Apply the copies kept for packs this box now reports: into its
        roster when the roster holds the pack (a new driver bumps the epoch,
        so the report's converge sends it), otherwise as an import. Never
        creates a roster."""
        rows = conn.execute(
            "SELECT pack_id, fields_json FROM bms_import_pending WHERE box_id = ?", (box_id,)
        ).fetchall()
        due = {r["pack_id"]: json.loads(r["fields_json"]) for r in rows if r["pack_id"] in reported}
        if not due:
            return
        row = conn.execute(
            "SELECT roster_json FROM bms_roster WHERE box_id = ?", (box_id,)
        ).fetchone()
        roster = json.loads(row["roster_json"]) if row is not None else None
        held = {p["packId"] for p in roster["packs"]} if roster is not None else set()
        if roster is not None and held & set(due):
            packs = [{**p, **due.get(p["packId"], {})} for p in roster["packs"]]
            bump = _synced(packs) != _synced(roster["packs"])
            roster = {**roster, "n": roster["n"] + (1 if bump else 0), "packs": packs}
            conn.execute(
                "UPDATE bms_roster SET roster_json = ? WHERE box_id = ?",
                (json.dumps(roster), box_id),
            )
        imported = _imported(conn)
        for pack_id, fields in due.items():
            if pack_id not in held:
                conn.execute(
                    "INSERT OR REPLACE INTO bms_imported (pack_id, fields_json) VALUES (?, ?)",
                    (pack_id, json.dumps({**imported.get(pack_id, {}), **fields})),
                )
            conn.execute("DELETE FROM bms_import_pending WHERE pack_id = ?", (pack_id,))

    def _seal_days(self, conn, pack: dict, now_ms: int, tz_name: str | None) -> None:
        """Seal each local day of the last week that is over and not sealed
        yet: the island's `bmsMonths`. A sample keeps 8 days, a sealed day
        for good."""
        today = local_date(now_ms, tz_name)
        nominal = nominal_ah_of(pack.get("nominalMah"))
        for back in range(REPORT_LIVE_DAYS_BACK, 0, -1):
            date = _shift_date(today, -back)
            if conn.execute(
                "SELECT 1 FROM bms_days WHERE pack_id = ? AND date = ?", (pack["id"], date)
            ).fetchone():
                continue
            start_ms, end_ms = local_day_range_ms(date, tz_name)
            samples = _samples_between(
                conn, pack["id"], (start_ms - GAP_MS) // 1000, (end_ms + GAP_MS) // 1000
            )
            summary = build_day_summary(samples, date, tz_name, nominal)
            if summary is not None:
                conn.execute(
                    "INSERT INTO bms_days (pack_id, date, summary_json) VALUES (?, ?, ?)",
                    (pack["id"], date, json.dumps(summary)),
                )

    def _history_rows_sync(self, box_id: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT entry_json FROM bms_history WHERE box_id = ? "
                "ORDER BY received_at DESC, rowid DESC",
                (box_id,),
            ).fetchall()
            return [json.loads(r["entry_json"]) for r in rows]
        finally:
            conn.close()

    def _pack_history_sync(self, pack_id: str, from_ms: int, to_ms: int) -> dict:
        """`buildBmsHistory`."""
        cells = to_ms - from_ms <= HISTORY_CELLS_MAX_RANGE_MS
        conn = _connect(self._db_path)
        try:
            samples = _samples_between(conn, pack_id, -(-from_ms // 1000), to_ms // 1000)
        finally:
            conn.close()
        points = []
        for raw in samples:
            point = {**raw, "at": raw["at"] * 1000}
            if isinstance(raw.get("c"), list) and raw["c"]:
                point["cMin"], point["cMax"] = min(raw["c"]), max(raw["c"])
                point["spread"] = point["cMax"] - point["cMin"]
            if not cells:
                point.pop("c", None)
            points.append(point)
        return {"cells": cells, "points": points}

    def _last_ok_sync(self) -> dict[str, int | None]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute("SELECT pack_id, last_ok_at FROM bms_pack_seen").fetchall()
            return {r["pack_id"]: r["last_ok_at"] for r in rows}
        finally:
            conn.close()

    def _report_sync(self, from_date: str, to_date: str, tz_name: str | None, now_ms: int) -> dict:
        """`readBmsReport`: every pack the boxes report, one summary per local
        day of the range; the last week's unsealed days, and today, built
        live and marked `live: true`."""
        states = self._state_sync(None)
        packs: dict[str, dict] = {}
        for state in states.values():
            for pack in state.get("packs", []):
                packs.setdefault(pack["id"], pack)
        ordered = sorted(
            packs.values(),
            key=lambda p: (p.get("adoptedAt", math.inf), p["id"]),
        )
        today = local_date(now_ms, tz_name)
        conn = _connect(self._db_path)
        try:
            out = []
            for pack in ordered:
                sealed = {
                    r["date"]: json.loads(r["summary_json"])
                    for r in conn.execute(
                        "SELECT date, summary_json FROM bms_days "
                        "WHERE pack_id = ? AND date >= ? AND date <= ?",
                        (pack["id"], from_date, to_date),
                    ).fetchall()
                }
                nominal = nominal_ah_of(pack.get("nominalMah"))
                live: dict[str, dict | None] = {}
                for back in range(REPORT_LIVE_DAYS_BACK, -1, -1):
                    date = _shift_date(today, -back)
                    if date < from_date or date > to_date:
                        continue
                    if date != today and date in sealed:
                        continue
                    start_ms, end_ms = local_day_range_ms(date, tz_name)
                    samples = _samples_between(
                        conn, pack["id"], (start_ms - GAP_MS) // 1000, (end_ms + GAP_MS) // 1000
                    )
                    live[date] = build_day_summary(samples, date, tz_name, nominal)
                days = [{"date": d, "live": False, **s} for d, s in sealed.items() if d not in live]
                days += [{"date": d, "live": True, **s} for d, s in live.items() if s]
                days.sort(key=lambda d: d["date"])
                entry: dict[str, Any] = {
                    "packId": pack["id"],
                    "title": pack.get("label"),
                    "driver": pack.get("driver"),
                    "days": days,
                }
                for day in reversed(days):
                    if isinstance(day.get("cMin"), list) and day["cMin"]:
                        entry["cellCount"] = len(day["cMin"])
                        break
                if nominal is not None:
                    entry["nominalAh"] = nominal
                out.append(entry)
        finally:
            conn.close()
        return {"from": from_date, "to": to_date, "timezone": tz_name or "UTC", "packs": out}

    def _rosters_sync(self) -> dict[str, dict]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute("SELECT * FROM bms_roster ORDER BY box_id").fetchall()
            return {r["box_id"]: json.loads(r["roster_json"]) for r in rows}
        finally:
            conn.close()

    def _change_rosters_sync(self, change) -> dict[str, dict]:
        """Apply `change(rosters)` to every box's pack list in one write and
        bump the epoch of each box whose list changed. A box with no roster
        yet starts from the packs it last reported, at the next n of the
        epoch it reported, so the H2 neither drops a pack the cloud gave it
        nor refuses the set as older. Returns the changed rosters."""
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            rosters = {
                r["box_id"]: json.loads(r["roster_json"])
                for r in conn.execute("SELECT * FROM bms_roster")
            }
            states = {
                r["box_id"]: json.loads(r["state_json"])
                for r in conn.execute("SELECT * FROM bms_state")
            }
            imported = _imported(conn)
            current = {box_id: r["packs"] for box_id, r in rosters.items()}
            for box_id, state in states.items():
                current.setdefault(
                    box_id,
                    [
                        _desired_of({**p, **imported.get(p["id"], {})})
                        for p in state.get("packs", [])
                    ],
                )
            wanted = change({k: [dict(p) for p in v] for k, v in current.items()})
            changed: dict[str, dict] = {}
            for box_id, packs in wanted.items():
                if box_id in rosters and packs == rosters[box_id]["packs"]:
                    continue
                if box_id not in rosters and packs == current.get(box_id):
                    continue
                roster = rosters.get(box_id)
                if roster is None:
                    epoch = (states.get(box_id) or {}).get("rosterEpoch")
                    if isinstance(epoch, dict) and isinstance(epoch.get("generation"), str):
                        roster = {"generation": epoch["generation"], "n": int(epoch["n"])}
                    else:
                        roster = {"generation": new_roster_generation(), "n": 0}
                    roster["lastSync"] = None
                # A label is not part of `bms_sync`: changing one alone keeps
                # the epoch, so the box is not sent a set it already holds.
                previous = rosters[box_id]["packs"] if box_id in rosters else current.get(box_id)
                bump = box_id not in rosters or _synced(packs) != _synced(previous or [])
                roster = {**roster, "n": roster["n"] + (1 if bump else 0), "packs": packs}
                conn.execute(
                    "INSERT OR REPLACE INTO bms_roster (box_id, roster_json) VALUES (?, ?)",
                    (box_id, json.dumps(roster)),
                )
                changed[box_id] = roster
            conn.commit()
            return changed
        finally:
            conn.close()

    def _imported_sync(self) -> dict[str, dict]:
        conn = _connect(self._db_path)
        try:
            return _imported(conn)
        finally:
            conn.close()

    def _import_sync(self, imported: dict[str, dict]) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            held = _imported(conn)
            for pack_id, fields in imported.items():
                conn.execute(
                    "INSERT OR REPLACE INTO bms_imported (pack_id, fields_json) VALUES (?, ?)",
                    (pack_id, json.dumps({**held.get(pack_id, {}), **fields})),
                )
            conn.commit()
        finally:
            conn.close()

    def _import_pending_sync(self, box_id: str, pending: dict[str, dict], settled) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for pack_id in settled:
                conn.execute("DELETE FROM bms_import_pending WHERE pack_id = ?", (pack_id,))
            for pack_id, fields in pending.items():
                conn.execute(
                    "INSERT OR REPLACE INTO bms_import_pending (pack_id, box_id, fields_json) "
                    "VALUES (?, ?, ?)",
                    (pack_id, box_id, json.dumps(fields)),
                )
            conn.commit()
        finally:
            conn.close()

    def _update_roster_sync(self, box_id: str, fields: dict) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT roster_json FROM bms_roster WHERE box_id = ?", (box_id,)
            ).fetchone()
            if row is not None:
                roster = {**json.loads(row["roster_json"]), **fields}
                conn.execute(
                    "UPDATE bms_roster SET roster_json = ? WHERE box_id = ?",
                    (json.dumps(roster), box_id),
                )
            conn.commit()
        finally:
            conn.close()

    # ── async wrappers ────────────────────────────────────────────────────────

    async def _run(self, func, *args):
        if self._hass is not None:
            return await self._hass.async_add_executor_job(func, *args)
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def async_rosters(self) -> dict[str, dict]:
        """Every box's desired roster, by box id: `{generation, n, packs,
        lastSync}`. A box Home Assistant never changed has none."""
        return await self._run(self._rosters_sync)

    async def async_roster(self, box_id: str) -> dict | None:
        """The box's desired roster, or None while it is not managed here."""
        return (await self.async_rosters()).get(box_id)

    async def async_change_rosters(self, change) -> dict[str, dict]:
        """Change several boxes' rosters at once (a move): `change` takes and
        returns `{box_id: [pack]}`. Returns the rosters that changed."""
        return await self._run(self._change_rosters_sync, change)

    async def async_change_roster(self, box_id: str, change) -> dict | None:
        """Change one box's pack list: `change` takes and returns it. Returns
        the new roster, or None when nothing changed."""

        def one(rosters: dict) -> dict:
            return {**rosters, box_id: change(rosters.get(box_id, []))}

        return (await self.async_change_rosters(one)).get(box_id)

    async def async_imported(self) -> dict[str, dict]:
        """The labels and drivers copied in for packs of a box with no roster
        here (`label`, `driver`, `driverSource`), by pack id."""
        return await self._run(self._imported_sync)

    async def async_import(self, imported: dict[str, dict]) -> None:
        """Keep `{pack_id: fields}` copied in from the cloud. They show over
        what the box reports, and seed the box's roster on its first change."""
        await self._run(self._import_sync, imported)

    async def async_import_pending(
        self, box_id: str, pending: dict[str, dict], settled: list[str] = ()
    ) -> None:
        """Keep `{pack_id: fields}` copied in for packs `box_id` has not
        reported yet; each applies when the box first reports the pack. A
        copy kept for a pack in `settled` (imported now) is dropped."""
        await self._run(self._import_pending_sync, box_id, pending, list(settled))

    async def async_update_roster(self, box_id: str, **fields) -> None:
        """Store fields beside the box's roster (`lastSync`, a new epoch)."""
        await self._run(self._update_roster_sync, box_id, fields)

    async def async_store_report(
        self,
        box_id: str,
        report: dict,
        received_at: datetime | None = None,
        tz_name: str | None = None,
    ) -> bool:
        """Store the box's report received at `received_at` (now); days are
        sealed in `tz_name`, the household's zone. Returns whether the report
        was a new cycle, recorded in the history."""
        return await self._run(
            self._store_report_sync, box_id, report, received_at or _utcnow(), tz_name
        )

    async def async_state(self, box_id: str) -> dict | None:
        """The box's latest report as stored, or None before its first."""
        return (await self._run(self._state_sync, box_id)).get(box_id)

    async def async_states(self) -> dict[str, dict]:
        """Every box's latest report, by box id."""
        return await self._run(self._state_sync, None)

    async def async_history_rows(self, box_id: str) -> list[dict]:
        """The box's history rows, newest first."""
        return await self._run(self._history_rows_sync, box_id)

    async def async_pack_history(self, pack_id: str, from_ms: int, to_ms: int) -> dict:
        """The pack's 5-minute samples in `[from_ms, to_ms]`, oldest first:
        `{cells, points}` as the cloud's pack history."""
        return await self._run(self._pack_history_sync, pack_id, from_ms, to_ms)

    async def async_last_ok(self) -> dict[str, int | None]:
        """When each pack was last read well, in epoch ms, by pack id."""
        return await self._run(self._last_ok_sync)

    async def async_report(
        self, from_date: str, to_date: str, tz_name: str | None, now_ms: int | None = None
    ) -> dict:
        """The battery report of `from_date`..`to_date` (local dates)."""
        return await self._run(
            self._report_sync, from_date, to_date, tz_name, now_ms or _ms(_utcnow())
        )
