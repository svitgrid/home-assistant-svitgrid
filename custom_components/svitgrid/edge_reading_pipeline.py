"""The reading pipeline for an edge box's readings: what the cloud does to a
reading before it stores it.

The cloud never stores a raw payload. `POST /api/v1/ingest/reading`
(`services/api/src/services/reading-ingest.ts`) and `processReading`
(`process-reading.ts`) run it through `packages/reading-core` first. This
module ports, in the same order, every step of that path that changes a
stored value or refuses a reading, following the phone island's port
(`apps/mobile/lib/core/island/reading_derivation.dart` and
`reading_validator.dart`) and adding the two steps only an edge box needs:

1. `normalizeInboundReading`: alias keys onto canonical names.
2. `InverterReadingSchema.safeParse`: refuse a malformed payload, coerce
   the fields the schema coerces, and drop unknown keys.
3. Raw-stage derive hooks: the HA-Solarman battery-sign flip.
4. `applyBatterySignOverride`: the per-inverter `batterySignInverted`.
5. `applyPowerScaleOverrides`: `powerScaleOverrides` merged over
   `MODEL_POWER_SCALE_DEFAULTS`.
6. `deriveStoredReading`: export-only fill, derived-stage hooks (PI30 grid
   power), `validateReading`, the timestamp fence, the no-panel and
   no-battery clamps, the data-quality score, the daily grid-counter gate
   and bucket spike clamp, and the debounced `gridPresent`.
7. `maybeEstimateBatterySoc`: a voltage-derived SOC for an HV pack whose
   BMS reports 0.
8. `computeStoreOutcome`: the edge-confirmed precedence
   (`shouldKeepEdgeConfirmed`) and `gridPresentSince`.

The stored timestamp is the reading's, canonicalised to millisecond
precision (`new Date(ts).toISOString()`): a box sends `12:00:00Z`, the cloud
stores `12:00:00.000Z`.

Steps that only write Sentry events, Redis, Firestore side fields, or alerts
are not ported, as on the phone. `tests/test_edge_reading_pipeline.py`
replays `tests/fixtures/island_pipeline_cases.jsonl`, which the cloud's own
pipeline generates (`scripts/sync-island-pipeline-cases.sh` vendors it).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Results and context
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """`store_data` is what the store keeps, or None when the cloud would
    refuse the reading, with its reason in `rejected` (`schema` for the
    cloud's 400, else the 422 reason). `bucket_diff` holds the daily
    counters where the 5-minute bucket's view differs from `store_data`."""

    store_data: dict | None
    rejected: str | None
    warnings: list[str] = field(default_factory=list)
    bucket_diff: dict = field(default_factory=dict)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v: Any) -> bool:
    return _is_num(v) and math.isfinite(v)


@dataclass(frozen=True)
class InverterContext:
    """What the pipeline reads from the inverter's adopt details (the
    cloud's `inverters/{id}`). A field of the wrong type is treated as
    absent, as `readInverterDocFields` treats it."""

    model_id: str | None = None
    model: str | None = None
    has_solar_panels: bool | None = None
    has_battery: bool | None = None
    shared_battery: bool | None = None
    export_only: bool | None = None
    protocol_id: str | None = None
    power_scale_overrides: dict | None = None
    battery_sign_inverted: bool | None = None
    derive_hooks: tuple[str, ...] | None = None
    gen_port_mode: float | None = None
    pv_strings: float | None = None
    phases: float | None = None
    battery_capacity_wh: float | None = None
    battery_chemistry: str | None = None
    battery_cell_count: float | None = None
    battery_empty_voltage: float | None = None
    battery_full_voltage: float | None = None
    battery_internal_resistance_ohms: float | None = None
    battery_corrupt_guard_enabled: bool = False
    grid_confirmed_source_allowed: bool = True

    @classmethod
    def from_json(cls, j: dict | None) -> InverterContext:
        j = j if isinstance(j, dict) else {}

        def s(k):
            v = j.get(k)
            return v if isinstance(v, str) else None

        def b(k):
            v = j.get(k)
            return v if isinstance(v, bool) else None

        def n(k):
            v = j.get(k)
            return v if _finite(v) else None

        raw_scale = j.get("powerScaleOverrides")
        scale = None
        if isinstance(raw_scale, dict):
            scale = {k: v for k, v in raw_scale.items() if isinstance(k, str) and _is_num(v)}
        raw_hooks = j.get("deriveHooks")
        hooks = None
        if (
            isinstance(raw_hooks, list)
            and len(raw_hooks) <= 8
            and all(isinstance(h, str) for h in raw_hooks)
        ):
            hooks = tuple(raw_hooks)
        return cls(
            model_id=s("modelId"),
            model=s("model"),
            has_solar_panels=b("hasSolarPanels"),
            has_battery=b("hasBattery"),
            shared_battery=b("sharedBattery"),
            export_only=b("exportOnly"),
            protocol_id=s("protocolId"),
            power_scale_overrides=scale,
            battery_sign_inverted=b("batterySignInverted"),
            derive_hooks=hooks,
            gen_port_mode=n("genPortMode"),
            pv_strings=n("pvStrings"),
            phases=n("phases"),
            battery_capacity_wh=n("batteryCapacityWh"),
            battery_chemistry=s("batteryChemistry"),
            battery_cell_count=n("batteryCellCount"),
            battery_empty_voltage=n("batteryEmptyVoltage"),
            battery_full_voltage=n("batteryFullVoltage"),
            battery_internal_resistance_ohms=n("batteryInternalResistanceOhms"),
        )

    @property
    def _scale_model_id(self) -> str:
        if self.model:
            return self.model
        return self.model_id or ""


# ---------------------------------------------------------------------------
# JavaScript semantics
# ---------------------------------------------------------------------------


def js_round(x: float) -> int:
    """`Math.round`: half rounds toward positive infinity."""
    return math.floor(x + 0.5)


def js_num(v: Any) -> str:
    """A number printed as a JavaScript template literal prints it."""
    if v is None:
        return "undefined"
    if isinstance(v, bool):
        return "true" if v else "false"
    if not _is_num(v):
        return str(v)
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "Infinity" if v > 0 else "-Infinity"
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    return repr(float(v))


def _f(m: dict, k: str) -> float:
    """A field read as JavaScript reads `obj.field` in arithmetic."""
    v = m.get(k)
    return float(v) if _is_num(v) else math.nan


def _parse_ms(ts: Any) -> int | None:
    """`Date.parse` for the ISO strings the schema admits."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return round(dt.timestamp() * 1000)


def _iso_ms(ms: int) -> str:
    """`Date.prototype.toISOString`."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


# ---------------------------------------------------------------------------
# Model tables (reading-validator.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelLimits:
    rated_power_w: float
    nominal_ac_power_w: float
    max_pv_power_w: float
    max_grid_power_w: float
    max_load_power_w: float
    max_battery_power_w: float
    max_daily_energy_kwh: float
    max_daily_grid_energy_kwh: float
    pv_total_is_dc_input_register: bool = False


def limits_from_rated(
    rated: float,
    *,
    max_grid_power_w: float | None = None,
    max_battery_power_w: float | None = None,
    pv_total_is_dc_input_register: bool = False,
) -> ModelLimits:
    """`limitsFromRated` with its optional overrides."""
    return ModelLimits(
        rated_power_w=rated,
        nominal_ac_power_w=rated,
        max_pv_power_w=rated * 2,
        max_grid_power_w=(
            max_grid_power_w if max_grid_power_w is not None else js_round(rated * 1.5)
        ),
        max_load_power_w=js_round(rated * 1.5),
        max_battery_power_w=(
            max_battery_power_w if max_battery_power_w is not None else js_round(rated * 1.2)
        ),
        max_daily_energy_kwh=(rated / 1000) * 10,
        max_daily_grid_energy_kwh=500,
        pv_total_is_dc_input_register=pv_total_is_dc_input_register,
    )


_r = limits_from_rated


def _huawei(w):
    return limits_from_rated(w, pv_total_is_dc_input_register=True)


