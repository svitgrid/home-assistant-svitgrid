"""Identify the device, then choose its register map from what it reports.

**Never choose a register map from a brand or a model name.** Anenji already
ships two different platforms, and inside the SmartESS family the map is
versioned by register 184. A map chosen from a name the user picked during
onboarding would decode a real frame into plausible, wrong numbers.

The identity block is the stable part. Registers 171, 184, and 186 mean the
same thing in every published SMG II map and on our hardware, so they can be
read before any map has been selected.

Measured 2026-08-20 on collector `I20000282044487591`:

    171  device type       0x7803
    184  protocol number   11
    186  serial, 12 regs   "99432604107106"
    626  firmware, 8 regs  "7803_A6260126v1"

Register 171 is the firmware prefix as packed binary-coded decimal -- `0x7803`
renders as the four digits that open the firmware string. That correspondence
is what makes it trustworthy as an identifier rather than a coincidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .modbus_rtu import words_to_ascii
from .register_map import PLATFORMS, OutputMode, RegisterMap

_LOGGER = logging.getLogger(__name__)

REG_DEVICE_TYPE = 171
REG_PROTOCOL = 184
REG_SERIAL = 186
SERIAL_REGISTERS = 12
REG_FIRMWARE = 626
FIRMWARE_REGISTERS = 8


class UnknownPlatform(Exception):
    """The device reports a protocol number we have never measured."""


class ReadsRegisters(Protocol):
    async def read_registers(
        self, address: int, count: int, timeout_s: float = 5.0
    ) -> list[int]: ...


@dataclass(frozen=True)
class DeviceIdentity:
    protocol_number: int
    device_type: int
    serial: str
    firmware: str
    # How this unit is wired in -- standalone, one of a parallel bank, or one
    # phase of three. Read from the device rather than configured, so a
    # rewire is picked up without anyone editing anything.
    output_mode: OutputMode = OutputMode.UNKNOWN


async def identify(link: ReadsRegisters) -> DeviceIdentity:
    """Read the identity block. Only the firmware string is optional."""
    device_type = (await link.read_registers(REG_DEVICE_TYPE, 1))[0]
    protocol_number = (await link.read_registers(REG_PROTOCOL, 1))[0]
    serial = words_to_ascii(await link.read_registers(REG_SERIAL, SERIAL_REGISTERS))

    # Corroboration, not identification: the protocol number selects the map.
    # A device that does not carry the string is still fully identified.
    firmware = ""
    try:
        firmware = words_to_ascii(await link.read_registers(REG_FIRMWARE, FIRMWARE_REGISTERS))
    except Exception as err:  # noqa: BLE001 - best effort by design
        _LOGGER.debug("firmware string unavailable: %s", err)

    # The output-mode register is protocol-version specific (300 on protocol
    # 11, documented at 600 on protocols 3-6), so it is read from the MAP --
    # which means it can only be read once the protocol number is known.
    output_mode = OutputMode.UNKNOWN
    register_map = PLATFORMS.get(protocol_number)
    if register_map is not None and register_map.topology_register is not None:
        try:
            raw = await link.read_registers(register_map.topology_register, 1)
            output_mode = OutputMode.from_raw(raw[0])
        except Exception as err:  # noqa: BLE001 - best effort, like firmware
            _LOGGER.debug("output mode unavailable: %s", err)

    identity = DeviceIdentity(
        protocol_number=protocol_number,
        device_type=device_type,
        serial=serial,
        firmware=firmware,
        output_mode=output_mode,
    )
    if firmware and f"{device_type:04x}" != firmware[:4]:
        # Not fatal, but worth surfacing: on every unit measured so far these
        # agree, so a mismatch means one of the two assumptions is wrong.
        _LOGGER.warning(
            "device type 0x%04x does not match firmware prefix %r",
            device_type,
            firmware[:4],
        )
    return identity


def resolve_map(identity: DeviceIdentity) -> RegisterMap:
    """Select the register map, or refuse.

    Refusing is deliberate. Decoding an unmeasured protocol with the wrong map
    yields readings that look real, and nothing downstream can tell.
    """
    register_map = PLATFORMS.get(identity.protocol_number)
    if register_map is None:
        raise UnknownPlatform(
            f"unrecognised protocol number {identity.protocol_number} "
            f"(device type 0x{identity.device_type:04x}, serial {identity.serial!r}). "
            f"Known: {sorted(PLATFORMS)}. Capture this device before decoding it."
        )
    return register_map
