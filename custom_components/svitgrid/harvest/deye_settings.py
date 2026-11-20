"""`set_inverter_setting` for a Deye hybrid (svitgrid D-2346).

One setting per command, planned by the shared catalogue
(``deye_settings.json``, vendored from the monorepo's
``packages/inverter_protocol/deye-settings.json`` by
``scripts/sync-edge-command-fixtures.sh``). The phone harvester
(``deye_settings_executor.dart``), the S3 (``components/deye_settings``) and
the API fence plan from the same file, so this add-on refuses exactly what
they refuse.

Every check runs against registers read from the inverter in this command,
not against a mirror that can be minutes old: the setting's own register,
the battery mode word, and each threshold it is linked to. A read that fails
refuses the write. A bit setting changes only its bit of the word it read.
Writes are FC16 with one word, then a read-back.

Values cross every boundary in display units. The result is
``{"ok", "readBack", "message"}``, the shape the board and the phone answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings_read_plan import CONFIG_RANGES

_LOGGER = logging.getLogger(__name__)

CATALOGUE_PATH = Path(__file__).parent / "deye_settings.json"

# The 3-phase config batch starts here; every other Deye model is 1-phase.
_THREE_PHASE_BATCH_START = 115

# Models whose power registers count 10 W (`DEYE_10W_POWER_MODELS` in the
# API's register-mapping.ts, `deye10WPowerModels` in the Dart package).
DEYE_10W_POWER_MODELS = frozenset(
    {"deye_sg01hp3_30k", "deye_sg01hp3_50k", "deye_sg02hp3_80k", "deye_gb_s20k"}
)

# Registers no Svitgrid writer ever touches (`blockedRegisters` in
# battery_charge_registers.dart and its C twin). The catalogue holds none of
# them; this keeps a later edit from writing one anyway.
_BLOCKED_REGISTERS = frozenset({20, 80, 201, 202, 203, 210, 211, 220, 221, 222})

_BATTERY_MODE_NAMES = {0: "voltage", 1: "soc", 2: "none"}

# Seconds between the write and its read-back, as the phone and the board wait.
SETTLE_S = 2.0


@dataclass(frozen=True)
class DeyeSetting:
    key: str
    mirror_key: str
    encoding: str
    register3p: int
    register1p: int
    unit: str
    min: int | None
    max: int | None
    max_is_rated_power: bool
    not_on_ten_watt_models: bool
    mask: int | None
    choices: tuple[int, ...]
    requires_battery_mode: str | None
    writable: bool

    def register_for(self, three_phase: bool) -> int:
        return self.register3p if three_phase else self.register1p

    def to_display(self, word: int) -> int:
        if self.encoding == "bit":
            return 1 if word & (self.mask or 0) else 0
        return word


@dataclass(frozen=True)
class DeyeConstraint:
    low: str
    high: str
    rule: str


@dataclass(frozen=True)
class WritePlan:
    """A planned write, or the reason there is none."""

    refusal: str | None = None
    message: str | None = None
    register: int | None = None
    raw: int | None = None
    mask: int | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def _refused(refusal: str, message: str) -> WritePlan:
    return WritePlan(refusal=refusal, message=message)


class DeyeSettingsCatalogue:
    def __init__(self, data: dict[str, Any]) -> None:
        fam = data["families"]
        self.battery_mode_register = {
            True: int(fam["threePhase"]["batteryModeRegister"]),
            False: int(fam["onePhase"]["batteryModeRegister"]),
        }
        self.settings: list[DeyeSetting] = [
            DeyeSetting(
                key=s["key"],
                mirror_key=s["mirrorKey"],
                encoding=s["encoding"],
                register3p=int(s["register3p"]),
                register1p=int(s["register1p"]),
                unit=s.get("unit", ""),
                min=s.get("min"),
                max=s.get("max"),
                max_is_rated_power=bool(s.get("maxIsRatedPower", False)),
                not_on_ten_watt_models=bool(s.get("notOnTenWattModels", False)),
                mask=s.get("mask"),
                choices=tuple(s.get("choices", [])),
                requires_battery_mode=s.get("requiresBatteryMode"),
                writable=bool(s["writable"]),
            )
            for s in data["settings"]
        ]
        self.constraints: list[DeyeConstraint] = [
            DeyeConstraint(c["low"], c["high"], c["rule"]) for c in data["constraints"]
        ]

    def by_key(self, key: str) -> DeyeSetting | None:
        return next((s for s in self.settings if s.key == key), None)

    def partners(self, key: str) -> list[DeyeSetting]:
        """The settings *key* is checked against, in constraint order."""
        out = []
        for c in self.constraints:
            if c.low == key:
                out.append(self.by_key(c.high))
            elif c.high == key:
                out.append(self.by_key(c.low))
        return [p for p in out if p is not None]

    def plan_write(
        self,
        key: str,
        value: Any,
        *,
        model_id: str,
        rated_power_w: int | None,
        battery_mode: str | None,
        current: dict[str, int],
    ) -> WritePlan:
        """Plan writing *value* to *key*, as the Dart ``planWrite`` does.

        A constraint whose partner was not read is refused rather than
        skipped: a missing register must not remove a check on a protective
        setting.
        """
        s = self.by_key(key)
        if s is None:
            return _refused("unknownSetting", f"Svitgrid does not write {key} on a Deye.")
        if not s.writable:
            return _refused("notWritable", f"{key} is read-only.")
        three_phase = deye_three_phase(model_id)
        if three_phase is None:
            return _refused("notDeye", "This inverter is not in the Deye family.")
        if s.not_on_ten_watt_models and model_id in DEYE_10W_POWER_MODELS:
            return _refused(
                "notOnThisModel",
                f"{key} is not written on this model, whose power registers count 10 W.",
            )
        # bool is an int in Python; the other hosts take only a number.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value != value
            or float(value) != float(int(value))
        ):
            return _refused("wrongType", f"{key} takes a whole number.")
        v = int(value)

        if s.encoding == "bit":
            if v not in (0, 1):
                return _out_of_range(s, "0 or 1")
        elif s.encoding == "choice":
            if v not in s.choices:
                return _out_of_range(s, ", ".join(str(c) for c in s.choices))
        else:
            top = rated_power_w if s.max_is_rated_power else s.max
            if top is None:
                return _refused(
                    "outOfRange",
                    f"The rated power of this inverter is unknown, so {key} cannot be checked.",
                )
            if (s.min is not None and v < s.min) or v > top:
                return _out_of_range(s, f"{s.min if s.min is not None else 0}-{top}{s.unit}")

        if s.requires_battery_mode is not None and battery_mode != s.requires_battery_mode:
            return _refused(
                "batteryMode",
                f"{key} applies only while the battery runs on percent thresholds.",
            )

        for c in self.constraints:
            if key not in (c.low, c.high):
                continue
            partner = c.high if c.low == key else c.low
            other = current.get(partner)
            if other is None:
                return _refused(
                    "partnerUnread",
                    f"{partner} has not been read from the inverter yet, so {key} "
                    "cannot be checked against it.",
                )
            low = v if c.low == key else other
            high = v if c.high == key else other
            if not low < high:
                return _refused("constraint", c.rule)

        register = s.register_for(three_phase)
        if register in _BLOCKED_REGISTERS:
            return _refused("notWritable", f"Register {register} is never written.")
        if s.encoding == "bit":
            return WritePlan(register=register, raw=s.mask if v == 1 else 0, mask=s.mask)
        return WritePlan(register=register, raw=v)


def _out_of_range(s: DeyeSetting, bounds: str) -> WritePlan:
    return _refused("outOfRange", f"{s.key} must be {bounds}.")


def deye_three_phase(model_id: str | None) -> bool | None:
    """True for the 3-phase family, False for 1-phase, None outside Deye.

    From the settings mirror's config batch, so a model gets these settings
    exactly when the mirror reads its Deye registers.
    """
    batch = CONFIG_RANGES.get(model_id or "")
    if batch is None:
        return None
    return batch[0] == _THREE_PHASE_BATCH_START


def apply_bits(word: int, mask: int, raw: int) -> int:
    """*word* with only the bits in *mask* replaced by those of *raw*."""
    return (word & ~mask & 0xFFFF) | (raw & mask)


def load_catalogue() -> DeyeSettingsCatalogue:
    return DeyeSettingsCatalogue(json.loads(CATALOGUE_PATH.read_text()))


# Read once at import, like the other bundled tables: imports run off the
# event loop.
CATALOGUE = load_catalogue()

ReadWord = Callable[[int], Awaitable[int | None]]
WriteWord = Callable[[int, int], Awaitable[None]]


async def apply_deye_setting(
    *,
    model_id: str | None,
    payload: dict[str, Any],
    read_word: ReadWord,
    write_word: WriteWord,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    catalogue: DeyeSettingsCatalogue = CATALOGUE,
) -> dict[str, Any]:
    """Run one ``set_inverter_setting`` and return ``{ok, readBack, message}``.

    *read_word* returns a holding register's word, or None when it could not
    be read. *write_word* writes one word with FC16 and raises on failure.
    """
    key = str(payload.get("setting") or "")
    value = payload.get("value")
    rated = payload.get("ratedPowerW")
    rated_power_w = (
        int(rated)
        if isinstance(rated, (int, float)) and not isinstance(rated, bool) and rated > 0
        else None
    )
    model = model_id or ""

    setting = catalogue.by_key(key)
    three_phase = deye_three_phase(model)
    if setting is None or three_phase is None:
        plan = catalogue.plan_write(
            key, value, model_id=model, rated_power_w=rated_power_w, battery_mode=None, current={}
        )
        return _answer(False, plan.message)

    own_register = setting.register_for(three_phase)
    mode_register = (
        catalogue.battery_mode_register[three_phase]
        if setting.requires_battery_mode is not None
        else None
    )
    partners = catalogue.partners(key)
    reads = [own_register]
    if mode_register is not None:
        reads.append(mode_register)
    reads += [p.register_for(three_phase) for p in partners]

    words: dict[int, int] = {}
    for register in dict.fromkeys(reads):
        word = await read_word(register)
        if word is None:
            return _answer(
                False,
                f"Register {register} could not be read from the inverter, so nothing was written.",
            )
        words[register] = word

    own_word = words[own_register]
    current = {key: setting.to_display(own_word)}
    for p in partners:
        current[p.key] = p.to_display(words[p.register_for(three_phase)])
    plan = catalogue.plan_write(
        key,
        value,
        model_id=model,
        rated_power_w=rated_power_w,
        battery_mode=(
            _BATTERY_MODE_NAMES.get(words[mode_register]) if mode_register is not None else None
        ),
        current=current,
    )
    if not plan.ok:
        _LOGGER.info("deye setting %s refused: %s", key, plan.message)
        return _answer(False, plan.message, setting.to_display(own_word))

    target = plan.raw if plan.mask is None else apply_bits(own_word, plan.mask, plan.raw or 0)
    if target == own_word:
        return _answer(True, None, setting.to_display(own_word))

    _LOGGER.info("deye setting %s: writing register %s = %s", key, own_register, target)
    try:
        await write_word(own_register, target)
    except Exception as err:  # noqa: BLE001 -- the answer carries it
        return _answer(False, f"The inverter did not accept the write: {err}")

    await sleep(SETTLE_S)
    read_back = await read_word(own_register)
    if read_back is None:
        return _answer(
            False, f"The write was sent, but register {own_register} could not be read back."
        )
    landed = (
        read_back == target
        if plan.mask is None
        else (read_back & plan.mask) == (target & plan.mask)
    )
    if not landed:
        return _answer(False, "The inverter kept its old value.", setting.to_display(read_back))
    return _answer(True, None, setting.to_display(read_back))


def _answer(ok: bool, message: str | None, read_back: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "message": message}
    if read_back is not None:
        out["readBack"] = read_back
    return out