# `MODEL_LIMITS` in reading-validator.ts. The reasons behind each rating and
# override are documented there; this table carries the numbers only.
# `tests/test_edge_reading_pipeline.py::test_model_tables_match_reading_core`
# holds it to the vendored reading-validator.ts.
MODEL_LIMITS: dict[str, ModelLimits] = {
    "deye_sg04lp3": _r(12000),
    "deye_sg03lp1": _r(8000),
    "deye_sg01hp3": _r(25000),
    "deye_sg01hp3_50k": _r(50000),
    "deye_sg01hp3_30k": _r(30000),
    "deye_sg02hp3_80k": _r(80000),
    "deye_sg05lp3": _r(20000),
    "deye_gb_s20k": _r(20000),
    "deye_sun_g3": _r(6000),
    "deye_sg01lp1": _r(8000),
    "deye_sg02lp1": _r(12000),
    "deye_sg01lp1_us": _r(8000),
    "deye_sg04lp1": _r(6000),
    "deye_sg05lp1": _r(8000),
    "deye_sg01lp1_16k": _r(16000),
    "anenji_anj_4200": _r(4200, max_grid_power_w=7500),
    "anenji_anj_5kw": _r(5000),
    "anenji_anj_6200": _r(6200),
    "anenji_anj_11kw": _r(11000),
    "anenji_anj_12kw": _r(12000),
    "deye_string": _r(15000),
    "deye_micro": _r(2000),
    "deye_sun_60k_g03": _r(60000),
    "solplanet_asw100k_lt": _r(100000),
    "solplanet_asw110k_lt": _r(110000),
    "solis_30k_5g": _r(30000),
    "solis_s6_eh1p_l_plus": _r(8000),
    "solis_s6_eh1p_10k_l_plus": _r(10000),
    "solis_rhi_48es_5g": _r(6000),
    "solis_s6_eh3p_nv_yd_l": _r(15000),
    "solis_s6_eh3p_h": _r(20000),
    "sunsynk_1phase": _r(8000),
    "sunsynk_3phase": _r(12000),
    "sunsynk_1phase_12k": _r(12000),
    "sunsynk_1phase_16k": _r(16000),
    "sunsynk_3phase_15k": _r(15000),
    "solark_5k": _r(5000),
    "solark_12k": _r(12000),
    "solark_15k": _r(15000),
    "solark_18k": _r(18000),
    "victron_multiplus_ii_gx_6k5": _r(6500),
    "victron_multiplus_ii_3k": _r(3000),
    "victron_multiplus_ii_5k": _r(5000),
    "victron_multiplus_ii_10k": _r(10000),
    "victron_multiplus_ii_15k": _r(15000),
    "victron_multiplus_ii_4k": _r(4000),
    "victron_multiplus_ii_8k": _r(8000),
    "victron_quattro_ii_5k": _r(5000),
    "huawei_sun2000_30ktl_m3": _huawei(30000),
    "huawei_sun2000_50ktl_m3": _huawei(50000),
    "huawei_sun2000_150k_mg0": _huawei(150000),
    "huawei_sun2000": _huawei(150000),
    "sofar_hyd_3ph_10k": _r(10000),
    "sofar_hyd_3ph_20k": _r(20000),
    "srne_hsi_1500": _r(1500),
    "srne_hf_3k6": _r(3600),
    "srne_hf_6k5": _r(6500),
    "srne_asf_10k": _r(10000),
    "srne_anenji_12k": _r(12000),
    "megarevo_r_h3_10k": _r(10000),
    "megarevo_r_h3_15k": _r(16500),
    "kstar_hybrid_1p_3k": _r(3680),
    "kstar_hybrid_1p_6k": _r(6000),
    "kstar_hybrid_3p_12k": _r(12000),
    "afore_hybrid_6k": _r(6000),
    "afore_hybrid_12k": _r(12000),
    "afore_hybrid_24k": _r(24000),
    "afore_hybrid_30k": _r(30000),
    "afore_bnt_t4_6k": _r(6000),
    "afore_bnt_t4_12k": _r(12000),
    "afore_bnt_t4_24k": _r(24000),
    "afore_bnt_t4_48k": _r(48000),
    "afore_bnt_t4_96k": _r(96000),
    "afore_bnt_t4_110k": _r(110000),
    "afore_bnt_t6_6k": _r(6000),
    "afore_bnt_t6_12k": _r(12000),
    "afore_bnt_t6_24k": _r(24000),
    "afore_bnt_t6_48k": _r(48000),
    "afore_bnt_t6_96k": _r(96000),
    "afore_bnt_t6_110k": _r(110000),
    "tsun_tsol_ms400": _r(400),
    "tsun_tsol_ms800": _r(800),
    "swatten_sih_th_10k": _r(10000),
    "chint_cps_sce_6k": _r(6000),
    "invt_xd_6k": _r(6000),
    "eg4_18kpv": _r(18000),
    "eg4_12kpv": _r(12000),
    "eg4_flexboss21": _r(21000),
    "luxpower_lxp_lb_eu_7k": _r(7000),
    "solax_x3_hybrid_g4_15k": _r(15000),
    "edecoa_em_624a": _r(6200, max_grid_power_w=11500),
    "luxpower_sna_5000_wpv": _r(5000, max_grid_power_w=9200, max_battery_power_w=6700),
    "must_pv18_1012": _r(1000, max_grid_power_w=1500),
    "must_pv18_1512": _r(1500, max_grid_power_w=2250),
    "must_pv18_2012": _r(2000, max_grid_power_w=3285),
    "must_pv18_3024": _r(3000, max_grid_power_w=4928),
    "must_pv18_3224": _r(3200, max_grid_power_w=5128),
    "must_pv18_3524": _r(3500, max_grid_power_w=5428),
    "must_pv18_3624": _r(3600, max_grid_power_w=5528),
    "must_pv18_4024": _r(4000, max_grid_power_w=6570),
    "must_pv18_5048": _r(5000, max_grid_power_w=8855),
    "must_pv18_5248": _r(5200, max_grid_power_w=9055),
    "must_pv18_6048": _r(6000, max_grid_power_w=12424),
    "must_ph18_5248": _r(5200, max_grid_power_w=10340),
    "must_pv19_4024": _r(4000, max_grid_power_w=6570),
    "must_pv19_6048": _r(6200, max_grid_power_w=12624),
}

_DEFAULT_LIMITS = _r(80000)


def limits_for_model(model_id: str) -> ModelLimits:
    """`getLimitsForModel`."""
    return MODEL_LIMITS.get(model_id, _DEFAULT_LIMITS)


def _is_known_model(model_id: str | None) -> bool:
    return bool(model_id) and model_id in MODEL_LIMITS


# `HV_BATTERY_MODELS` in reading-validator.ts.
HV_BATTERY_MODELS = frozenset(
    {
        "deye_sg01hp3",
        "deye_sg01hp3_50k",
        "deye_sg01hp3_30k",
        "deye_sg02hp3_80k",
        "deye_gb_s20k",
        "megarevo_r_h3_10k",
        "megarevo_r_h3_15k",
        "afore_hybrid_6k",
        "afore_hybrid_12k",
        "afore_hybrid_24k",
        "afore_hybrid_30k",
        "swatten_sih_th_10k",
        "solax_x3_hybrid_g4_15k",
        "sofar_hyd_3ph_10k",
        "sofar_hyd_3ph_20k",
        "solis_s6_eh3p_h",
    }
)

# `LV_PACK_NOMINAL_V`: LV packs that are not 48 V-class.
LV_PACK_NOMINAL_V: dict[str, float] = {
    "anenji_anj_4200": 24,
    "srne_hsi_1500": 12,
    "must_pv18_1012": 12,
    "must_pv18_1512": 12,
    "must_pv18_2012": 12,
    "must_pv18_3024": 24,
    "must_pv18_3224": 24,
    "must_pv18_3524": 24,
    "must_pv18_3624": 24,
    "must_pv18_4024": 24,
    "must_pv19_4024": 24,
}

_PV_STRING_NOISE_FLOOR_W = 50
_GENERATOR_ENERGIZED_MIN_V = 80
_GENERATOR_ENERGIZED_MAX_V = 300
_UINT16_SPAN = 65536

# ---------------------------------------------------------------------------
# 1. normalizeInboundReading
# ---------------------------------------------------------------------------

