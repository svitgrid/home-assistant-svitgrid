"""Which write family an inverter on an edge island belongs to.

The cloud evaluator sends a Deye register write to a Deye-family inverter and
a priority profile or a threshold command to a PI30, MUST, LuxPower SNA,
SMG II or SRNE. The island evaluator writes the Deye family, the priority
profiles (svitgrid#1254) and the PI30 and MUST thresholds (#1255); an event on
any other brand writes nothing and records ``unsupported_field`` rather than
sending a partial payload.

TS mapping:
  priorityProfileProtocol      → priority_profile_protocol  (priority-profile.ts)
  profileForMode               → profile_for_mode           (priority-profile.ts)
  DEYE_REGISTER_WRITABLE_MODELS → DEYE_REGISTER_WRITABLE_MODELS (register-mapping.ts)
  validPi30Thresholds          → valid_pi30_thresholds      (custom-action-catalog.ts)
  parsePi30Qpiri (fields 9, 23) → pi30_prior_thresholds     (pi30-settings-mapping.ts)
  validMustThresholds          → valid_must_thresholds      (must-calendar.ts)
  mustMirrorStopVoltages       → must_prior_thresholds      (must-calendar.ts)
"""

from __future__ import annotations

import math
import re
from typing import Literal

BrandKind = Literal["deye", "priority", "solax", "none"]

# register-mapping.ts MODELS_3_PHASE ∪ MODELS_1_PHASE: every model written
# through the Deye register map.
DEYE_REGISTER_WRITABLE_MODELS: frozenset[str] = frozenset(
    {
        "deye_sg04lp3",
        "deye_sg01hp3",
        "deye_sg01hp3_50k",
        "deye_sg01hp3_30k",
        "deye_sg02hp3_80k",
        "deye_sg05lp3",
        "deye_gb_s20k",
        "sunsynk_3phase",
        "sunsynk_3phase_15k",
        "deye_sg03lp1",
        "deye_sg01lp1",
        "deye_sg02lp1",
        "deye_sun_g3",
        "deye_sg04lp1",
        "deye_sg05lp1",
        "deye_sg01lp1_16k",
        "sunsynk_1phase",
        "sunsynk_1phase_12k",
        "sunsynk_1phase_16k",
        "deye_sg01lp1_us",
        "solark_5k",
        "solark_12k",
        "solark_15k",
        "solark_18k",
    }
)

# must-model.ts MUST_MODEL_PREFIX.
_MUST_MODEL_PREFIX = "must_"


def priority_profile_protocol(
    model_id: str | None, transport: str | None, protocol_id: str | None
) -> str | None:
    """The priority-profile protocol the box drives this inverter with, or None.

    A port of priorityProfileProtocol, in its order, pinned by
    tests/fixtures/priority_profile_resolution.json.
    """
    if transport == "pi30_serial":
        return "pi30"
    mid = model_id or ""
    # A MUST on the board's RS-485 port or on its EyBond stick, before the
    # generic eybond_at rule, which would hand a MUST the SMG II registers.
    if mid.startswith(_MUST_MODEL_PREFIX):
        on_stick = protocol_id == "eybond_at" or transport == "eybond_at"
        return "must" if transport == "modbus_rtu" or on_stick else None
    # An SMG II wired to the board's RS-485 port: no writer drives it yet.
    if protocol_id == "smg":
        return None
    if mid.startswith("anenji_") and transport == "modbus_rtu":
        return None
    if protocol_id == "eybond_at":
        return "smg2"
    # Only the SNA, and only on the board's RS-485 port.
    if transport == "modbus_rtu" and mid.startswith("luxpower_sna_"):
        return "luxpower"
    # Ruling R14: an Anenji with no transport hint speaks SMG2.
    if mid.startswith("anenji_"):
        return "smg2"
    if mid.startswith("srne_"):
        return "srne"
    return None


def profile_for_mode(mode: str) -> str | None:
    """The priority profile a calendar mode applies, or None (profileForMode)."""
    if mode in ("battery_charge", "battery_maintenance_charge"):
        return "grid_charge"
    if mode == "use_battery":
        return "battery_first"
    return None


def brand_kind(inv: dict) -> BrandKind:
    """The write family of an adopted inverter (its adopt body's keys).

    A Solis string inverter (``solis_30k_5g``) has nothing to set and is
    ``none``. A Solis hybrid is never harvested by a box, so it never gets here.
    """
    model_id = inv.get("modelId")
    if model_id in DEYE_REGISTER_WRITABLE_MODELS:
        return "deye"
    if priority_profile_protocol(model_id, inv.get("transport"), inv.get("protocolId")):
        return "priority"
    if isinstance(model_id, str) and model_id.startswith("solax_"):
        return "solax"
    return "none"


