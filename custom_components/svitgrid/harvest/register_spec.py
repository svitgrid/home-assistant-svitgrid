"""Register-spec data model — Python mirror of the Dart RegisterSpec.

Parses the JSON served by GET /api/v1/register-specs/:modelId. `writes` are
ignored here (SP-C consumes them)."""
from __future__ import annotations

from dataclasses import dataclass

BUILTIN_CATALOG = frozenset({
    "pv_power_from_vi", "battery_sign_normalize", "battery_temp_clamp",
    "phase_voltage_grid_or_load", "phase_load_ct_or_inverter",
    "grid_relay_bit", "daily_grid_unavailable",
})


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
        return problems
