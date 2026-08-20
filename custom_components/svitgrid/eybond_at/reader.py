"""One poll cycle: identify the device, then read and decode its map.

Identity is read once and cached, because four identity reads at 9600 baud
cost about a second and would halve the useful poll rate. `invalidate()`
clears it -- call that whenever the collector reconnects, since a customer can
swap an inverter without telling us.

Two refusals are deliberate:

* **An unknown protocol number raises before any telemetry is read.** A wrong
  register map produces plausible numbers, and nothing downstream can tell.
  Publishing nothing is recoverable.
* **A failed block marks the reading incomplete rather than filling zeros.**
  Absent and zero are very different, and the difference is invisible
  downstream unless it is carried explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .identity import DeviceIdentity, ReadsRegisters, identify, resolve_map
from .link import TransactionFailed
from .register_map import Confidence, RegisterMap

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    identity: DeviceIdentity
    register_map: RegisterMap
    values: dict[str, float]
    confidence: dict[str, Confidence]
    missing_blocks: tuple[tuple[int, int], ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_blocks


@dataclass
class EybondAtReader:
    link: ReadsRegisters
    _identity: DeviceIdentity | None = field(default=None, init=False)
    _map: RegisterMap | None = field(default=None, init=False)

    def invalidate(self) -> None:
        """Forget the cached identity. Call on every collector reconnect."""
        self._identity = None
        self._map = None

    async def ensure_identified(self) -> tuple[DeviceIdentity, RegisterMap]:
        if self._identity is None or self._map is None:
            identity = await identify(self.link)
            # resolve_map raises UnknownPlatform BEFORE any telemetry read, so
            # an unrecognised device costs one identity round trip and nothing
            # more.
            register_map = resolve_map(identity)
            self._identity, self._map = identity, register_map
            _LOGGER.info(
                "identified %s: protocol %d, serial %s, firmware %s",
                register_map.name,
                identity.protocol_number,
                identity.serial,
                identity.firmware or "unknown",
            )
        return self._identity, self._map

    async def read(self) -> Reading:
        identity, register_map = await self.ensure_identified()
        values: dict[str, float] = {}
        missing: list[tuple[int, int]] = []
        for address, count in register_map.read_plan():
            try:
                words = await self.link.read_registers(address, count)
            except TransactionFailed as err:
                # One bad block does not invalidate the others, but it must be
                # visible: see the module docstring.
                _LOGGER.warning("block %d..%d unreadable: %s", address, address + count - 1, err)
                missing.append((address, count))
                continue
            values.update(register_map.decode_block(address, words))
        confidence = {
            spec.field: spec.confidence for spec in register_map.fields if spec.field in values
        }
        return Reading(
            identity=identity,
            register_map=register_map,
            values=values,
            confidence=confidence,
            missing_blocks=tuple(missing),
        )
