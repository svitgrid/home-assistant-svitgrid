"""Wire transport: plan contiguous register ranges from the spec, then read
them over Solarman V5 (pysolarmanv5) or Modbus TCP (pymodbus). All blocking
socket I/O is run by the caller via hass.async_add_executor_job."""
from __future__ import annotations

import contextlib
import logging

from .decoder import RawRegisters
from .register_spec import RegisterSpec

_LOGGER = logging.getLogger(__name__)
MAX_RANGE = 100  # registers per read


def plan_ranges(spec: RegisterSpec) -> list[tuple[int, int, int, str]]:
    """Group reads into (unitId, startAddr, count, functionCode) ranges.

    A ``words==2`` read occupies ``address`` and ``address+1``.  Ranges are
    capped at ``MAX_RANGE`` registers and grouped per ``(unitId, functionCode)``
    bucket.  The result list is sorted.
    """
    # Collect (unitId, fc) -> set of addresses
    buckets: dict[tuple[int, str], set[int]] = {}
    for r in spec.reads:
        key = (r.unit_id, r.function_code)
        addrs = buckets.setdefault(key, set())
        addrs.add(r.address)
        if r.words == 2:
            addrs.add(r.address + 1)

    ranges: list[tuple[int, int, int, str]] = []
    for (unit_id, fc), addr_set in buckets.items():
        addrs = sorted(addr_set)
        start = prev = addrs[0]
        for a in addrs[1:]:
            # Start a new range if non-contiguous OR would exceed MAX_RANGE
            if a == prev + 1 and (a - start + 1) <= MAX_RANGE:
                prev = a
                continue
            ranges.append((unit_id, start, prev - start + 1, fc))
            start = prev = a
        ranges.append((unit_id, start, prev - start + 1, fc))

    ranges.sort()
    return ranges


async def read_raw(hass, spec: RegisterSpec, cfg: dict) -> RawRegisters:
    """Read all registers defined by *spec* and return a ``RawRegisters`` map.

    Dispatches to ``_read_solarman`` or ``_read_modbus`` depending on
    ``spec.protocol``, running the blocking I/O via
    ``hass.async_add_executor_job``.
    """
    ranges = plan_ranges(spec)
    if spec.protocol == "solarman_v5":
        return await hass.async_add_executor_job(_read_solarman, cfg, ranges)
    if spec.protocol == "modbus_tcp":
        return await hass.async_add_executor_job(_read_modbus, cfg, ranges)
    raise ValueError(f"unsupported protocol: {spec.protocol}")


def _read_solarman(cfg: dict, ranges: list[tuple[int, int, int, str]]) -> RawRegisters:
    """Open one Solarman V5 connection and read all *ranges*.

    Installed library: pysolarmanv5 3.0.6
    Constructor: PySolarmanV5(address, serial, **kwargs)
      kwargs: port, mb_slave_id, socket_timeout, auto_reconnect, verbose, …
    Read method: read_holding_registers(register_addr, quantity) → list[int]
    (positional names; keyword-argument style also accepted)
    """
    from pysolarmanv5 import PySolarmanV5  # lazy import — not needed by other modules

    out: RawRegisters = {}
    sm = PySolarmanV5(
        cfg["ip"],
        int(cfg["logger_serial"]),
        port=int(cfg.get("port", 8899)),
        mb_slave_id=int(cfg.get("slave_id", 1)),
        socket_timeout=8,
        auto_reconnect=False,
    )
    try:
        for unit_id, start, count, _fc in ranges:
            words = sm.read_holding_registers(register_addr=start, quantity=count)
            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(words):
                slot[start + i] = w
    finally:
        with contextlib.suppress(Exception):
            sm.disconnect()
    return out


def _read_modbus(cfg: dict, ranges: list[tuple[int, int, int, str]]) -> RawRegisters:
    """Open one Modbus TCP connection and read all *ranges*.

    Installed library: pymodbus 3.13.1
    Constructor: ModbusTcpClient(host, *, port=502, timeout=3, …)
    Read methods:
      read_holding_registers(address, *, count=1, device_id=1, …) → result
      read_input_registers(address, *, count=1, device_id=1, …) → result
    NOTE: pymodbus 3.x uses ``device_id`` (not ``slave`` or ``unit``).
    result.isError() → bool; result.registers → list[int]
    """
    from pymodbus.client import ModbusTcpClient  # lazy import

    out: RawRegisters = {}
    client = ModbusTcpClient(cfg["ip"], port=int(cfg.get("port", 502)), timeout=8)
    try:
        client.connect()
        for unit_id, start, count, fc in ranges:
            if fc == "FC04":
                rr = client.read_input_registers(start, count=count, device_id=unit_id)
            else:
                rr = client.read_holding_registers(start, count=count, device_id=unit_id)
            if rr.isError():
                _LOGGER.debug(
                    "modbus read error unit=%s addr=%s: %s", unit_id, start, rr
                )
                continue
            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(rr.registers):
                slot[start + i] = w
    finally:
        with contextlib.suppress(Exception):
            client.close()
    return out
