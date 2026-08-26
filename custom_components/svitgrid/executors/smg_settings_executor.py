"""Reads and writes the SMG II configuration block over a collector session.

Ported from `smg_settings_executor.dart` + `register_writer.dart` in the
Flutter repo (`packages/inverter_protocol/lib/src/protocol/eybond_at/`). Same
semantics: three validation stages before anything reaches the device, a
read-back-verified write, and a group-wide unconfirmed lock over the six
DC voltage setpoints whenever the derivation the six share (doubling the
measured 24 V bounds for a 48 V pack) is contradicted by what the device
actually holds.

`link` is anything with `CollectorSession`'s `read_registers` /
`write_register` signature (`(address, count_or_value, timeout_s=5.0)`). In
production that is a live `CollectorSession`; in tests it is a fake. This
class does not itself resolve a *live* session from a hub -- see
`EybondSmgSettingsExecutor` below, which is what `__init__.py` actually wires
up for an inverter on the EyBond collector transport, and which re-resolves
the current session (and the device's live protocol number) on every
dispatch, because the collector can reconnect -- or connect to a swapped
unit -- between polls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ..eybond_at.identity import REG_PROTOCOL
from ..eybond_at.register_map import MAX_BLOCK_GAP, MAX_BLOCK_REGISTERS
from ..eybond_at.session import TransactionFailed, WriteRefused
from ..eybond_at.smg_settings import (
    SmgSetting,
    smg_settings_for,
    unevaluatable_constraints_for,
    validate_smg_settings,
)
from .base import BaseExecutor

_LOGGER = logging.getLogger(__name__)


class RegisterLink(Protocol):
    async def read_registers(
        self, address: int, count: int, timeout_s: float = 5.0
    ) -> list[int]: ...

    async def write_register(self, address: int, value: int, timeout_s: float = 5.0) -> None: ...


@dataclass(frozen=True)
class VerifiedWrite:
    """What a verified write actually achieved."""

    # The register holds the requested value.
    ok: bool

    # The register already held it, so nothing was sent.
    skipped: bool

    # The value we asked for (raw register units).
    written: int

    # What the register held afterwards, in raw register units. None when
    # the read-back failed, and None when the device refused outright -- a
    # refusal means nothing was written, so there is nothing to read back.
    read_back: int | None

    message: str

    # Which documented refusal the DEVICE returned, if any: "read_only",
    # "out_of_range", "wrong_mode", or "device_error". None covers both a
    # success and a value our own validation rejected before sending -- our
    # decision is not the device's, and labelling it with a device reason
    # would misattribute it.
    refusal: str | None = None


async def write_register_verified(
    link: RegisterLink, *, address: int, value: int, timeout_s: float = 5.0
) -> VerifiedWrite:
    """Writes `value` to `address` and proves it stuck.

    The acknowledgement is not treated as evidence. An FC16 ack proves the
    device PARSED the request -- it carries the quantity of registers
    written, not even the value -- and a register that is read-only in
    practice can acknowledge without raising a Modbus exception. Only a
    read-back distinguishes those cases -- and on this hardware the values in
    question are battery charge setpoints, where "it said OK and did nothing"
    is the failure that costs a pack.

    A write the DEVICE refuses returns `ok=False` with `refusal` naming which
    of the three documented refusals it was, so the caller can tell the user
    something true rather than "it failed". A write that could not be
    attempted at all -- a timeout, a dropped line -- still raises
    `TransactionFailed`: nothing is known about it, and inventing a reason
    would be worse than propagating the failure. A write that lands but
    cannot be verified returns `ok=False` rather than raising: the change may
    well have taken effect, and reporting success for something unconfirmed
    is worse than reporting uncertainty.
    """
    # Skip a write that would change nothing. Fewer writes to an unproven
    # path is strictly better, and it makes an idempotent "apply settings"
    # cheap.
    try:
        current = await link.read_registers(address, 1, timeout_s=timeout_s)
        if current and current[0] == value:
            return VerifiedWrite(
                ok=True,
                skipped=True,
                written=value,
                read_back=value,
                message=f"register {address} already holds {value}",
            )
    except TransactionFailed:
        # Could not pre-read. Fall through and attempt the write anyway: the
        # read-back below is what decides the outcome.
        pass

    try:
        await link.write_register(address, value, timeout_s=timeout_s)
    except WriteRefused as err:
        # The device answered and declined. That is a fact worth reporting
        # precisely: "not in this working mode" and "read-only" send the user
        # in opposite directions, and neither is "the write may have worked".
        # Returned rather than raised so the reason survives into the command
        # response the user actually sees.
        return VerifiedWrite(
            ok=False,
            skipped=False,
            written=value,
            read_back=None,
            message=str(err),
            refusal=err.reason,
        )

    try:
        words = await link.read_registers(address, 1, timeout_s=timeout_s)
        read_back = words[0] if words else None
    except TransactionFailed as err:
        return VerifiedWrite(
            ok=False,
            skipped=False,
            written=value,
            read_back=None,
            message=(
                f"wrote {value} to register {address} but could not read it "
                f"back: {err}"
            ),
        )

    if read_back == value:
        return VerifiedWrite(
            ok=True,
            skipped=False,
            written=value,
            read_back=read_back,
            message=f"register {address} now holds {value}",
        )

    return VerifiedWrite(
        ok=False,
        skipped=False,
        written=value,
        read_back=read_back,
        message=f"wrote {value} to register {address} but it holds {read_back}",
    )


@dataclass(frozen=True)
class SmgReadResult:
    """One read of the whole configuration block."""

    # Display-unit values keyed by setting name.
    values: dict[str, float | int]

    # Settings whose DERIVED bounds the device's own held value contradicts.
    # These render read-only and refuse writes: a bound nobody measured has
    # just been shown to be wrong, and the register is protective.
    unconfirmed: set[str]


class SmgSettingsExecutor(BaseExecutor):
    """Reads and writes the SMG II configuration block over any register link.

    Transport-agnostic on purpose, matching the Dart original: the mobile
    harvester reaches this inverter through a `CollectorSession`, while every
    other model in the Flutter app is reached through a different client.
    Binding the settings logic to either would mean writing it twice, and the
    half that mattered -- the cross-field validation -- would then exist in
    two places with one of them eventually wrong.
    """

    def __init__(
        self,
        *,
        link: RegisterLink,
        protocol_number: int,
        nominal_pack_voltage: int,
        timeout_s: float = 5.0,
    ) -> None:
        self._link = link
        self._timeout_s = timeout_s
        # The device's reported protocol number (register 184), echoed back
        # in read_inverter_settings so a caller building this executor from a
        # live session (rather than a hardcoded constant) can report it
        # alongside a read without keeping a second copy in step.
        self.protocol_number = protocol_number
        # Empty only when the protocol number is not recognised. A pack
        # voltage with no bounds table still yields the pack-independent
        # settings.
        self.settings: list[SmgSetting] = smg_settings_for(
            protocol_number=protocol_number, nominal_pack_voltage=nominal_pack_voltage
        )

    def _by_key(self, key: str) -> SmgSetting | None:
        for s in self.settings:
            if s.key == key:
                return s
        return None

    def _unconfirmed(self, raw: dict[int, int]) -> set[str]:
        """Settings whose DERIVED bounds `raw` contradicts.

        All-or-nothing over the pack-dependent group: if ANY `bounds_derived`
        setting holds a value outside its own derived range, EVERY
        `bounds_derived` setting comes back unconfirmed, not just the
        offender. One contradicted bound falsifies the derivation itself, and
        that derivation (doubling the 24 V table) is shared by the whole
        group -- trusting the other five after one has been proven wrong is
        exactly the reasoning this guard exists to prevent.
        """
        derived = [s for s in self.settings if s.bounds_derived]
        any_contradicted = False
        for s in derived:
            value = raw.get(s.address)
            # A short read (fewer words than requested -- see `_read_raw`,
            # which fills only what actually arrived) leaves this address
            # absent from `raw` rather than zero. Absent means "we don't
            # know", not "the device agrees" -- reading it as agreement is
            # exactly backwards for a guard whose only job is refusing a
            # write it cannot vouch for.
            if value is None or not s.contains(value):
                any_contradicted = True
                break
        if not any_contradicted:
            return set()
        return {s.key for s in derived}

    async def read(self) -> SmgReadResult:
        """Every catalogued setting, in display units, plus which derived
        bounds the device has just contradicted."""
        raw = await self._read_raw()
        values: dict[str, float | int] = {}
        for s in self.settings:
            if s.address in raw:
                values[s.key] = raw[s.address] if s.decimals == 0 else s.to_display(raw[s.address])
        return SmgReadResult(values=values, unconfirmed=self._unconfirmed(raw))

    async def read_all(self) -> dict[str, float | int]:
        """Every catalogued setting, in display units, keyed by setting name."""
        return (await self.read()).values

    async def _read_raw(self) -> dict[int, int]:
        """Raw register values, read in contiguous blocks.

        One request is in flight at a time on this transport and there is no
        transaction id, so each round trip costs a full turnaround. Eighteen
        of them is a visibly slow screen; a handful of blocks is not.
        """
        if not self.settings:
            return {}
        addresses = sorted({s.address for s in self.settings})

        values: dict[int, int] = {}
        start = previous = addresses[0]
        for address in [*addresses[1:], -1]:
            break_here = (
                address < 0
                or address - previous > MAX_BLOCK_GAP
                or address - start + 1 > MAX_BLOCK_REGISTERS
            )
            if break_here:
                count = previous - start + 1
                words = await self._link.read_registers(start, count, timeout_s=self._timeout_s)
                for i, word in enumerate(words):
                    values[start + i] = word
                start = address
            previous = address
        return values

    async def apply(self, key: str, display_value: float) -> VerifiedWrite:
        """Applies one setting, in display units.

        Validates in three stages, and nothing reaches the device until all
        three pass: the key is known, the value is inside the published
        range, and the resulting combination satisfies every cross-field
        constraint.

        The combination is checked against what the device is holding RIGHT
        NOW, re-read for the purpose. A cached copy goes stale the moment
        somebody changes a value at the inverter's own panel, and the failure
        that follows is a pack-damaging combination that our validation
        approved.
        """
        setting = self._by_key(key)
        if setting is None:
            return VerifiedWrite(
                ok=False, skipped=False, written=0, read_back=None,
                message=f'unknown setting "{key}"',
            )

        raw = setting.to_raw(float(display_value))
        if not setting.contains(raw):
            return VerifiedWrite(
                ok=False, skipped=False, written=raw, read_back=None,
                message=(
                    f"{key} {display_value}{setting.unit} is outside the permitted "
                    f"range {setting.to_display(setting.raw_min)}"
                    f"–{setting.to_display(setting.raw_max)}{setting.unit}"
                ),
            )

        current = dict(await self._read_raw())
        unconfirmed = self._unconfirmed(current)
        if key in unconfirmed:
            # Name the ACTUAL offender, not `setting`: under the group rule
            # this may not be the same register. floatChargeVoltage can be
            # refused while its own held value sits fine inside its own
            # derived range -- it is unconfirmed because a DIFFERENT setpoint
            # in the group falsified the shared derivation.
            offender: SmgSetting | None = None
            for s in self.settings:
                if s.bounds_derived and s.address in current and not s.contains(current[s.address]):
                    offender = s
                    break
            if offender is None:
                # No `settings` entry currently out of its derived range,
                # even though `key in unconfirmed` guarantees SOME
                # bounds_derived setting is -- that guarantee lives in a
                # second, separately-evaluated method. A future edit could
                # desync them, and this is a protective-register guard path
                # -- it must degrade to a still-true, if less specific,
                # message rather than assume an invariant that just broke.
                message = (
                    f"{key} has unconfirmed bounds for this pack: the derivation "
                    "shared by this group is contradicted by the device. "
                    "Refusing to write."
                )
            else:
                message = (
                    f"{key} has unconfirmed bounds for this pack: {offender.key} "
                    f"holds {offender.to_display(current[offender.address])}"
                    f"{offender.unit}, outside the range derived for it -- that "
                    "falsifies the derivation shared by the whole group. Refusing "
                    "to write."
                )
            return VerifiedWrite(
                ok=False, skipped=False, written=raw, read_back=None, message=message
            )

        # Every unconfirmed setting shares one falsified derivation (see
        # _unconfirmed), so none of their raw values can be trusted for a
        # cross-field comparison against ANY other setting either. Drop them
        # all before validating: validate_smg_settings already skips a
        # constraint whose fields are not both present, so removal is enough
        # to express "unknown, don't check" without silently blocking an
        # unrelated write.
        for_validation = dict(current)
        for s in self.settings:
            if s.key in unconfirmed:
                for_validation.pop(s.address, None)
        for_validation[setting.address] = raw

        # Refuse a write whose cross-field constraints cannot all be
        # EVALUATED, not just the ones it would break. validate_smg_settings
        # skips a constraint whose partner register is absent, so a short read
        # -- fewer words than requested, which CollectorSession.read_registers
        # passes through without ever comparing against the count asked for --
        # makes the protective comparison vanish instead of failing. At 24 V
        # nothing is bounds_derived, so the unconfirmed group lock above
        # returns an empty set no matter how little of the block arrived, and
        # this is the only thing standing between a truncated read and an
        # unvalidated write to a battery charge setpoint.
        unevaluatable = unevaluatable_constraints_for(setting.address, for_validation)
        if unevaluatable:
            detail = ", ".join(
                f"{constraint_key} needs register {partner}"
                for constraint_key, partner in unevaluatable
            )
            return VerifiedWrite(
                ok=False, skipped=False, written=raw, read_back=None,
                message=(
                    f"{key} cannot be checked against what the device holds: "
                    f"{detail}. Refusing to write."
                ),
            )

        violations = validate_smg_settings(for_validation)
        if violations:
            return VerifiedWrite(
                ok=False, skipped=False, written=raw, read_back=None,
                message=f"would break {', '.join(v.key for v in violations)}",
            )

        return await write_register_verified(
            self._link, address=setting.address, value=raw, timeout_s=self._timeout_s
        )

    # ── BaseExecutor / dispatch contract ────────────────────────────────

    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        # This executor does not implement the legacy set_battery_charge
        # entry point; it only serves the two commands below.
        raise NotImplementedError(
            "SmgSettingsExecutor does not support set_battery_charge"
        )

    async def dispatch(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command_name == "read_inverter_settings":
            result = await self.read()
            return {
                "protocolNumber": self.protocol_number,
                "settings": dict(result.values),
                "unconfirmed": sorted(result.unconfirmed),
            }
        if command_name == "set_inverter_setting":
            key = payload["setting"]
            value = payload["value"]
            setting = self._by_key(key)
            vw = await self.apply(key, value)
            read_back_display: float | int | None
            if vw.read_back is None:
                read_back_display = None
            elif setting is not None:
                read_back_display = (
                    vw.read_back if setting.decimals == 0 else setting.to_display(vw.read_back)
                )
            else:
                # Unknown key: nothing to convert against, and read_back is
                # always None on that path anyway (see apply()).
                read_back_display = vw.read_back
            return {
                "ok": vw.ok,
                "readBack": read_back_display,
                "message": vw.message or None,
                "refusal": vw.refusal,
            }
        raise NotImplementedError(f"SmgSettingsExecutor does not support command {command_name!r}")


class NoCollectorConnected(Exception):
    """No live CollectorSession for this inverter's serial right now."""