def edge_inverter(boxes, inverter_id: str) -> dict | None:
    """The brand keys of *inverter_id* on an adopted edge box, or None.

    The keys come from the box's adopt body (``EDGE_ADOPT_INVERTER_KEYS``); one
    the body left out is None. None for an inverter no box holds: the
    integration writes it itself, as before.
    """
    for box in boxes:
        if inverter_id in box.inverter_ids:
            adopted = box.inverters.get(inverter_id) or {}
            return {k: adopted.get(k) for k in ("modelId", "transport", "protocolId")}
    return None


# ── The owner's threshold voltages (the cloud's settings mirror) ──────────

# A QPIRI reply carries 25 fields; fewer means a truncated frame.
_QPIRI_MIN_FIELDS = 25
# A QPIRI field the cloud reads as a number (pi30-settings-mapping.ts num).
_QPIRI_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
# QPIRI fields, 1-based as the protocol numbers them.
_QPIRI_BACK_TO_GRID = 9
_QPIRI_BACK_TO_BATTERY = 23

# must-calendar.ts MUST_STOP_DISCHARGE_REGISTER and MUST_STOP_CHARGE_REGISTER,
# 0.1 V per count.
MUST_STOP_DISCHARGE_REGISTER = 20118
MUST_STOP_CHARGE_REGISTER = 20119


def _finite(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def valid_pi30_thresholds(value) -> dict | None:
    """Both PI30 switch-over voltages from a stored snapshot, or None."""
    if not isinstance(value, dict):
        return None
    g, b = value.get("backToGridVoltage"), value.get("backToBatteryVoltage")
    if not _finite(g) or not _finite(b):
        return None
    return {"backToGridVoltage": g, "backToBatteryVoltage": b}


def valid_must_thresholds(value) -> dict | None:
    """Both MUST stop voltages from a stored snapshot, or None (0 is never read)."""
    if not isinstance(value, dict):
        return None
    d, c = value.get("stopDischargeV"), value.get("stopChargeV")
    if not _finite(d) or d <= 0 or not _finite(c) or c <= 0:
        return None
    return {"stopDischargeV": d, "stopChargeV": c}


def _body(snapshot, protocol: str) -> dict | None:
    """The brand body of an edge settings snapshot (edge_api), or None."""
    body = snapshot.get("body") if isinstance(snapshot, dict) else None
    if not isinstance(body, dict) or body.get("protocol") != protocol:
        return None
    return body


def _qpiri_number(fields: list[str], one_based: int) -> float | None:
    field = fields[one_based - 1]
    return float(field) if _QPIRI_NUMBER.match(field) else None


def pi30_prior_thresholds(snapshot) -> dict | None:
    """The owner's switch-over voltages from the box's QPIRI, or None.

    The cloud reads them from ``globalSettings.pi30``, which parsePi30Qpiri
    fills from the same reply: ``backToGridVoltage`` is field 9 and
    ``backToBatteryVoltage`` field 23.
    """
    body = _body(snapshot, "pi30")
    qpiri = body.get("qpiri") if body else None
    if not isinstance(qpiri, str):
        return None
    fields = qpiri.split()
    if len(fields) < _QPIRI_MIN_FIELDS:
        return None
    return valid_pi30_thresholds(
        {
            "backToGridVoltage": _qpiri_number(fields, _QPIRI_BACK_TO_GRID),
            "backToBatteryVoltage": _qpiri_number(fields, _QPIRI_BACK_TO_BATTERY),
        }
    )


def _must_word(blocks, address: int) -> int | None:
    for block in blocks:
        if not isinstance(block, dict):
            continue
        start, words = block.get("startRegister"), block.get("registers")
        if type(start) is not int or not isinstance(words, list):
            continue
        if start <= address < start + len(words):
            word = words[address - start]
            return word if type(word) is int and word > 0 else None
    return None


def must_prior_thresholds(snapshot) -> dict | None:
    """The owner's stop voltages from the box's MUST settings, or None.

    mustMirrorStopVoltages: both words of 20118 and 20119, in volts. A word
    missing or 0 was never read, and one voltage alone is never restored.
    """
    body = _body(snapshot, "must")
    blocks = body.get("blocks") if body else None
    if not isinstance(blocks, list):
        return None
    d = _must_word(blocks, MUST_STOP_DISCHARGE_REGISTER)
    c = _must_word(blocks, MUST_STOP_CHARGE_REGISTER)
    if d is None or c is None:
        return None
    return {"stopDischargeV": round(d) / 10, "stopChargeV": round(c) / 10}
