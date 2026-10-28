"""Which settings an inverter has, and which registers the add-on reads for them.

The island settings view serves a *settings snapshot*: the body the
inverter's harvester would POST to the cloud's ``sync-settings`` route, plus
``inverterId``, ``modelId`` and ``readAt``. The body comes in one of six
shapes, the *snapshot kind*, and the app maps every one of them itself. The
add-on only reads and stores; it never decodes a setting.

``settings_snapshot_kind_for`` mirrors the app's ``settingsSnapshotKindFor``
(``packages/inverter_protocol/lib/src/registers/settings_register_mapping.dart``
in the monorepo) rule for rule, and the two must agree: a model with no kind
answers ``nothing_to_configure``, and one with a kind but no read yet answers
``not_read_yet``. Getting it wrong either way leaves a settings screen
waiting for a snapshot that never comes, or hides one that would.

``settings_read_plan_for`` says how this add-on reads a kind, or None when it
cannot. PI30 has a kind but no plan: the add-on cannot harvest a PI30
inverter, so its view answers ``not_read_yet``, which is true.

Block lists are copied from what the edge firmware posts, never guessed; each
constant names its source. Pure module: no HA imports, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

SnapshotKind = Literal["deye", "pi30", "luxpower", "must", "smg", "solis"]

# Config register ranges: model family → (start, count)
#
# 3-phase models were widened 2026-07-28 (peak-shaving read path, task 8)
# from 63 to 77 registers (115-191) to reach register 178 (grid
# peak-shaving enable, bit 4) and register 191 (grid peak-shaving power,
# 1 W) — see docs/superpowers/specs/2026-07-26-peak-shaving-registers.md
# in the main svitgrid repo. 1-phase is intentionally left at 63: 1-phase
# peak-shaving addresses are unprobed and gated off everywhere, so we must
# not sync values we don't trust. The server's `validateRegisterBatch`
# accepts BOTH the legacy 63-count 3-phase batch and this widened 77-count
# one, so an add-on that hasn't picked up this change yet is never rejected.
#
# `settings_sync.read_config_registers` always reads in <=25-register chunks
# regardless of this count, so widening here does not add single-frame
# partial-read risk the way it does on the edge-connector firmware path
# (which reads its config batch in one Solarman V5 frame).
#
# The same two sets as `configRegisterBatchForModel` in the monorepo
# (`packages/inverter_protocol/lib/src/registers/config_register_batch.dart`).
CONFIG_RANGES = {
    # 3-phase models: registers 115-191 (77 registers)
    "deye_sg04lp3": (115, 77),
    "deye_sg01hp3": (115, 77),
    "deye_sg01hp3_50k": (115, 77),
    "deye_sg01hp3_30k": (115, 77),
    "deye_sg02hp3_80k": (115, 77),
    "deye_sg05lp3": (115, 77),
    "deye_gb_s20k": (115, 77),
    "sunsynk_3phase": (115, 77),
    "sunsynk_3phase_15k": (115, 77),
    # 1-phase models: registers 217-279 (63 registers, unwidened)
    "deye_sg03lp1": (217, 63),
    "deye_sg01lp1": (217, 63),
    "deye_sg02lp1": (217, 63),
    "deye_sun_g3": (217, 63),
    "deye_sg04lp1": (217, 63),
    "deye_sg05lp1": (217, 63),
    "deye_sg01lp1_16k": (217, 63),
    "sunsynk_1phase": (217, 63),
    "sunsynk_1phase_12k": (217, 63),
    "sunsynk_1phase_16k": (217, 63),
    "deye_sg01lp1_us": (217, 63),
    "solark_5k": (217, 63),
    "solark_12k": (217, 63),
    "solark_15k": (217, 63),
    "solark_18k": (217, 63),
}


def config_range_for_model(model_id: str) -> tuple[int, int] | None:
    """Return (start, count) register range for a model, or None if unsupported."""
    return CONFIG_RANGES.get(model_id)


# ── The kind table ─────────────────────────────────────────────────────
#
# Model-id sets, not prefixes, wherever the Dart asks the model catalogue
# (`InverterModels.findById(...).brand`, `luxpowerSnaSettings.models`,
# `modbusTcpPort != null`). A prefix would pull in a sibling the app keeps on
# the generic screen: the EG4 hybrids and the LXP-LB-EU share the LuxPower
# brand, and `solis_30k_5g` is a grid-tie string inverter with nothing to
# configure. Only MUST is a prefix, because the Dart's `isMustModel` is one.

# `luxpowerSnaSettings.models` (`protocol/luxpower/luxpower_settings.dart`).
LUXPOWER_SNA_MODELS = frozenset({"luxpower_sna_5000_wpv"})

# The catalogue's `InverterBrand.anenji` models (`models_catalog.dart`).
ANENJI_MODELS = frozenset(
    {"anenji_anj_4200", "anenji_anj_5kw", "anenji_anj_6200", "anenji_anj_11kw"}
)

# The catalogue's `InverterBrand.edecoa` models.
EDECOA_MODELS = frozenset({"edecoa_em_624a"})

# `isSolisHybridModel`: the catalogue's Solis models with a `modbusTcpPort`.
SOLIS_HYBRID_MODELS = frozenset(
    {
        "solis_rhi_48es_5g",
        "solis_s6_eh1p_l_plus",
        "solis_s6_eh1p_10k_l_plus",
        "solis_s6_eh3p_h",
        "solis_s6_eh3p_nv_yd_l",
    }
)


def _is_must_model(model_id: str) -> bool:
    """The Dart `isMustModel` (`protocol/must/must_family.dart`)."""
    return model_id.startswith("must_")


def settings_snapshot_kind_for(model_id: str | None) -> SnapshotKind | None:
    """The snapshot kind ``model_id`` has, or None when it has nothing to configure.

    Same order as the Dart ``settingsSnapshotKindFor``, so a model that two
    rules would claim gets the same answer on both sides.
    """
    if not model_id:
        return None
    if model_id in LUXPOWER_SNA_MODELS:
        return "luxpower"
    if config_range_for_model(model_id) is not None:
        return "deye"
    if model_id in ANENJI_MODELS:
        return "smg"
    if model_id in EDECOA_MODELS:
        return "pi30"
    if _is_must_model(model_id):
        return "must"
    if model_id in SOLIS_HYBRID_MODELS:
        return "solis"
    return None


# ── Block lists ────────────────────────────────────────────────────────

# `BLOCKS` in the firmware's
# `components/luxpower_modbus/luxpower_settings.c`, which
# `luxpower_build_sync_body` posts block by block. Holding registers 0-199.
LUXPOWER_SETTINGS_BLOCKS = ((0, 40), (40, 40), (80, 40), (120, 40), (160, 40))

# Holding 7-8 carry the family code, low byte first
# (`LUXPOWER_FAMILY_REG` in `luxpower_settings.h`).
LUXPOWER_FAMILY_REG = 7

# `BLOCKS` in the firmware's `components/must_modbus/must_settings.c`.
MUST_SETTINGS_BLOCKS = ((10101, 12), (20101, 43))

# Single words `read_snapshot` in `must_settings.c` reads after the blocks
# (`MUST_EDITION_REG` and `MUST_PACK_REG` in `must_identity.h`). Either may
# fail without failing the read: the body then leaves the key out.
MUST_EDITION_REG = 20006
MUST_PACK_REG = 25275

# `must_holding_pack`: any other value is no pack voltage at all.
MUST_PACK_VOLTAGES = (12, 24, 48)

# `k_smg_ii_blocks` and `k_smg_p4_blocks` in the firmware's
# `components/eybond_at/smg_settings_sync.c`, keyed by the protocol number
# register 184 holds. Another protocol number has no body.
SMG_SYNC_BLOCKS = {
    11: ((303, 1), (313, 31)),
    4: ((600, 10), (630, 27), (677, 2)),
}

# Every holding address `SolisSettingsExecutor._readRaw` reads, in read
# order (`solisSettingsReadAddresses` in the monorepo's
# `packages/inverter_protocol/lib/src/registers/solis/settings.dart`).
# `tests/fixtures/solis_settings_addresses.json` holds the same list.
SOLIS_SETTINGS_ADDRESSES = (
    *range(43011, 43011 + 14),
    *range(43110, 43110 + 9),
)


def _contiguous_runs(addresses) -> tuple[tuple[int, int], ...]:
    """``addresses`` as ``(start, count)`` runs of consecutive registers."""
    runs: list[tuple[int, int]] = []
    for address in addresses:
        if runs and runs[-1][0] + runs[-1][1] == address:
            start, count = runs[-1]
            runs[-1] = (start, count + 1)
        else:
            runs.append((address, 1))
    return tuple(runs)


@dataclass(frozen=True)
class SettingsReadPlan:
    """How the add-on reads one inverter's settings.

    ``blocks`` are ``(start, count)`` holding-register ranges, all of which
    must answer in full. They are empty for ``smg``, whose blocks depend on
    the protocol number the inverter reports (``SMG_SYNC_BLOCKS``).
    ``extra`` carries what a kind reads besides its blocks.
    """

    kind: Literal["deye", "luxpower", "must", "smg", "solis"]
    blocks: tuple[tuple[int, int], ...]
    extra: dict = field(default_factory=dict)


def settings_read_plan_for(
    model_id: str | None, harvest_config: dict | None, eybond: bool
) -> SettingsReadPlan | None:
    """How to read ``model_id``'s settings over ``harvest_config``, or None.

    None for a model with nothing to configure, and for one this add-on
    cannot read: PI30 (no PI30 harvest), an SMG II inverter that is not on
    an EyBond collector, and any other kind on one (the collector speaks
    only to SMG II units).
    """
    kind = settings_snapshot_kind_for(model_id)
    if kind is None or kind == "pi30":
        return None
    if eybond:
        if kind != "smg":
            return None
        return SettingsReadPlan(kind="smg", blocks=())
    if kind == "smg":
        return None
    protocol = (harvest_config or {}).get("protocol", "solarman_v5")
    if protocol not in ("solarman_v5", "modbus_tcp"):
        return None
    if kind == "deye":
        return SettingsReadPlan(kind="deye", blocks=(config_range_for_model(model_id),))
    if kind == "luxpower":
        return SettingsReadPlan(kind="luxpower", blocks=LUXPOWER_SETTINGS_BLOCKS)
    if kind == "must":
        return SettingsReadPlan(
            kind="must",
            blocks=MUST_SETTINGS_BLOCKS,
            extra={"optional": (MUST_EDITION_REG, MUST_PACK_REG)},
        )
    return SettingsReadPlan(kind="solis", blocks=_contiguous_runs(SOLIS_SETTINGS_ADDRESSES))


# ── Snapshot bodies ────────────────────────────────────────────────────


def blocks_body(blocks, words: dict[int, int]) -> list[dict]:
    """``[{startRegister, registers}]`` for ``blocks``, every word from ``words``."""
    return [
        {"startRegister": start, "registers": [words[a] for a in range(start, start + count)]}
        for start, count in blocks
    ]


def luxpower_family_code(words: dict[int, int]) -> str | None:
    """The family code holding 7-8 carry, or None when it is not printable.

    `luxpower_holding_family` in the firmware: low byte first, and every
    byte 0x21-0x7E. The firmware posts no body without one.
    """
    code = []
    for offset in (0, 1):
        word = words.get(LUXPOWER_FAMILY_REG + offset)
        if word is None:
            return None
        code.extend((word & 0xFF, word >> 8))
    if not all(0x21 <= b <= 0x7E for b in code):
        return None
    return bytes(code).decode("ascii")


def must_edition_text(word: int) -> str:
    """`must_format_edition`: 10415 reads "1.04.15"."""
    return f"{word // 10000}.{(word // 100) % 100:02d}.{word % 100:02d}"


def build_snapshot_body(plan: SettingsReadPlan, words: dict[int, int]) -> dict | None:
    """The brand body for a register read, or None when the read cannot make one.

    ``words`` maps address to value and must hold every address of
    ``plan.blocks``. Not for ``deye``, whose snapshot keeps its own stored
    shape, nor ``smg``, which the EyBond executor builds from its session.
    """
    for start, count in plan.blocks:
        if any(a not in words for a in range(start, start + count)):
            return None
    if plan.kind == "luxpower":
        family = luxpower_family_code(words)
        if family is None:
            return None
        return {
            "protocol": "luxpower",
            "familyCode": family,
            "blocks": blocks_body(plan.blocks, words),
        }
    if plan.kind == "must":
        body: dict = {"protocol": "must"}
        edition = words.get(MUST_EDITION_REG)
        if edition is not None:
            body["edition"] = must_edition_text(edition)
        pack = words.get(MUST_PACK_REG)
        if pack in MUST_PACK_VOLTAGES:
            body["packVoltage"] = pack
        body["blocks"] = blocks_body(plan.blocks, words)
        return body
    if plan.kind == "solis":
        return {
            "protocol": "solis",
            "registers": {str(a): words[a] for a in SOLIS_SETTINGS_ADDRESSES},
        }
    return None


# ── Onboarding bodies ──────────────────────────────────────────────────

_EDITION_TEXT = re.compile(r"^\d+\.\d{2}\.\d{2}$")


def _is_word(value) -> bool:
    return type(value) is int and 0 <= value <= 0xFFFF


def _checked_blocks(blocks, expected) -> tuple[list[dict] | None, str | None]:
    """``blocks`` copied when they lay out exactly as ``expected``, or why not.

    ``bad_body`` for a value that is not a block list, ``bad_range`` for a
    layout other than ``expected``, ``bad_registers`` for a value that is
    not a register word.
    """
    if not isinstance(blocks, list) or not all(
        isinstance(b, dict) and isinstance(b.get("registers"), list) for b in blocks
    ):
        return None, "bad_body"
    layout = [(b.get("startRegister"), len(b["registers"])) for b in blocks]
    if layout != [tuple(e) for e in expected]:
        return None, "bad_range"
    if not all(_is_word(v) for b in blocks for v in b["registers"]):
        return None, "bad_registers"
    return [
        {"startRegister": b["startRegister"], "registers": list(b["registers"])} for b in blocks
    ], None


def onboarding_body(kind: str, settings: dict) -> tuple[dict | None, str | None]:
    """The brand body of an onboarding snapshot, or the reason it is refused.

    ``settings`` is the app's snapshot in the contract's keys; the body that
    comes back is what a read of the same inverter would store, so the
    settings view serves the two alike. Refusals: ``bad_body`` (not this
    kind's body), ``bad_range`` (blocks or addresses other than this
    add-on reads), ``bad_registers`` (a value that is not a register word).
    Not for ``deye``, whose onboarding block keeps its own rules.
    """
    if kind == "luxpower":
        family = settings.get("familyCode")
        if (
            settings.get("protocol") != "luxpower"
            or not isinstance(family, str)
            or len(family) != 4
            or not all(0x21 <= ord(c) <= 0x7E for c in family)
        ):
            return None, "bad_body"
        blocks, error = _checked_blocks(settings.get("blocks"), LUXPOWER_SETTINGS_BLOCKS)
        if error:
            return None, error
        return {"protocol": "luxpower", "familyCode": family, "blocks": blocks}, None
    if kind == "must":
        if settings.get("protocol") != "must":
            return None, "bad_body"
        body: dict = {"protocol": "must"}
        edition = settings.get("edition")
        if _is_word(edition):
            body["edition"] = must_edition_text(edition)
        elif isinstance(edition, str) and _EDITION_TEXT.match(edition):
            body["edition"] = edition
        elif edition is not None:
            return None, "bad_body"
        pack = settings.get("packVoltage")
        if pack is not None:
            if pack not in MUST_PACK_VOLTAGES or type(pack) is not int:
                return None, "bad_body"
            body["packVoltage"] = pack
        blocks, error = _checked_blocks(settings.get("blocks"), MUST_SETTINGS_BLOCKS)
        if error:
            return None, error
        body["blocks"] = blocks
        return body, None
    if kind == "smg":
        number = settings.get("protocolNumber")
        if type(number) is not int or number not in SMG_SYNC_BLOCKS:
            return None, "bad_body"
        blocks, error = _checked_blocks(settings.get("blocks"), SMG_SYNC_BLOCKS[number])
        if error:
            return None, error
        return {"protocolNumber": number, "blocks": blocks}, None
    if kind == "solis":
        registers = settings.get("registers")
        if settings.get("protocol") != "solis" or not isinstance(registers, dict):
            return None, "bad_body"
        if set(registers) != {str(a) for a in SOLIS_SETTINGS_ADDRESSES}:
            return None, "bad_range"
        if not all(_is_word(v) for v in registers.values()):
            return None, "bad_registers"
        return {
            "protocol": "solis",
            "registers": {str(a): registers[str(a)] for a in SOLIS_SETTINGS_ADDRESSES},
        }, None
    return None, "bad_body"
