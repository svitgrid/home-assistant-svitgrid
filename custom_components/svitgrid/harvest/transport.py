"""Wire transport: plan contiguous register ranges from the spec, then read
them over Solarman V5 (pysolarmanv5) or Modbus TCP (pymodbus). All blocking
socket I/O is run by the caller via hass.async_add_executor_job."""

from __future__ import annotations

import contextlib
import logging
import time

from .decoder import RawRegisters
from .device_lock import logger_lock
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
    ``hass.async_add_executor_job``. Holds the logger's lock (see
    ``device_lock``) for the whole connection.
    """
    ranges = plan_ranges(spec)
    if spec.protocol == "solarman_v5":
        async with logger_lock(cfg):
            return await hass.async_add_executor_job(_read_solarman, cfg, ranges)
    if spec.protocol == "modbus_tcp":
        async with logger_lock(cfg):
            return await hass.async_add_executor_job(_read_modbus, cfg, ranges)
    _LOGGER.error("unsupported harvest protocol: %s", spec.protocol)
    raise ValueError(f"unsupported protocol: {spec.protocol}")


_SOLARMAN_CONNECT_ATTEMPTS = 3
_SOLARMAN_CONNECT_BACKOFF = 0.5  # seconds between connection-setup retries
# Per-range read timeout. Healthy Solarman reads return in ~0.2s, so a short
# timeout doesn't affect them — but on a flaky logger it lets a failing range
# bail fast (vs. an 8s stall) so the whole poll finishes in time to honour a
# short harvest cadence instead of grinding for minutes.
_SOLARMAN_SOCKET_TIMEOUT_S = 3


def _solarman_reader_for(sm, fc: str):
    """Return the pysolarmanv5 read method for *fc*.

    Raises RuntimeError for anything we cannot serve. That is deliberate: the
    previous behaviour — log at debug and read holding registers anyway — turned
    an unsupported function code into a plausible value read from the wrong
    bank, which is undetectable from the data.
    """
    method = {"FC03": "read_holding_registers", "FC04": "read_input_registers"}.get(fc)
    if method is None:
        raise RuntimeError(
            f"solarman: unsupported function code {fc!r} — refusing to read the wrong "
            f"register bank. Only FC03 (holding) and FC04 (input) are implemented."
        )
    reader = getattr(sm, method, None)
    if reader is None:
        raise RuntimeError(
            f"solarman: pysolarmanv5 has no {method}() — cannot serve {fc}. "
            f"Upgrade pysolarmanv5 (>=3.0 provides it)."
        )
    return reader


def _read_solarman(cfg: dict, ranges: list[tuple[int, int, int, str]]) -> RawRegisters:
    """Open one Solarman V5 connection and read all *ranges*.

    Installed library: pysolarmanv5 3.0.6
    Constructor: PySolarmanV5(address, serial, **kwargs)
      kwargs: port, mb_slave_id, socket_timeout, auto_reconnect, verbose, …
    Read method: read_holding_registers(register_addr, quantity) → list[int]
    (positional names; keyword-argument style also accepted)

    Resilient against flaky loggers:
    - Connection setup is retried up to _SOLARMAN_CONNECT_ATTEMPTS times with
      short backoff, catching connection-class errors (OSError, ConnectionError,
      BrokenPipeError, pysolarmanv5.NoSocketAvailableError when available).
    - Each range read is retried once (with a fresh client) on any exception.
      If the retry also fails, the range is skipped; partial results accumulate.
    - ``auto_reconnect`` is off, as in ``_write_solarman``. The retry above
      does the reconnect. With it on, against a logger that closes after each
      reply, pysolarmanv5's reader thread reconnected and re-sent frames by
      itself and died with uncaught OSErrors (2026-10-15, mock logger).
    - Raises RuntimeError when zero registers were read across all ranges so
      the calling loop can still back off for a truly dead logger.
    """
    from pysolarmanv5 import PySolarmanV5  # lazy import — not needed by other modules

    # Import pysolarmanv5-specific connection error defensively; fall back to OSError.
    try:
        from pysolarmanv5 import (
            NoSocketAvailableError as _NoSocketAvailableError,  # type: ignore[attr-defined]
        )
    except (ImportError, AttributeError):
        _NoSocketAvailableError = OSError  # type: ignore[assignment,misc]

    _connect_errors = (OSError, ConnectionError, BrokenPipeError, _NoSocketAvailableError)

    def _make_client():
        return PySolarmanV5(
            cfg["ip"],
            int(cfg["logger_serial"]),
            port=int(cfg.get("port", 8899)),
            mb_slave_id=int(cfg.get("slave_id", 1)),
            socket_timeout=_SOLARMAN_SOCKET_TIMEOUT_S,
            auto_reconnect=False,
        )

    # --- Connection-setup retry ---
    sm = None
    last_err: Exception | None = None
    for attempt in range(_SOLARMAN_CONNECT_ATTEMPTS):
        try:
            sm = _make_client()
            break
        except _connect_errors as exc:
            last_err = exc
            _LOGGER.debug(
                "solarman: connect attempt %d/%d failed: %s",
                attempt + 1,
                _SOLARMAN_CONNECT_ATTEMPTS,
                exc,
            )
            if attempt < _SOLARMAN_CONNECT_ATTEMPTS - 1:
                time.sleep(_SOLARMAN_CONNECT_BACKOFF)

    if sm is None:
        raise RuntimeError(
            f"solarman: failed to connect after {_SOLARMAN_CONNECT_ATTEMPTS} attempts: {last_err}"
        )

    out: RawRegisters = {}
    try:
        for unit_id, start, count, fc in ranges:
            # FC03 = holding registers, FC04 = input registers. These are two
            # DIFFERENT register banks: reading one at the other's address
            # silently returns an unrelated number. 20 corpus models (16 Afore,
            # 3 KSTAR, solis_30k_5g) declare FC04 on 100% of their reads, and
            # pysolarmanv5 has served read_input_registers all along — until
            # 2026-08-17 this branch logged a debug line and issued FC03 anyway.
            reader = _solarman_reader_for(sm, fc)

            # --- Per-range read with one retry on any transient failure ---
            try:
                words = reader(register_addr=start, quantity=count)
            except Exception as exc:
                _LOGGER.debug(
                    "solarman: %s addr=%s count=%s read failed (%s); "
                    "reconnecting and retrying once",
                    fc,
                    start,
                    count,
                    exc,
                )
                with contextlib.suppress(Exception):
                    sm.disconnect()
                try:
                    sm = _make_client()
                    # Re-resolve against the NEW client, but on the SAME bank —
                    # a retry must never silently fall back to FC03.
                    words = _solarman_reader_for(sm, fc)(register_addr=start, quantity=count)
                except Exception as exc2:
                    # Dropping a whole range is data loss, not noise: on a
                    # 100%-FC04 model every range failing is what an inverter
                    # that cannot serve input registers looks like.
                    _LOGGER.warning(
                        "solarman: %s addr=%s count=%s retry failed (%s); skipping range — "
                        "these registers will be missing from this reading",
                        fc,
                        start,
                        count,
                        exc2,
                    )
                    continue

            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(words):
                slot[start + i] = w
    finally:
        with contextlib.suppress(Exception):
            sm.disconnect()

    if not out:
        raise RuntimeError("solarman: all ranges failed — logger unreachable or no registers read")
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
                _LOGGER.debug("modbus read error unit=%s addr=%s: %s", unit_id, start, rr)
                continue
            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(rr.registers):
                slot[start + i] = w
    finally:
        with contextlib.suppress(Exception):
            client.close()
    return out


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def read_word(
    hass,
    spec: RegisterSpec,
    cfg: dict,
    unit_id: int,
    address: int,
    function_code: str = "FC03",
) -> int | None:
    """Read a single register and return its value, or ``None`` on any error.

    Reuses the protocol-level read helpers so callers can use it for
    pre-write verify reads and bit-RMW prior-value reads.

    ``function_code`` defaults to FC03 because the write path (pre-write prior
    reads, post-write verify reads) always targets holding registers. The
    reachability probe passes the model's own function code instead — probing
    an FC04 model in the holding bank can reject setup with
    ``cannot_reach_inverter`` on an inverter that is perfectly reachable.
    """
    try:
        ranges = [(unit_id, address, 1, function_code)]
        if spec.protocol == "solarman_v5":
            async with logger_lock(cfg):
                raw = await hass.async_add_executor_job(_read_solarman, cfg, ranges)
        elif spec.protocol == "modbus_tcp":
            async with logger_lock(cfg):
                raw = await hass.async_add_executor_job(_read_modbus, cfg, ranges)
        else:
            return None
        return raw.get(unit_id, {}).get(address)
    except Exception:
        _LOGGER.debug("read_word error unit=%s addr=%s fc=%s", unit_id, address, function_code)
        return None


def _contiguous_ranges(
    unit_id: int, addresses: list[int], function_code: str
) -> list[tuple[int, int, int, str]]:
    """Group *addresses* into ``(unit, start, count, fc)`` ranges of at most
    ``MAX_RANGE`` registers."""
    addrs = sorted(set(addresses))
    if not addrs:
        return []
    ranges: list[tuple[int, int, int, str]] = []
    start = prev = addrs[0]
    for a in addrs[1:]:
        if a == prev + 1 and (a - start + 1) <= MAX_RANGE:
            prev = a
            continue
        ranges.append((unit_id, start, prev - start + 1, function_code))
        start = prev = a
    ranges.append((unit_id, start, prev - start + 1, function_code))
    return ranges


async def read_words(
    hass,
    spec: RegisterSpec,
    cfg: dict,
    unit_id: int,
    addresses: list[int],
) -> dict[int, int]:
    """Read holding registers *addresses* over one connection.

    Returns ``{address: value}`` for every register that was read. An address
    missing from the result was not read; on any error the result is empty.
    Contiguous addresses go out as one frame, so a Deye slot table (30
    registers) costs one request instead of 30 connections.
    """
    ranges = _contiguous_ranges(unit_id, list(addresses), "FC03")
    if not ranges:
        return {}
    try:
        if spec.protocol == "solarman_v5":
            raw = await hass.async_add_executor_job(_read_solarman, cfg, ranges)
        elif spec.protocol == "modbus_tcp":
            raw = await hass.async_add_executor_job(_read_modbus, cfg, ranges)
        else:
            return {}
    except Exception:
        _LOGGER.debug("read_words error unit=%s addrs=%s", unit_id, sorted(addresses))
        return {}
    got = raw.get(unit_id, {})
    return {a: got[a] for a in addresses if a in got}


async def write_registers(
    hass,
    spec: RegisterSpec,
    cfg: dict,
    writes: list[tuple[int, int, int]],
) -> None:
    """Write one or more holding registers.

    *writes* is a list of ``(unit_id, address, value)`` tuples.  All writes
    are dispatched in a single blocking call via
    ``hass.async_add_executor_job``.
    """
    if spec.protocol == "solarman_v5":
        async with logger_lock(cfg):
            await hass.async_add_executor_job(_write_solarman, cfg, writes)
    elif spec.protocol == "modbus_tcp":
        async with logger_lock(cfg):
            await hass.async_add_executor_job(_write_modbus, cfg, writes)
    else:
        raise ValueError(f"unsupported protocol: {spec.protocol}")


def _write_solarman(cfg: dict, writes: list[tuple[int, int, int]]) -> None:
    """Write every *(unit_id, address, value)* tuple over Solarman V5.

    Each register goes out as its own FC16 (write multiple registers) frame
    with one value: ``write_multiple_holding_registers(register_addr,
    values)``. Deye loggers refuse FC06, which is what pysolarmanv5's
    ``write_holding_register`` sends, so the edge firmware
    (``solarman_v5_build_write_frame``) and the phone harvester
    (``BatteryChargeExecutor``) write a Deye the same way. A whole slot program
    is not one contiguous block anyway (starts, powers and SOCs are separate
    six-register runs), and per-register frames keep the recipe's write order.

    Some loggers close the TCP connection after every reply. A write that
    finds the connection gone is retried once on a fresh client, as a failed
    read range is in ``_read_solarman``; a logger that keeps the connection
    takes every write on one. ``auto_reconnect`` stays off here: against a
    closing logger, pysolarmanv5's reader thread re-sends the last frame on
    its own and dies with an uncaught ``BrokenPipeError`` (seen against the
    mock logger), and the explicit retry already covers the reconnect.
    Writing the same value twice is harmless. A write that fails on the retry
    too raises, and the registers after it are not written.

    ``mb_slave_id`` is fixed at connection time; *unit_id* is ignored here
    because Solarman V5 carries the slave in the frame the client builds.
    """
    from pysolarmanv5 import PySolarmanV5  # lazy import

    def _make_client():
        return PySolarmanV5(
            cfg["ip"],
            int(cfg["logger_serial"]),
            port=int(cfg.get("port", 8899)),
            mb_slave_id=int(cfg.get("slave_id", 1)),
            socket_timeout=8,
            auto_reconnect=False,
        )

    sm = _make_client()
    try:
        for _unit_id, address, value in writes:
            try:
                sm.write_multiple_holding_registers(register_addr=address, values=[value])
            except Exception as exc:
                _LOGGER.debug(
                    "solarman: write addr=%s failed (%s); reconnecting and retrying once",
                    address,
                    exc,
                )
                with contextlib.suppress(Exception):
                    sm.disconnect()
                sm = _make_client()
                sm.write_multiple_holding_registers(register_addr=address, values=[value])
    finally:
        with contextlib.suppress(Exception):
            sm.disconnect()


def _write_modbus(cfg: dict, writes: list[tuple[int, int, int]]) -> None:
    """Open one Modbus TCP connection and write all *(unit_id, address, value)* tuples.

    pymodbus 3.x write method:
      write_registers(address, values, *, device_id) → result
    result.isError() → bool
    """
    from pymodbus.client import ModbusTcpClient  # lazy import

    client = ModbusTcpClient(cfg["ip"], port=int(cfg.get("port", 502)), timeout=8)
    try:
        client.connect()
        for unit_id, address, value in writes:
            result = client.write_registers(address, [value], device_id=unit_id)
            if result.isError():
                raise RuntimeError(f"modbus write error unit={unit_id} addr={address}: {result}")
    finally:
        with contextlib.suppress(Exception):
            client.close()
