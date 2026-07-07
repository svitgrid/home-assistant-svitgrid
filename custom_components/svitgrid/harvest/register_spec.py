"""Register-spec data model — Python mirror of the Dart RegisterSpec.

Parses the JSON served by GET /api/v1/register-specs/:modelId. `writes` are
parsed here (SP-C consumes them)."""

from __future__ import annotations

import re
from dataclasses import dataclass

BUILTIN_CATALOG = frozenset(
    {
        "pv_power_from_vi",
        "battery_sign_normalize",
        "battery_temp_clamp",
        "phase_voltage_grid_or_load",
        "phase_load_ct_or_inverter",
        "grid_relay_bit",
        "daily_grid_unavailable",
    }
)


@dataclass(frozen=True)
class ReadDef:
    field: str
    address: int
    words: int = 1
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit_id: int = 1
    sentinel: int | None = None
    function_code: str = "FC03"

    @staticmethod
    def from_dict(d: dict) -> ReadDef:
        return ReadDef(
            field=d["field"],
            address=int(d["address"]),
            words=int(d.get("words", 1)),
            signed=bool(d.get("signed", False)),
            scale=float(d.get("scale", 1.0)),
            offset=float(d.get("offset", 0.0)),
            unit_id=int(d.get("unitId", 1)),
            sentinel=d.get("sentinel"),
            function_code=d.get("functionCode", "FC03"),
        )


@dataclass(frozen=True)
class Derivation:
    field: str
    op: str
    inputs: tuple[str, ...]
    scale: float | None = None
    builtin: str | None = None

    @staticmethod
    def from_dict(d: dict) -> Derivation:
        return Derivation(
            field=d["field"],
            op=d["op"],
            inputs=tuple(d.get("inputs", [])),
            scale=(float(d["scale"]) if d.get("scale") is not None else None),
            builtin=d.get("builtin"),
        )


@dataclass(frozen=True)
class SpecFlags:
    pv_power_from_voltage_current: bool = False
    battery_positive_is_discharge: bool = False
    daily_grid_unavailable: bool = False
    uses_input_registers: bool = False
    grid_relay_address: int | None = None
    grid_relay_mask: int | None = None

    @staticmethod
    def from_dict(d: dict) -> SpecFlags:
        d = d or {}
        return SpecFlags(
            pv_power_from_voltage_current=bool(d.get("pvPowerFromVoltageCurrent", False)),
            battery_positive_is_discharge=bool(d.get("batteryPositiveIsDischarge", False)),
            daily_grid_unavailable=bool(d.get("dailyGridUnavailable", False)),
            uses_input_registers=bool(d.get("usesInputRegisters", False)),
            grid_relay_address=d.get("gridRelayAddress"),
            grid_relay_mask=d.get("gridRelayMask"),
        )


_ENCODING_RE = re.compile(r"^bit:\d+$")


def _validate_field_write(command: str, fw: FieldWrite, context: str) -> list[str]:
    """Return a (possibly empty) list of problems for one FieldWrite."""
    problems: list[str] = []
    if fw.encoding != "full_word" and not _ENCODING_RE.match(fw.encoding):
        problems.append(
            f"write command {command!r} {context} field {fw.payload_field!r}: "
            f"invalid encoding {fw.encoding!r}"
        )
    if not fw.payload_field:
        problems.append(
            f"write command {command!r} {context} field: payload_field must be non-empty"
        )
    return problems


@dataclass(frozen=True)
class FieldWrite:
    payload_field: str
    address: int | None = None
    base: int | None = None
    encoding: str = "full_word"
    value_scale: float = 1.0
    on_value: int | None = None
    off_value: int | None = None
    clear_mask: int | None = None
    via_next_slot: bool = False
    limits: object | None = None

    @staticmethod
    def from_dict(d: dict) -> FieldWrite:
        return FieldWrite(
            payload_field=d["payloadField"],
            address=(int(d["address"]) if d.get("address") is not None else None),
            base=(int(d["base"]) if d.get("base") is not None else None),
            encoding=d.get("encoding", "full_word"),
            value_scale=float(d.get("valueScale", 1.0)),
            on_value=(int(d["onValue"]) if d.get("onValue") is not None else None),
            off_value=(int(d["offValue"]) if d.get("offValue") is not None else None),
            clear_mask=(int(d["clearMask"]) if d.get("clearMask") is not None else None),
            via_next_slot=bool(d.get("viaNextSlot", False)),
            limits=d.get("limits"),
        )


@dataclass(frozen=True)
class SlotSpec:
    index_field: str
    count: int
    stride: int
    end_via_next_slot: bool
    fields: tuple[FieldWrite, ...]

    @staticmethod
    def from_dict(d: dict) -> SlotSpec:
        return SlotSpec(
            index_field=d["indexField"],
            count=int(d["count"]),
            stride=int(d["stride"]),
            end_via_next_slot=bool(d.get("endViaNextSlotStart", False)),
            fields=tuple(FieldWrite.from_dict(f) for f in d.get("fields", [])),
        )


@dataclass(frozen=True)
class WriteCommand:
    command: str
    fields: tuple[FieldWrite, ...]
    slot: SlotSpec | None = None

    @staticmethod
    def from_dict(d: dict) -> WriteCommand:
        return WriteCommand(
            command=d["command"],
            fields=tuple(FieldWrite.from_dict(f) for f in d.get("fields", [])),
            slot=(SlotSpec.from_dict(d["slot"]) if d.get("slot") is not None else None),
        )


@dataclass(frozen=True)
class RegisterSpec:
    model_id: str
    version: int
    protocol: str
    port: int
    default_slave_id: int
    flags: SpecFlags
    reads: tuple[ReadDef, ...]
    derivations: tuple[Derivation, ...]
    writes: tuple[WriteCommand, ...]

    @staticmethod
    def from_dict(d: dict) -> RegisterSpec:
        return RegisterSpec(
            model_id=d["modelId"],
            version=int(d["version"]),
            protocol=d["protocol"],
            port=int(d["port"]),
            default_slave_id=int(d.get("defaultSlaveId", 1)),
            flags=SpecFlags.from_dict(d.get("flags") or {}),
            reads=tuple(ReadDef.from_dict(r) for r in d.get("reads", [])),
            derivations=tuple(Derivation.from_dict(x) for x in d.get("derivations", [])),
            writes=tuple(WriteCommand.from_dict(w) for w in d.get("writes", [])),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        known = {r.field for r in self.reads} | {x.field for x in self.derivations}
        for x in self.derivations:
            if x.op == "builtin" and (x.builtin not in BUILTIN_CATALOG):
                problems.append(f"unknown builtin: {x.builtin}")
            for inp in x.inputs:
                if inp == "|":  # phase-select group separator, not a field
                    continue
                if inp not in known:
                    problems.append(f"derivation {x.field} references missing field: {inp}")
        for wc in self.writes:
            for fw in wc.fields:
                problems.extend(_validate_field_write(wc.command, fw, "top-level"))
            if wc.slot is not None:
                if not wc.slot.index_field:
                    problems.append(f"write command {wc.command!r} slot: missing index_field")
                if wc.slot.count < 1:
                    problems.append(f"write command {wc.command!r} slot: count must be >= 1")
                if not wc.slot.fields:
                    problems.append(f"write command {wc.command!r} slot: fields must be non-empty")
                for fw in wc.slot.fields:
                    problems.extend(_validate_field_write(wc.command, fw, "slot"))
        return problems