class EybondSmgSettingsExecutor(BaseExecutor):
    """The executor `__init__.py` wires up for an inverter on the EyBond
    collector transport.

    `SmgSettingsExecutor` above is a faithful, transport-agnostic port of the
    Dart original: it takes a fixed link and a fixed protocol number at
    construction, exactly like the mobile app's short-lived instance built
    once a settings screen opens. This add-on's executors, by contrast, are
    built once at config-entry setup and live for the lifetime of the entry,
    while the collector connection they depend on comes and goes -- the same
    problem `run_eybond_harvest_loop` solves by re-fetching
    `hub.session_for(serial)` every tick rather than holding one session
    forever (see `eybond_at/harvest.py`).

    So this class holds no session and no protocol number. On every dispatch
    it resolves the CURRENT session from the hub, reads register 184 fresh
    (the collector may have reconnected to a different unit since the last
    command), and builds a throwaway `SmgSettingsExecutor` around both. That
    is one extra register read per command -- negligible next to the 18
    already needed for the read it is about to do -- for the alternative of
    silently trusting a protocol number that may no longer be true.
    """

    def __init__(
        self,
        *,
        hub,
        inverter_serial: str | None,
        nominal_pack_voltage: int = 24,
        timeout_s: float = 5.0,
    ) -> None:
        self._hub = hub
        self._inverter_serial = inverter_serial
        self._nominal_pack_voltage = nominal_pack_voltage
        self._timeout_s = timeout_s

    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            "EybondSmgSettingsExecutor does not support set_battery_charge"
        )

    async def dispatch(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._hub.session_for(self._inverter_serial)
        if session is None:
            raise NoCollectorConnected(
                f"no collector connected for serial {self._inverter_serial!r}"
            )
        protocol_words = await session.read_registers(
            REG_PROTOCOL, 1, timeout_s=self._timeout_s
        )
        protocol_number = protocol_words[0] if protocol_words else 0
        inner = SmgSettingsExecutor(
            link=session,
            protocol_number=protocol_number,
            nominal_pack_voltage=self._nominal_pack_voltage,
            timeout_s=self._timeout_s,
        )
        return await inner.dispatch(command_name, payload)
