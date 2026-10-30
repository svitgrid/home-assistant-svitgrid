"""Which write family an inverter on an edge island belongs to.

The cloud evaluator sends a Deye register write to a Deye-family inverter and
a priority profile or a threshold command to a PI30, MUST, LuxPower SNA,
SMG II or SRNE. The island evaluator writes the Deye family only: until the
priority-profile and threshold commands are ported (svitgrid#1254, #1255), an
event on any other brand writes nothing and records ``unsupported_field``
rather than sending a partial payload.

TS mapping:
  priorityProfileProtocol      → priority_profile_protocol  (priority-profile.ts)
  profileForMode               → profile_for_mode           (priority-profile.ts)
  DEYE_REGISTER_WRITABLE_MODELS → DEYE_REGISTER_WRITABLE_MODELS (register-mapping.ts)
"""

from __future__ import annotations

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
