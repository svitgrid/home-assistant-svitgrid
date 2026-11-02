"""The household's battery temperature for the `bms_temperature` condition.

A port of the API's `bms-condition-signals.ts` (svitgrid#1327) and
`bms-temp-slots.ts`, over the reports the adopted edge boxes send to Home
Assistant (`BmsPackStore.async_states`), for the island scheduler
(svitgrid#1328).

ONE number: the mean of every accepted battery sensor across the boxes'
packs, or None when none is accepted. None is "not met", so an active rule
releases. A pack two boxes report counts once, from the first box.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# How old a pack may be and still count (`BMS_TEMP_FRESH_MS`).
BMS_TEMP_FRESH_MS = 15 * 60_000
# ANT's "no sensor" value; outside the window anyway.
BMS_TEMP_SENTINEL_C = -40
BMS_TEMP_MIN_C = -30
BMS_TEMP_MAX_C = 100
# A 0 beside a cell slot at least this far from 0 is a phantom (jk02_32s
# reports `[14,13,0,0,18]` for absent T3/T4 probes). A 0 the whole pack
# agrees with is kept: 0 degC is a real winter reading.
BMS_TEMP_PHANTOM_ZERO_GAP_C = 3


def _all_cells(n: int) -> list[str] | None:
    return ["cell"] * n if n >= 1 else None


def _ant(n: int) -> list[str] | None:
    # `ant_new.c`: the cell sensors, then the MOSFET, then the balancer.
    return ["cell"] * (n - 2) + ["mosfet", "balancer"] if n >= 2 else None


def _jk24(n: int) -> list[str] | None:
    return ["cell", "cell"] if n == 2 else None


def _jk32(n: int) -> list[str] | None:
    return _all_cells(n) if 2 <= n <= 5 else None


_SLOT_RULES = {
    "ant_new": _ant,
    "jk02_24s": _jk24,
    "jk02_32s": _jk32,
    "jbd": _all_cells,
    "daly_modbus": _all_cells,
    "daly_a5": _all_cells,
    "daly_x81": _all_cells,
}


def bms_temp_slots(driver: Any, count: int) -> list[str] | None:
    """`bmsTempSlots`: what each `tempsC` slot measures, or None when unknown."""
    if not isinstance(driver, str) or not isinstance(count, int) or count < 1:
        return None
    rule = _SLOT_RULES.get(driver)
    return rule(count) if rule is not None else None


def _finite(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v) if math.isfinite(v) else None


def _cell_slots(pack: dict) -> list | None:
    temps = pack.get("tempsC")
    if not isinstance(temps, list) or not temps:
        return None
    kinds = bms_temp_slots(pack.get("driver"), len(temps))
    if not kinds:
        return None
    cells = [t for t, kind in zip(temps, kinds, strict=False) if kind == "cell"]
    return cells or None


def accepted_pack_temperatures(pack: dict) -> list[float]:
    """The plausible cell temperatures of one pack, after the sentinel and
    phantom-zero filters (`acceptedPackTemperatures`)."""
    plausible = [
        v
        for v in (_finite(t) for t in _cell_slots(pack) or [])
        if v is not None and v != BMS_TEMP_SENTINEL_C and BMS_TEMP_MIN_C <= v <= BMS_TEMP_MAX_C
    ]
    if any(abs(v) >= BMS_TEMP_PHANTOM_ZERO_GAP_C for v in plausible):
        return [v for v in plausible if v != 0]
    return plausible


def _packs(states: Mapping[str, dict] | None):
    """Every pack with the time its box's report arrived, a pack id once."""
    seen: set[str] = set()
    for state in (states or {}).values():
        if not isinstance(state, dict):
            continue
        received = _finite(state.get("receivedAtMs"))
        packs = state.get("packs")
        for pack in packs if isinstance(packs, list) else []:
            if not isinstance(pack, dict):
                continue
            pack_id = pack.get("id")
            if isinstance(pack_id, str) and pack_id:
                if pack_id in seen:
                    continue
                seen.add(pack_id)
            yield pack, received


def _read_at_ms(pack: dict, received_ms: float | None) -> float | None:
    own = _finite(pack.get("readAtMs"))
    if own is not None:
        return own
    if received_ms is None:
        return None
    return received_ms - max(0.0, _finite(pack.get("ageMs")) or 0.0)


def bms_temperature_from_states(states: Mapping[str, dict] | None, now_ms: float) -> float | None:
    """The mean of the accepted sensors in degC, or None (`bmsTemperatureFromSources`)."""
    values: list[float] = []
    for pack, received in _packs(states):
        if _cell_slots(pack) is None:
            continue
        read_at = _read_at_ms(pack, received)
        if read_at is None or now_ms - read_at > BMS_TEMP_FRESH_MS:
            continue
        if pack.get("result") != "ok":
            continue
        values.extend(accepted_pack_temperatures(pack))
    return sum(values) / len(values) if values else None


def has_bms_temperature(states: Mapping[str, dict] | None) -> bool:
    """Whether a pack has a labelled cell sensor, fresh or not
    (`hasBmsTemperatureFromSources`): the capability for the condition."""
    return any(_cell_slots(pack) is not None for pack, _ in _packs(states))