_PV_STRING_ALIASES = {
    **{f"pv{i}Power": f"pvPower{i}" for i in range(1, 5)},
    **{f"pv{i}Voltage": f"pvVoltage{i}" for i in range(1, 7)},
    **{f"pv{i}Current": f"pvCurrent{i}" for i in range(1, 7)},
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


def _apply_aliases(m: dict, aliases: dict) -> None:
    for alias, canonical in aliases.items():
        if alias not in m or not _finite(m[alias]) or m.get(canonical) is not None:
            continue
        m[canonical] = m.pop(alias)


def normalize_inbound_reading(body: dict) -> dict:
    """`normalizeInboundReading`. Returns a copy; never mutates `body`."""
    m = dict(body)
    _apply_aliases(m, _PV_STRING_ALIASES)
    if "totalPvPower" in m:
        total = m["totalPvPower"]
        current = m.get("pvPower")
        if _finite(total) and total >= 0 and (current is None or current == 0):
            m["pvPower"] = total
            del m["totalPvPower"]
    _apply_aliases(m, _DAILY_ENERGY_ALIASES)
    for prefix, canonical in _PER_PHASE_GROUPS:
        if m.get(canonical) is not None:
            continue
        values = []
        for phase in range(1, 4):
            v = m.get(f"{prefix}{phase}")
            if not _finite(v):
                break
            values.append(v)
        if not values:
            continue
        m[canonical] = values
        for phase in range(1, len(values) + 1):
            m.pop(f"{prefix}{phase}", None)
    return m


# ---------------------------------------------------------------------------
# 2. InverterReadingSchema
# ---------------------------------------------------------------------------


class _SchemaError(Exception):
    pass


_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_PI30_MODE = re.compile(r"^[A-Z]$")
_ABSENT = object()


def _number(v, *, lo=None, hi=None, finite=False):
    if not _is_num(v) or math.isnan(v):
        raise _SchemaError
    if finite and not math.isfinite(v):
        raise _SchemaError
    if lo is not None and v < lo:
        raise _SchemaError
    if hi is not None and v > hi:
        raise _SchemaError
    return v


def _int(v, lo, hi):
    n = _number(v, lo=lo, hi=hi)
    if n != math.trunc(n):
        raise _SchemaError
    return n


def _num(lo=None, hi=None, finite=False):
    return lambda v: _number(v, lo=lo, hi=hi, finite=finite)


def _keep_if(ok):
    """A `z.preprocess` that turns an out-of-range value into `undefined`."""
    return lambda v: v if ok(v) else _ABSENT


def _num_array(max_len):
    def rule(v):
        if not isinstance(v, list) or len(v) > max_len:
            raise _SchemaError
        for x in v:
            if not _is_num(x) or math.isnan(x):
                raise _SchemaError
        return list(v)

    return rule


def _enum(values):
    def rule(v):
        if not isinstance(v, str) or v not in values:
            raise _SchemaError
        return v

    return rule


def _list(v):
    if not isinstance(v, list):
        raise _SchemaError
    return v


def _neg_to_zero(v):
    return _number(0 if _is_num(v) and v < 0 else v, lo=0)


def _fault_bits(v):
    if not isinstance(v, list) or len(v) > 8:
        raise _SchemaError
    for x in v:
        _int(x, 0, 0xFFFF)
    return v


def _is_bool(v):
    return isinstance(v, bool)


_OPTIONAL_RULES = {
    **{f"pvPower{i}": _num(lo=0) for i in range(1, 7)},
    **{f"pvVoltage{i}": _num(lo=0) for i in range(1, 7)},
    **{f"pvCurrent{i}": _num(lo=0) for i in range(1, 7)},
    "batterySoc": _num(lo=0, hi=100),
    "batteryTemperature": _num(),
    "batteryCurrent": _num(),
    "batteryPower2": _keep_if(lambda v: _finite(v) and abs(v) <= 100000),
    "batteryVoltage2": _keep_if(lambda v: _finite(v) and 0 <= v <= 1000),
    "batterySoc2": _keep_if(lambda v: _finite(v) and 0 <= v <= 100),
    "batteryCurrent2": _keep_if(lambda v: _finite(v) and abs(v) <= 500),
    "bmsChargeCurrentLimit": _num(lo=0),
    "bmsDischargeCurrentLimit": _num(lo=0),
    "inverterTemperature": _num(),
    "gridPower": _num(),
    "loadPower": _neg_to_zero,
    "gridVoltage": _num(lo=0),
    "phaseVoltages": _num_array(3),
    "phaseLoads": _num_array(3),
    "phaseGridPowers": _num_array(3),
    "phaseGridCurrents": _num_array(3),
    "phaseLoadVoltages": _num_array(3),
    "throughInverterLoad": _num(lo=0, finite=True),
    "upsLoadPower": _num(lo=0, finite=True),
    "gridPortPower": _num(finite=True),
    "temperature": _num(),
    "dailyPvEnergy": _num(lo=0),
    "dailyImportEnergy": _num(lo=0),
    "dailyExportEnergy": _num(lo=0),
    "dailyLoadEnergy": _num(),
    "dailyBatteryChargeEnergy": _num(lo=0, finite=True),
    "dailyBatteryDischargeEnergy": _num(lo=0, finite=True),
    "generatorPower": _num(),
    "generatorVoltage": _num(lo=0),
    "generatorFrequency": _num(lo=0),
    "generatorStatus": _num(lo=0),
    "dailyGeneratorEnergy": _num(lo=0),
    "dailyGeneratorRuntime": _num(lo=0),
    "genPortMode": _num(),
    "evMaxChargePower": lambda v: _ABSENT,
    "evChargeControlPower": lambda v: _ABSENT,
    "workMode": _enum({"selling", "zero_export", "self_consumption", "tou"}),
    "touSchedule": _list,
    "socketReadings": _list,
    "source": _enum(
        {"android-foreground", "android-background", "ios-foreground", "ios-background", "edge"}
    ),
    "gridFrequency": _keep_if(lambda v: _finite(v) and v >= 0),
    "runningState": _keep_if(lambda v: _finite(v) and v == math.trunc(v)),
    "gridRelayClosed": _keep_if(_is_bool),
    "pi30Mode": _keep_if(lambda v: isinstance(v, str) and bool(_PI30_MODE.match(v))),
    "pi30AcCharging": _keep_if(_is_bool),
    "pi30FloatCharging": _keep_if(_is_bool),
    "pvChargePower": _keep_if(lambda v: _finite(v) and 0 <= v <= 100000),
    "sampleCount": lambda v: _int(v, 1, 60),
    "periodSec": lambda v: _int(v, 60, 600),
    "faultBits": _fault_bits,
    "triggers": _list,
}


def _schema_gate(m: dict) -> dict:
    """`InverterReadingSchema.safeParse`: the parsed reading, or raises
    `_SchemaError`. Unknown keys are dropped, as Zod's strip mode drops them."""
    out: dict = {}
    inverter_id = m.get("inverterId")
    if not isinstance(inverter_id, str) or not inverter_id:
        raise _SchemaError
    out["inverterId"] = inverter_id
    ts = m.get("timestamp")
    if not isinstance(ts, str) or not _ISO_DATETIME.match(ts) or _parse_ms(ts) is None:
        raise _SchemaError
    out["timestamp"] = ts
    out["pvPower"] = _neg_to_zero(m.get("pvPower"))
    out["batteryPower"] = _number(m.get("batteryPower"))
    out["batteryVoltage"] = _number(m.get("batteryVoltage"), lo=0)
    for key, rule in _OPTIONAL_RULES.items():
        if key not in m:
            continue
        # A JSON null fails every plain field and is `undefined` to a preprocess.
        parsed = rule(m[key])
        if parsed is not _ABSENT and parsed is not None:
            out[key] = parsed
    return out


# ---------------------------------------------------------------------------
# 3-5. Raw hooks, sign override, power scale
# ---------------------------------------------------------------------------

_HA_SOLARMAN_PROTOCOL = "home_assistant_solarman"


def _negate_battery(d: dict) -> None:
    for f in ("batteryPower", "batteryCurrent"):
        v = d.get(f)
        if _finite(v):
            d[f] = 0 if v == 0 else -v


def _hooks_for(ctx: InverterContext, stage: str) -> list[str]:
    """`deriveHooksFor`: the hook ids that apply to `ctx` at `stage`."""
    protocol_id = ctx.protocol_id
    if protocol_id is None:
        return []
    wanted = set(ctx.derive_hooks or ())
    if protocol_id == _HA_SOLARMAN_PROTOCOL:
        wanted.add("ha-solarman-battery-sign")
    out = []
    if (
        stage == "raw"
        and "ha-solarman-battery-sign" in wanted
        and protocol_id == _HA_SOLARMAN_PROTOCOL
    ):
        out.append("ha-solarman-battery-sign")
    if stage == "derived" and "pi30-grid-power" in wanted and protocol_id == "pi30":
        out.append("pi30-grid-power")
    return out


def _apply_hooks(r: dict, ctx: InverterContext, stage: str) -> None:
    for hook in _hooks_for(ctx, stage):
        if hook == "ha-solarman-battery-sign":
            _negate_battery(r)
        if hook == "pi30-grid-power":
            _pi30_grid_power(r)


def _pi30_grid_power(r: dict) -> None:
    """`pi30GridPower` in derive-hooks/pi30-grid-power.ts."""
    if "gridPower" in r:
        return
    mode = r.get("pi30Mode")
    if not isinstance(mode, str):
        return
    if mode in ("B", "S", "F"):
        r["gridPower"] = 0
        return
    if mode != "L":
        return

    def at_least(v, lo):
        return _finite(v) and v >= lo

    load = r.get("loadPower")
    pv_charge = r.get("pvChargePower")
    v = r.get("batteryVoltage")
    i = r.get("batteryCurrent")
    ac_charging = r.get("pi30AcCharging")
    if not at_least(load, 0) or not isinstance(ac_charging, bool):
        return
    # The battery and PV-charge terms feed only the AC-charging share, so they
    # are required only while AC charging (#1100).
    ac_charge_dc = 0
    if ac_charging:
        if not at_least(pv_charge, 0) or not at_least(v, 0) or not _finite(i):
            return
        battery_charge = max(v * i, 0)
        ac_charge_dc = max(battery_charge - pv_charge, 0)
    r["gridPower"] = js_round(load + ac_charge_dc / 0.9 + 40)


# `MODEL_POWER_SCALE_DEFAULTS` in power-scale.ts.
MODEL_POWER_SCALE_DEFAULTS = {"deye_sg02lp1": {"grid": 10}}


def _resolve_power_scale(overrides: dict | None, model_id: str | None) -> dict | None:
    defaults = MODEL_POWER_SCALE_DEFAULTS.get(model_id) if model_id else None
    if defaults is None:
        return overrides
    return {**defaults, **(overrides or {})}


def apply_power_scale_overrides(d: dict, overrides: dict | None, model_id: str | None) -> None:
    """`applyPowerScaleOverrides`, in place."""
    o = _resolve_power_scale(overrides, model_id)
    if o is None:
        return

    def ok(f):
        return f is not None and _finite(f) and f > 0 and f != 1

    def scale(k, f):
        v = d.get(k)
        if _is_num(v):
            d[k] = v * f

    def scale_list(k, f):
        v = d.get(k)
        if isinstance(v, list):
            d[k] = [x * f if _is_num(x) else x for x in v]

    grid = o.get("grid")
    if ok(grid):
        scale("gridPower", grid)
        scale("gridPortPower", grid)
        scale_list("phaseGridPowers", grid)
    pv = o.get("pv")
    if ok(pv):
        for k in (*(f"pvPower{i}" for i in ("", 1, 2, 3, 4, 5, 6)), "pvChargePower"):
            scale(k, pv)
    load = o.get("load")
    if ok(load):
        scale("loadPower", load)
        scale("throughInverterLoad", load)
        scale("upsLoadPower", load)
        scale_list("phaseLoads", load)
    battery = o.get("battery")
    if ok(battery):
        scale("batteryPower", battery)
    battery_current = o.get("batteryCurrent")
    if ok(battery_current):
        scale("batteryCurrent", battery_current)
    energy = o.get("energy")
    if ok(energy):
        for k in (
            "dailyPvEnergy",
            "dailyImportEnergy",
            "dailyExportEnergy",
            "dailyLoadEnergy",
            "dailyBatteryChargeEnergy",
            "dailyBatteryDischargeEnergy",
        ):
            scale(k, energy)


# ---------------------------------------------------------------------------
# validateReading and its daily-counter helpers (reading-validator.ts,
# battery-frame-corruption.ts)
# ---------------------------------------------------------------------------


def compute_max_energy_jump_kwh(rated_power_w: float, minutes_delta: float) -> float:
    effective_minutes = max(float(minutes_delta), 5.0)
    max_jump = (rated_power_w / 1000) * (effective_minutes / 60)
    max_daily = (rated_power_w / 1000) * 10
    return min(max_jump, max_daily)


def clamp_daily_counter_spike_for_bucket(new_val, prev_val, max_jump_kwh):
    if new_val is None or prev_val is None:
        return new_val
    return prev_val if new_val > prev_val + max_jump_kwh else new_val


def gate_daily_grid_counter_by_power(new_val, prev_val, grid_power, direction, threshold_w=50):
    if new_val is None:
        return new_val
    prev = prev_val if prev_val is not None else 0
    flowing = grid_power is not None and (
        grid_power < -threshold_w if direction == "export" else grid_power > threshold_w
    )
    if flowing:
        return new_val
    return prev if new_val > prev else new_val


def _frame_chemistry(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    if s in ("lfp", "lifepo4", "lifepo", "life", "lfp4"):
        return "lfp"
    if s in ("nmc", "ncm", "nca", "li-ion", "liion", "lion"):
        return "nmc"
    return None


def _configured_pack_floor_v(chemistry, cell_count):
    if _frame_chemistry(chemistry) is None:
        return None
    if not _is_num(cell_count) or cell_count != round(cell_count) or cell_count <= 0:
        return None
    return js_round(cell_count * 2.5 * 10) / 10


def _battery_reading_contradicts_soc(voltage, soc, chemistry, cell_count) -> bool:
    if not _finite(voltage) or voltage <= 0:
        return False
    if not _finite(soc):
        return False
    if cell_count is None or not _finite(cell_count) or cell_count <= 0:
        return False
    if soc < 60:
        return False
    chem = _frame_chemistry(chemistry)
    if chem is None:
        return False
    per_cell = voltage / cell_count
    hi = 3.65 if chem == "lfp" else 4.2
    if per_cell > hi or per_cell < 2.5:
        return False
    return per_cell < (3.0 if chem == "lfp" else 3.4)


def _sink_deficit_w(r: dict) -> float:
    bp = _f(r, "batteryPower")
    gp = _f(r, "gridPower")
    load = float(r["loadPower"]) if _is_num(r.get("loadPower")) else 0.0
    sinks = load + max(0.0, bp) + max(0.0, -gp)
    sources = _f(r, "pvPower") + max(0.0, -bp) + max(0.0, gp)
    return sinks - sources


def _recover_wrapped_generator_power(data, sanitized, limits, model_id):
    raw = data.get("generatorPower")
    if not _is_num(raw) or raw >= 0:
        return None
    if raw != math.trunc(raw) or raw < -32768:
        return None
    if not _is_known_model(model_id):
        return None
    unwrapped = raw + _UINT16_SPAN
    if unwrapped > js_round(limits.rated_power_w * 1.5):
        return None
    deficit = _sink_deficit_w(sanitized)
    if abs(unwrapped - deficit) > max(1000, 0.25 * unwrapped):
        return None
    return unwrapped


def _gt(a: float, b: float) -> bool:
    """`a > b` with NaN false, as in JavaScript (Python agrees)."""
    return a > b


def _validate_reading(data: dict, ctx: InverterContext, prev: dict | None, now_ms: int):
    """A port of `validateReading`: (warnings, corrected, sanitized)."""
    warnings: list[str] = []
    corrected: list[str] = []
    sources_incomplete = False
    model_id = ctx.model_id or ""
    limits = limits_for_model(model_id)
    s = dict(data)

    def d(k):
        return _f(data, k)

    def sv(k):
        return _f(s, k)

    # CATEGORY 1: sign and polarity.
    if d("pvPower") < 0:
        warnings.append(
            f"pvPower {js_num(data.get('pvPower'))}W is negative — solar panels cannot consume power"
        )
        s["pvPower"] = 0
        corrected.append("pvPower")
    if _is_num(data.get("loadPower")) and d("loadPower") < 0:
        warnings.append(
            f"loadPower {js_num(data.get('loadPower'))}W is negative — loads cannot generate power"
        )
        s["loadPower"] = 0
        corrected.append("loadPower")
    if data.get("batterySoc") is not None:
        if d("batterySoc") < 0:
            warnings.append(f"batterySoc {js_num(data.get('batterySoc'))}% is negative")
            s["batterySoc"] = 0
            corrected.append("batterySoc")
        elif d("batterySoc") > 100:
            warnings.append(f"batterySoc {js_num(data.get('batterySoc'))}% exceeds 100%")
            s["batterySoc"] = 100
            corrected.append("batterySoc")
    if d("batteryVoltage") < 0:
        warnings.append(f"batteryVoltage {js_num(data.get('batteryVoltage'))}V is negative")
        s["batteryVoltage"] = 0
        corrected.append("batteryVoltage")

    # CATEGORY 2: register error codes.
    register_error_values = (65535, 32767, 32768, -32768)

    def check_register_error(name, value) -> bool:
        if not _is_num(value):
            return False
        if value in register_error_values or abs(value) in register_error_values:
            warnings.append(
                f"{name}={js_num(value)} appears to be a register error code (0xFFFF/0x7FFF)"
            )
            return True
        return False

    power_fields = ("pvPower", "gridPower", "loadPower", "batteryPower")
    for f in power_fields:
        if check_register_error(f, data.get(f)):
            s[f] = 0
            corrected.append(f)
    register_error_fields = [f for f in power_fields if f in corrected]
    if len(register_error_fields) >= 3:
        warnings.append(
            f"Multiple power fields ({', '.join(register_error_fields)}) show register errors — "
            "likely communication failure, entire reading is unreliable"
        )
        corrected.append("communicationFailure")

    # CATEGORY 3: model limits.
    if d("pvPower") > limits.max_pv_power_w:
        model_label = model_id or "unknown model"
        warnings.append(
            f"pvPower {js_num(data.get('pvPower'))}W exceeds max "
            f"{js_num(limits.max_pv_power_w)}W for {model_label}"
        )
        s["pvPower"] = 0
        corrected.append("pvPower")
    for i in range(1, 7):
        k = f"pvPower{i}"
        if _is_num(data.get(k)) and d(k) > limits.max_pv_power_w:
            warnings.append(
                f"{k} {js_num(data.get(k))}W exceeds max {js_num(limits.max_pv_power_w)}W"
            )
            s[k] = 0
            corrected.append(k)
    for i in range(3, 7):
        k = f"pvPower{i}"
        v = s.get(k)
        if _is_num(v) and 0 < v < _PV_STRING_NOISE_FLOOR_W:
            s[k] = 0
            corrected.append(k)

    pv_strings = ctx.pv_strings
    if pv_strings is not None and pv_strings > 0:
        string_sum_before_clamp = 0
        for i in range(1, 7):
            v = s.get(f"pvPower{i}")
            if _is_num(v) and v > 0:
                string_sum_before_clamp += v
        phantom_watts = 0
        for i in range(3, 7):
            k = f"pvPower{i}"
            v = s.get(k)
            if i > pv_strings and _is_num(v) and v > 0:
                warnings.append(
                    f"{k} {js_num(v)}W on a {js_num(pv_strings)}-string inverter — "
                    "phantom MPPT read, clamped to 0"
                )
                phantom_watts += v
                s[k] = 0
                corrected.append(k)
        pv = s.get("pvPower")
        if phantom_watts > 0 and _is_num(pv) and not limits.pv_total_is_dc_input_register:
            tolerance = max(50, string_sum_before_clamp * 0.05)
            if abs(pv - string_sum_before_clamp) <= tolerance:
                without_phantom = max(0, pv - phantom_watts)
                warnings.append(
                    f"pvPower {js_num(pv)}W was the sum of the string registers including "
                    f"{js_num(phantom_watts)}W of phantom MPPT reads — reduced to "
                    f"{js_num(without_phantom)}W"
                )
                s["pvPower"] = without_phantom
                corrected.append("pvPower")

    strings = [
        s[f"pvPower{i}"]
        for i in range(1, 7)
        if _is_num(s.get(f"pvPower{i}")) and s[f"pvPower{i}"] > 0
    ]
    if len(strings) >= 2:
        string_sum = sum(strings)
        deficit = string_sum - sv("pvPower")
        if deficit > 500 and deficit > string_sum * 0.15:
            warnings.append(
                f"pvPower {js_num(s.get('pvPower'))}W below string sum "
                f"({' + '.join(js_num(x) for x in strings)} = {js_num(string_sum)}W) — "
                "using string sum"
            )
            s["pvPower"] = string_sum
            corrected.append("pvPower")

    if abs(d("gridPower")) > limits.max_grid_power_w:
        warnings.append(
            f"gridPower {js_num(data.get('gridPower'))}W exceeds max "
            f"±{js_num(limits.max_grid_power_w)}W"
        )
        s["gridPower"] = 0
        corrected.append("gridPower")
    if _is_num(data.get("loadPower")) and d("loadPower") > limits.max_load_power_w:
        warnings.append(
            f"loadPower {js_num(data.get('loadPower'))}W exceeds max "
            f"{js_num(limits.max_load_power_w)}W — field stripped"
        )
        s.pop("loadPower", None)
        corrected.append("loadPower")
    if abs(d("batteryPower")) > limits.max_battery_power_w:
        warnings.append(
            f"batteryPower {js_num(data.get('batteryPower'))}W exceeds max "
            f"±{js_num(limits.max_battery_power_w)}W"
        )
        s["batteryPower"] = 0
        corrected.append("batteryPower")

    if _is_num(data.get("generatorPower")) and d("generatorPower") < 0:
        recovered = _recover_wrapped_generator_power(data, s, limits, model_id)
        if recovered is not None:
            warnings.append(
                f"generatorPower {js_num(data.get('generatorPower'))}W is a signed-decode wrap of "
                f"{js_num(recovered)}W — recovered; the frame's "
                f"{js_num(js_round(_sink_deficit_w(s)))}W unsourced demand corroborates it"
            )
            s["generatorPower"] = recovered
            corrected.append("generatorPower")
        else:
            warnings.append(
                f"generatorPower {js_num(data.get('generatorPower'))}W is negative — a gen port "
                "cannot consume power; magnitude untrustworthy (signed-decode wrap?), "
                "field stripped"
            )
            s.pop("generatorPower", None)
            corrected.append("generatorPower")
            sources_incomplete = True

    if (
        "generatorPower" in s
        and s["generatorPower"] != 0
        and _is_num(s.get("generatorVoltage"))
        and (
            sv("generatorVoltage") < _GENERATOR_ENERGIZED_MIN_V
            or sv("generatorVoltage") > _GENERATOR_ENERGIZED_MAX_V
        )
    ):
        warnings.append(
            f"generatorPower {js_num(s.get('generatorPower'))}W ignored — gen port not "
            f"energized (voltage {js_num(s.get('generatorVoltage'))}V outside "
            f"{_GENERATOR_ENERGIZED_MIN_V}-{_GENERATOR_ENERGIZED_MAX_V}V)"
        )
        s["generatorPower"] = 0
        s["generatorVoltage"] = 0
        if s.get("generatorFrequency") is not None:
            s["generatorFrequency"] = 0
        corrected.append("generatorPower")

    if s.get("batteryPower2") is not None:
        if check_register_error("batteryPower2", s["batteryPower2"]):
            s.pop("batteryPower2", None)
            corrected.append("batteryPower2")
        elif abs(sv("batteryPower2")) > limits.max_battery_power_w:
            warnings.append(
                f"batteryPower2 {js_num(s.get('batteryPower2'))}W exceeds max "
                f"±{js_num(limits.max_battery_power_w)}W"
            )
            s.pop("batteryPower2", None)
            corrected.append("batteryPower2")

    max_battery_voltage = 1000 if model_id in HV_BATTERY_MODELS else 70
    if d("batteryVoltage") > max_battery_voltage:
        warnings.append(
            f"batteryVoltage {js_num(data.get('batteryVoltage'))}V exceeds "
            f"{max_battery_voltage}V — likely register glitch"
        )
        s["batteryVoltage"] = 0
        corrected.append("batteryVoltage")

    if _is_num(data.get("gridVoltage")) and d("gridVoltage") > 500:
        warnings.append(f"gridVoltage {js_num(data.get('gridVoltage'))}V exceeds 500V")
        s["gridVoltage"] = 0
        corrected.append("gridVoltage")
    if _is_num(s.get("gridVoltage")) and 0 < sv("gridVoltage") < 50:
        warnings.append(
            f"gridVoltage {js_num(s.get('gridVoltage'))}V below 50V minimum — register misalignment"
        )
        s["gridVoltage"] = 0
        corrected.append("gridVoltage")

    # `sanitized.gridPower !== 0` is true for an absent gridPower.
    if isinstance(s.get("phaseVoltages"), list) and s.get("gridPower", _ABSENT) != 0:
        pvs = s["phaseVoltages"]
        if any(0 < v < 50 for v in pvs):
            warnings.append(
                f"phaseVoltages [{','.join(js_num(v) for v in pvs)}] contains sub-50V values "
                "while grid is active — register misalignment"
            )
            s["phaseVoltages"] = [0 for _ in pvs]
            s["gridVoltage"] = 0
            corrected.append("phaseVoltages")

    if isinstance(s.get("phaseVoltages"), list) and len(s["phaseVoltages"]) >= 2:
        ratio = 0.75
        phases = s["phaseVoltages"]
        ref = max(phases)
        if ref >= 180:
            healthy = sorted(v for v in phases if v >= ref * ratio)
            mid = len(healthy) // 2
            healthy_median = (
                (healthy[mid - 1] + healthy[mid]) / 2 if len(healthy) % 2 == 0 else healthy[mid]
            )
            repaired = [healthy_median if 0 < v < ref * ratio else v for v in phases]
            if repaired != phases:
                warnings.append(
                    f"phaseVoltages [{','.join(js_num(v) for v in phases)}] contains a "
                    f"sub-{js_num(js_round(ref * ratio))}V outlier while siblings read "
                    f"~{ref:.0f}V — register glitch, repaired from healthy phases"
                )
                s["phaseVoltages"] = repaired
                corrected.append("phaseVoltages")
                if _is_num(s.get("gridVoltage")) and sv("gridVoltage") < ref * ratio:
                    s["gridVoltage"] = healthy_median
                    if "gridVoltage" not in corrected:
                        corrected.append("gridVoltage")

    if isinstance(s.get("phaseGridCurrents"), list) and s["phaseGridCurrents"]:
        has_voltage = (
            isinstance(s.get("phaseVoltages"), list) and any(v > 50 for v in s["phaseVoltages"])
        ) or (_is_num(s.get("gridVoltage")) and sv("gridVoltage") > 50)
        if not has_voltage:
            warnings.append(
                f"phaseGridCurrents [{','.join(js_num(v) for v in s['phaseGridCurrents'])}] "
                "present without grid voltage — dropped"
            )
            s["phaseGridCurrents"] = []
            corrected.append("phaseGridCurrents")

    pgp = s.get("phaseGridPowers")
    if isinstance(pgp, list) and len(pgp) >= 2 and all(_is_num(v) for v in pgp):
        total = sum(pgp)
        # `sanitized.gridPower ?? 0`.
        grid_total = s["gridPower"] if _is_num(s.get("gridPower")) else 0
        tol = max(200, abs(grid_total))
        if abs(total - grid_total) > tol:
            warnings.append(
                f"phaseGridPowers [{','.join(js_num(v) for v in pgp)}] sum {js_num(total)}W "
                f"disagrees with total grid {js_num(grid_total)}W — corrupt read, dropped"
            )
            s["phaseGridPowers"] = []
            corrected.append("phaseGridPowers")
            if isinstance(s.get("phaseGridCurrents"), list) and s["phaseGridCurrents"]:
                s["phaseGridCurrents"] = []
                corrected.append("phaseGridCurrents")

    pgc = s.get("phaseGridCurrents")
    if isinstance(pgc, list) and pgc and all(_is_num(v) for v in pgc):
        pv_list = s.get("phaseVoltages")
        phase_count = len(pv_list) if isinstance(pv_list, list) and pv_list else 1
        nominal_w = limits.nominal_ac_power_w
        if nominal_w > 0:
            cap_amps = (nominal_w / (phase_count * 200)) * 1.5
            if any(abs(a) > cap_amps for a in pgc):
                warnings.append(
                    f"phaseGridCurrents [{','.join(js_num(v) for v in pgc)}] exceeds plausible "
                    f"{cap_amps:.0f}A/phase for {model_id} — corrupt, dropped"
                )
                s["phaseGridCurrents"] = []
                corrected.append("phaseGridCurrents")

    # Daily energy counters.
    if _is_num(data.get("dailyPvEnergy")) and d("dailyPvEnergy") > limits.max_daily_energy_kwh:
        warnings.append(
            f"dailyPvEnergy {js_num(data.get('dailyPvEnergy'))}kWh exceeds max "
            f"{js_num(limits.max_daily_energy_kwh)}kWh"
        )
        s.pop("dailyPvEnergy", None)
        corrected.append("dailyPvEnergy")
    for k in ("dailyImportEnergy", "dailyExportEnergy", "dailyLoadEnergy"):
        if _is_num(data.get(k)) and d(k) > limits.max_daily_grid_energy_kwh:
            warnings.append(
                f"{k} {js_num(data.get(k))}kWh exceeds max "
                f"{js_num(limits.max_daily_grid_energy_kwh)}kWh"
            )
            s[k] = 0
            corrected.append(k)

    if (
        _is_num(data.get("dailyLoadEnergy"))
        and _is_num(s.get("loadPower"))
        and sv("loadPower") >= 100
        and abs(d("dailyLoadEnergy") - sv("loadPower")) / sv("loadPower") < 0.02
    ):
        warnings.append(
            f"dailyLoadEnergy {js_num(data.get('dailyLoadEnergy'))}kWh mirrors loadPower "
            f"{js_num(s.get('loadPower'))}W — instantaneous watts stored in the daily energy "
            "field, field stripped"
        )
        s.pop("dailyLoadEnergy", None)
        if "dailyLoadEnergy" not in corrected:
            corrected.append("dailyLoadEnergy")

    if (
        _is_num(s.get("dailyPvEnergy"))
        and _is_num(s.get("dailyImportEnergy"))
        and sv("dailyPvEnergy") > 0.5
        and abs(sv("dailyPvEnergy") - sv("dailyImportEnergy")) < 0.101
        and (sv("pvPower") if _is_num(s.get("pvPower")) else 0) < 50
    ):
        warnings.append(
            f"dailyPvEnergy {js_num(s.get('dailyPvEnergy'))} mirrors dailyImportEnergy "
            f"{js_num(s.get('dailyImportEnergy'))} with pvPower≈0 — Solarman cross-batch read "
            "corruption, field stripped"
        )
        s.pop("dailyPvEnergy", None)
        corrected.append("dailyPvEnergy")

    effective_gen_port_mode = (
        s.get("genPortMode") if s.get("genPortMode") is not None else ctx.gen_port_mode
    )
    if (
        effective_gen_port_mode != 2
        and _is_num(s.get("dailyExportEnergy"))
        and _is_num(s.get("dailyPvEnergy"))
        and sv("dailyExportEnergy") > sv("dailyPvEnergy") + 20 + 1
    ):
        warnings.append(
            f"dailyExportEnergy {js_num(s.get('dailyExportEnergy'))}kWh exceeds dailyPvEnergy "
            f"{js_num(s.get('dailyPvEnergy'))}kWh + battery — register misalignment"
        )
        s["dailyExportEnergy"] = 0
        corrected.append("dailyExportEnergy")

    # Timestamp: warn only.
    reading_time = _parse_ms(data.get("timestamp"))
    if reading_time is not None:
        if reading_time > now_ms + 5 * 60 * 1000:
            warnings.append(f"timestamp {data.get('timestamp')} is in the future")
        if reading_time < now_ms - 24 * 60 * 60 * 1000:
            warnings.append(f"timestamp {data.get('timestamp')} is more than 24h old")

    # Energy balance.
    battery_discharge = max(0.0, -sv("batteryPower"))
    grid_export = max(0.0, -sv("gridPower"))
    grid_import = max(0.0, sv("gridPower"))
    # `sanitized.generatorPower || 0`.
    generator_power = sv("generatorPower") if _is_num(s.get("generatorPower")) else 0.0
    total_sources = sv("pvPower") + battery_discharge + grid_import + generator_power
    impossible_export = (
        sv("pvPower") < 50 and grid_export > 500 and grid_export > total_sources + 500
    )
    load_for_balance = sv("loadPower") if _is_num(s.get("loadPower")) else 0.0
    load_deficit_threshold = max(300.0, 0.6 * load_for_balance)
    impossible_load = (
        grid_export < 100
        and load_for_balance > 200
        and (load_for_balance - total_sources) > load_deficit_threshold
    )

    def zero_powers():
        s["pvPower"] = 0
        s["gridPower"] = 0
        s["batteryPower"] = 0
        s["loadPower"] = 0
        corrected.append("energyBalance")

    if (impossible_export or impossible_load) and not sources_incomplete:
        if impossible_export:
            battery_charge = max(0.0, sv("batteryPower"))
            inferred_pv = (
                grid_export
                + load_for_balance
                + battery_charge
                - battery_discharge
                - grid_import
                - generator_power
            )
            warnings.append(
                f"energyBalance: pvPower={js_num(s.get('pvPower'))}W but exporting "
                f"{js_num(grid_export)}W with only {js_num(battery_discharge)}W battery "
                f"discharge + {js_num(generator_power)}W generator — impossible physics "
                f"(inferred pvPower ≈ {js_num(js_round(max(0, inferred_pv)))}W)"
            )
        else:
            deficit = load_for_balance - total_sources
            warnings.append(
                f"energyBalance: loadPower={js_num(load_for_balance)}W but only "
                f"{js_num(js_round(total_sources))}W available "
                f"(PV:{js_num(s.get('pvPower'))} + battDischarge:{js_num(battery_discharge)} + "
                f"gridImport:{js_num(grid_import)} + gen:{js_num(generator_power)}) — "
                f"impossible {js_num(js_round(deficit))}W deficit"
            )
        zero_powers()

    battery_charge = max(0.0, sv("batteryPower"))
    total_sources_for_charging = sv("pvPower") + grid_import + generator_power
    charge_deficit_threshold = max(300.0, 0.6 * battery_charge)
    impossible_charge = (
        battery_charge > 200
        and (battery_charge - total_sources_for_charging) > charge_deficit_threshold
    )
    if impossible_charge and not sources_incomplete:
        warnings.append(
            f"energyBalance: batteryCharge={js_num(battery_charge)}W but only "
            f"{js_num(js_round(total_sources_for_charging))}W available "
            f"(PV:{js_num(s.get('pvPower'))} + gridImport:{js_num(grid_import)} + "
            f"gen:{js_num(generator_power)}) — impossible "
            f"{js_num(js_round(battery_charge - total_sources_for_charging))}W excess"
        )
        zero_powers()

    if sources_incomplete and (impossible_export or impossible_load or impossible_charge):
        warnings.append(
            "energyBalance not evaluated — a source field was stripped as non-physical, "
            "so the source total is incomplete; the reading is stored without it"
        )

    # CATEGORY 4b: communication failure.
    has_battery = ctx.has_battery is True
    soc = s.get("batterySoc")
    soc_is_num = _is_num(soc)
    if (
        has_battery
        and s.get("pvPower") == 0
        and s.get("gridPower") == 0
        and s.get("batteryPower") == 0
        and s.get("loadPower") == 0
        and soc_is_num
        and soc <= 1
    ):
        warnings.append(
            f"all-zero reading on battery inverter (SOC={js_num(soc)}%) — likely communication "
            "failure (BMS prevents 0% SOC, houses have standby load)"
        )
        corrected.append("communicationFailure")

    if (
        has_battery
        and s.get("batteryVoltage") == 0
        and soc_is_num
        and soc <= 1
        and s.get("loadPower") == 0
        and "communicationFailure" not in corrected
    ):
        warnings.append(
            f"battery register block read as zero (batteryVoltage=0V, SOC={js_num(soc)}%) "
            "with zero load — likely partial/failed Modbus read (a connected battery always "
            "has non-zero voltage)"
        )
        corrected.append("communicationFailure")

    nominal_v = LV_PACK_NOMINAL_V.get(model_id, 48)
    configured_floor_v = _configured_pack_floor_v(ctx.battery_chemistry, ctx.battery_cell_count)
    lv_alive_min_v = (
        configured_floor_v
        if configured_floor_v is not None
        else js_round(nominal_v * (40 / 48) * 10) / 10
    )
    pack_label = (
        f"configured {js_num(ctx.battery_cell_count)}S pack"
        if configured_floor_v is not None
        else f"{js_num(nominal_v)}V-class LV pack"
    )
    if (
        has_battery
        and model_id not in HV_BATTERY_MODELS
        and d("batteryVoltage") > 0
        and d("batteryVoltage") < lv_alive_min_v
        and "communicationFailure" not in corrected
    ):
        warnings.append(
            f"battery voltage {js_num(data.get('batteryVoltage'))}V is implausibly low for a live "
            f"{pack_label} (min ~{js_num(lv_alive_min_v)}V before BMS cutoff) — "
            "corrupt frame, register block contaminated by another Modbus batch"
        )
        corrected.append("communicationFailure")

    if (
        ctx.battery_corrupt_guard_enabled
        and has_battery
        and _is_num(s.get("batteryVoltage"))
        and _battery_reading_contradicts_soc(
            data.get("batteryVoltage"),
            data.get("batterySoc"),
            ctx.battery_chemistry,
            ctx.battery_cell_count,
        )
    ):
        warnings.append(
            f"battery voltage {js_num(data.get('batteryVoltage'))}V is impossible for SOC "
            f"{js_num(data.get('batterySoc'))}% (near empty at high charge) — corrupt frame, "
            "voltage dropped"
        )
        s["batteryVoltage"] = None
        corrected.append("batteryVoltage")

    prev = prev or {}
    prev_soc = prev.get("batterySoc") if _is_num(prev.get("batterySoc")) else None
    prev_volt = prev.get("batteryVoltage") if _is_num(prev.get("batteryVoltage")) else None
    d_soc = data.get("batterySoc")
    if (
        has_battery
        and prev_soc is not None
        and prev_soc > 0
        and prev_volt is not None
        and prev_volt > 0
        and _is_num(d_soc)
        and 0 < d_soc < prev_soc
        and 0 < d("batteryVoltage") < prev_volt
        and "communicationFailure" not in corrected
    ):
        soc_fraction = d_soc / prev_soc
        voltage_fraction = d("batteryVoltage") / prev_volt
        if 1 - voltage_fraction >= 0.10 and abs(voltage_fraction - soc_fraction) <= 0.10:
            warnings.append(
                f"corrupt frame: battery SOC {js_num(prev_soc)}→{js_num(d_soc)}% and voltage "
                f"{js_num(prev_volt)}→{js_num(data.get('batteryVoltage'))}V fell by the same "
                f"proportion (SOC ×{soc_fraction:.2f}, V ×{voltage_fraction:.2f}) — whole "
                "register block scaled by a garbage factor, not a real discharge"
            )
            corrected.append("communicationFailure")

    # CATEGORY 5: SOC against power direction (warnings only).
    if soc_is_num and soc >= 100 and sv("batteryPower") > 500:
        warnings.append(
            f"batterySoc=100% but batteryPower={js_num(s.get('batteryPower'))}W (charging) — "
            "BMS should limit charge at full SOC"
        )
    if soc_is_num and soc <= 0 and sv("batteryPower") < -500:
        warnings.append(
            f"batterySoc=0% but batteryPower={js_num(s.get('batteryPower'))}W (discharging) — "
            "BMS should prevent discharge at empty SOC"
        )

    # CATEGORY 6: daily counters.
    gpm = s.get("genPortMode")
    if _is_num(gpm) and (gpm < 0 or gpm > 2 or gpm != math.trunc(gpm)):
        warnings.append(f"genPortMode {js_num(gpm)} is out of range [0..2]")
        s.pop("genPortMode", None)
        corrected.append("genPortMode")
    for k in ("dailyPvEnergy", "dailyImportEnergy", "dailyExportEnergy", "dailyLoadEnergy"):
        if _is_num(data.get(k)) and d(k) < 0:
            warnings.append(f"{k} {js_num(data.get(k))}kWh is negative")
            s[k] = 0
            corrected.append(k)

    return warnings, corrected, s


# ---------------------------------------------------------------------------
# 6. deriveStoredReading pieces
# ---------------------------------------------------------------------------

_NO_PANEL_PV_FIELDS = (
    *(f"pvPower{i}" for i in ("", 1, 2, 3, 4, 5, 6)),
    *(f"pvVoltage{i}" for i in range(1, 7)),
    *(f"pvCurrent{i}" for i in range(1, 7)),
    "dailyPvEnergy",
    "peakPvPower",
    "pvChargePower",
)

_NO_BATTERY_FIELDS = (
    "batterySoc",
    "batteryVoltage",
    "batteryCurrent",
    "batteryPower",
    "batteryTemperature",
    "dailyBatteryChargeEnergy",
    "dailyBatteryDischargeEnergy",
)

_BUCKET_MAX_FIELDS = (
    "dailyPvEnergy",
    "dailyImportEnergy",
    "dailyExportEnergy",
    "dailyLoadEnergy",
    "dailyGeneratorEnergy",
)

_MIN_PLAUSIBLE_READING_MS = 1577836800000  # Date.UTC(2020, 0, 1)
_MAX_READING_FUTURE_MS = 24 * 60 * 60 * 1000

# `EDGE_CONFIRM_STALENESS_MS` in edge-precedence.ts.
EDGE_CONFIRM_STALENESS_MS = 5 * 60 * 1000


def derive_grid_present(r: dict) -> bool | None:
    """`deriveGridPresent` in derive-grid-present.ts."""
    if r.get("gridRelayClosed") is True:
        return True
    phases = (
        [v for v in r["phaseVoltages"] if _finite(v)]
        if isinstance(r.get("phaseVoltages"), list)
        else []
    )
    if not phases and _finite(r.get("gridVoltage")):
        phases = [r["gridVoltage"]]
    has_voltage = bool(phases)
    freq = r["gridFrequency"] if _finite(r.get("gridFrequency")) else None
    power = r["gridPower"] if _finite(r.get("gridPower")) else None

    if r.get("gridRelayClosed") is False:
        volts_healthy = has_voltage and all(v >= 180 for v in phases)
        power_flowing = power is not None and abs(power) >= 200
        if not (volts_healthy and power_flowing):
            return False
    if not has_voltage and freq is None:
        return None
    volts_down = has_voltage and all(v < 30 for v in phases)
    freq_down = freq is not None and freq < 30
    power_near_zero = power is not None and abs(power) < 50
    if freq_down and volts_down:
        return False
    if volts_down and power_near_zero:
        return False
    return not (freq_down and power_near_zero)


def _debounce_grid_present(raw, prev_raw, prev_stored):
    """`debounceGridPresent`: a first "down" holds the prior state."""
    if raw is not False:
        return raw
    if prev_raw is False:
        return False
    return prev_stored


def _quality_penalty(corrected: list[str], is_soc_mismatch: bool) -> int:
    if not corrected:
        return 0
    if len(corrected) >= 2:
        return -30
    if is_soc_mismatch:
        return -20
    return -10


def _rolling_score(existing, penalty: int) -> int:
    base = existing if existing is not None else 100
    reading_score = min(max(100 + penalty, 0), 100)
    return min(max(js_round(base * 0.9 + reading_score * 0.1), 0), 100)


def _should_keep_edge_confirmed(aggregate, stored: dict, now_ms: int) -> bool:
    """`shouldKeepEdgeConfirmed` in edge-precedence.ts."""
    if aggregate is not None:
        return False
    if stored.get("gridConfirmedSource") != "edge":
        return False
    at = _parse_ms(stored.get("gridConfirmedAt"))
    if at is None:
        return False
    delta = now_ms - at
    return 0 <= delta <= EDGE_CONFIRM_STALENESS_MS


# ---------------------------------------------------------------------------
# 7. Battery SOC estimate (battery-soc-estimate.ts)
# ---------------------------------------------------------------------------

_HIGH_VOLTAGE_SOC_MODELS = frozenset(
    {"deye_sg01hp3", "deye_sg01hp3_50k", "deye_sg01hp3_30k", "deye_gb_s20k", "deye_sg02hp3_80k"}
)

_SOC_CURVES = {
    "nmc": (
        (0, 0),
        (0.25, 0.05),
        (0.375, 0.10),
        (0.458, 0.20),
        (0.517, 0.30),
        (0.567, 0.40),
        (0.608, 0.50),
        (0.667, 0.60),
        (0.725, 0.70),
        (0.792, 0.80),
        (0.875, 0.90),
        (1.0, 1.0),
    ),
    "lfp": (
        (0, 0),
        (0.435, 0.05),
        (0.565, 0.10),
        (0.609, 0.20),
        (0.652, 0.40),
        (0.678, 0.60),
        (0.696, 0.80),
        (0.722, 0.90),
        (0.783, 0.95),
        (1.0, 1.0),
    ),
}

# Per-cell (empty, full, nominal) volts.
_SOC_CELL = {"nmc": (3.0, 4.2, 3.65), "lfp": (2.5, 3.65, 3.30)}

_CHEMISTRY_CURVE = {
    "lifepo4": "lfp",
    "lifepo": "lfp",
    "life": "lfp",
    "lfp": "lfp",
    "lfp4": "lfp",
    "nmc": "nmc",
    "ncm": "nmc",
    "nca": "nmc",
    "li-ion": "nmc",
    "liion": "nmc",
    "lion": "nmc",
    "lto": None,
    "lead_acid": None,
    "lead-acid": None,
    "leadacid": None,
    "agm": None,
    "gel": None,
}


def _interpolate(curve, x: float) -> float:
    if x <= curve[0][0]:
        return curve[0][1]
    if x >= curve[-1][0]:
        return curve[-1][1]
    for i in range(1, len(curve)):
        x1, y1 = curve[i]
        if x <= x1:
            x0, y0 = curve[i - 1]
            t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return curve[-1][1]


def _maybe_estimate_battery_soc(battery_soc, battery_voltage, battery_current, ctx, prev_estimated):
    """`maybeEstimateBatterySoc`: the voltage-derived SOC for an HV pack whose
    BMS reports 0, or None when it does not apply."""
    if ctx.model_id not in _HIGH_VOLTAGE_SOC_MODELS:
        return None
    if battery_soc is not None and battery_soc != 0:
        return None
    v = battery_voltage
    if not _finite(v):
        return None

    raw = ctx.battery_chemistry
    if raw is None:
        chem = "nmc"
    else:
        key = raw.strip().lower()
        chem = _CHEMISTRY_CURVE.get(key, "nmc")
    if chem is None:
        return None
    cell_empty, cell_full, cell_nominal = _SOC_CELL[chem]
    r = ctx.battery_internal_resistance_ohms
    resistance = r if r is not None and r >= 0 else 0.1
    v_e = ctx.battery_empty_voltage
    v_f = ctx.battery_full_voltage
    if v_e is not None and v_f is not None and v_e > 0 and v_f > v_e:
        v_empty, v_full = v_e, v_f
    else:
        stored = ctx.battery_cell_count or 0

        def plausible(count):
            if not math.isfinite(count) or count <= 0:
                return False
            return cell_empty <= v / count <= cell_full

        if plausible(stored):
            count = stored
        else:
            lo = math.ceil(v / cell_full)
            hi = math.floor(v / cell_empty)
            c = js_round(v / cell_nominal)
            if hi >= lo:
                c = min(max(c, lo), hi)
            count = 1 if c < 1 else c
        v_empty = count * cell_empty
        v_full = count * cell_full
    if v < v_empty * 0.9 or v > v_full * 1.1:
        return None

    i = battery_current if _is_num(battery_current) else 0
    ocv = v - i * resistance
    f = min(max((ocv - v_empty) / (v_full - v_empty), 0.0), 1.0)
    soc01 = min(max(_interpolate(_SOC_CURVES[chem], f), 0.0), 1.0)
    estimate = js_round(soc01 * 100)
    if _finite(prev_estimated):
        estimate = js_round(0.3 * estimate + 0.7 * prev_estimated)
    return estimate


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _num_or_none(v):
    return v if _is_num(v) else None


def _prev_quality_score(prev: dict):
    """The previous rolling score: the cloud's `_dataQuality.score`, or the
    `_dataQualityScore` this pipeline stored."""
    quality = prev.get("_dataQuality")
    if isinstance(quality, dict):
        return _num_or_none(quality.get("score"))
    return _num_or_none(prev.get("_dataQualityScore"))


def process(
    reading: dict,
    ctx,
    prev: dict | None,
    *,
    now: datetime,
    default_source: str = "edge-device",
) -> PipelineResult:
    """The stored form of `reading` from the inverter `ctx` describes (its
    adopt details, or an `InverterContext`), given the previous stored
    reading `prev` (None for the first one). `now` stands in for the
    server clock. `default_source` is the `source` stored for a reading
    that names none: the cloud stores its sender's principal."""
    if not isinstance(ctx, InverterContext):
        ctx = InverterContext.from_json(ctx)
    now_ms = math.floor(now.timestamp() * 1000)

    # reading-ingest.ts: normalise, then the schema.
    try:
        data = _schema_gate(normalize_inbound_reading(reading if isinstance(reading, dict) else {}))
    except _SchemaError:
        return PipelineResult(store_data=None, rejected="schema")

    # process-reading.ts, inline before deriveStoredReading.
    _apply_hooks(data, ctx, "raw")
    if ctx.battery_sign_inverted is True:
        _negate_battery(data)
    apply_power_scale_overrides(data, ctx.power_scale_overrides, ctx._scale_model_id)

    # deriveStoredReading.
    normalized = normalize_inbound_reading(data)
    if (
        ctx.export_only is True
        and not _is_num(normalized.get("gridPower"))
        and _is_num(normalized.get("pvPower"))
        and normalized["pvPower"] > 50
    ):
        normalized["gridPower"] = -normalized["pvPower"]
        normalized["loadPower"] = 0
        if _is_num(normalized.get("dailyPvEnergy")):
            normalized["dailyExportEnergy"] = normalized["dailyPvEnergy"]
        normalized["dailyImportEnergy"] = 0
    _apply_hooks(normalized, ctx, "derived")

    warnings, corrected, sanitized = _validate_reading(normalized, ctx, prev, now_ms)

    ts = str(normalized.get("timestamp"))
    reading_ms = _parse_ms(ts)
    too_far_ahead = reading_ms is not None and reading_ms > now_ms + _MAX_READING_FUTURE_MS
    if reading_ms is None or reading_ms < _MIN_PLAUSIBLE_READING_MS or too_far_ahead:
        reason = (
            f"timestamp {ts} is more than a day ahead of the server clock (device clock jumped?)"
            if too_far_ahead
            else f"timestamp {ts} is not a usable reading time (device clock unset?)"
        )
        warnings.append(reason)
        return PipelineResult(store_data=None, rejected=reason, warnings=warnings)
    if "energyBalance" in corrected or "communicationFailure" in corrected:
        reason = warnings[0] if warnings else "Physically impossible reading"
        return PipelineResult(store_data=None, rejected=reason, warnings=warnings)

    store = normalized if not warnings else sanitized
    if ctx.has_solar_panels is False:
        for f in _NO_PANEL_PV_FIELDS:
            if _is_num(store.get(f)) and store[f] != 0:
                store[f] = 0
    if ctx.has_battery is False and ctx.shared_battery is not True:
        for f in _NO_BATTERY_FIELDS:
            store.pop(f, None)

    prev_doc = prev or {}
    penalty = _quality_penalty(corrected, "batterySoc" in corrected)
    score = _rolling_score(_prev_quality_score(prev_doc), penalty)

    # Daily grid-counter gate (mutates the live value) and the bucket spike
    # clamp (bucket only).
    prev_ts = _parse_ms(prev_doc.get("timestamp")) or 0
    minutes_delta = (reading_ms - prev_ts) / 60000 if prev_ts > 0 else 60
    max_jump_kwh = compute_max_energy_jump_kwh(
        limits_for_model(ctx.model_id or "").rated_power_w, minutes_delta
    )
    grid = _num_or_none(store.get("gridPower"))
    for key, direction in (("dailyExportEnergy", "export"), ("dailyImportEnergy", "import")):
        gated = gate_daily_grid_counter_by_power(
            _num_or_none(store.get(key)), _num_or_none(prev_doc.get(key)), grid, direction
        )
        if gated != store.get(key):
            store[key] = gated
    bucket_counters = {}
    for f in _BUCKET_MAX_FIELDS:
        cur = store.get(f)
        if not _is_num(cur):
            continue
        bucket_counters[f] = clamp_daily_counter_spike_for_bucket(
            cur, _num_or_none(prev_doc.get(f)), max_jump_kwh
        )

    raw_grid_present = derive_grid_present(store)
    prev_present = prev_doc.get("gridPresent") if prev is not None else None
    prev_raw = (
        (
            prev_doc.get("gridPresentRaw")
            if prev_doc.get("gridPresentRaw") is not None
            else prev_doc.get("gridPresent")
        )
        if prev is not None
        else None
    )
    grid_present = _debounce_grid_present(raw_grid_present, prev_raw, prev_present)

    # process-reading.ts, after deriveStoredReading.
    estimate = _maybe_estimate_battery_soc(
        store.get("batterySoc"),
        store.get("batteryVoltage"),
        store.get("batteryCurrent"),
        ctx,
        _num_or_none(prev_doc.get("batterySocEstimated")),
    )
    if estimate is not None:
        store["batterySocEstimated"] = estimate
        store["batterySocSource"] = "voltage"

    # computeStoreOutcome, with the edge-confirmed precedence.
    reading_iso = _iso_ms(reading_ms)
    keep_edge = ctx.grid_confirmed_source_allowed and _should_keep_edge_confirmed(
        grid_present, prev_doc, now_ms
    )
    effective = prev_doc.get("gridPresent") if keep_edge else grid_present
    transitioned = prev is None or effective != prev_present
    grid_present_since = (
        reading_iso if transitioned else (prev_doc.get("gridPresentSince") or reading_iso)
    )

    source = store.get("source")
    stored = {
        **store,
        "timestamp": reading_iso,
        "source": source if isinstance(source, str) and source else default_source,
        "_dataQualityScore": score,
        "gridPresent": effective,
        "gridPresentSince": grid_present_since,
        "gridPresentRaw": raw_grid_present,
    }
    if keep_edge:
        stored["gridConfirmedSource"] = prev_doc.get("gridConfirmedSource")
        stored["gridConfirmedAt"] = prev_doc.get("gridConfirmedAt")
    stored["sampleCount"] = data.get("sampleCount", 1)
    stored["periodSec"] = data.get("periodSec", 60)

    bucket_diff = {k: v for k, v in bucket_counters.items() if v != store.get(k)}
    return PipelineResult(
        store_data=stored, rejected=None, warnings=warnings, bucket_diff=bucket_diff
    )
