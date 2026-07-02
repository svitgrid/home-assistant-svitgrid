"""Wire transport: plan contiguous register ranges from the spec, then read
them over Solarman V5 (pysolarmanv5) or Modbus TCP (pymodbus). All blocking
socket I/O is run by the caller via hass.async_add_executor_job."""
from __future__ import annotations

import contextlib
import logging
import time

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
    _LOGGER.error("unsupported harvest protocol: %s", spec.protocol)
    raise ValueError(f"unsupported protocol: {spec.protocol}")


_SOLARMAN_CONNECT_ATTEMPTS = 3
_SOLARMAN_CONNECT_BACKOFF = 0.5  # seconds between connection-setup retries
# Per-range read timeout. Healthy Solarman reads return in ~0.2s, so a short
# timeout doesn't affect them — but on a flaky logger it lets a failing range
# bail fast (vs. an 8s stall) so the whole poll finishes in time to honour a
# short harvest cadence instead of grinding for minutes.
_SOLARMAN_SOCKET_TIMEOUT_S = 3


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
    - Raises RuntimeError when zero registers were read across all ranges so
      the calling loop can still back off for a truly dead logger.
    """
    from pysolarmanv5 import PySolarmanV5  # lazy import — not needed by other modules

    # Import pysolarmanv5-specific connection error defensively; fall back to OSError.
    try:
        from pysolarmanv5 import NoSocketAvailableError as _NoSocketAvailableError  # type: ignore[attr-defined]
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
            auto_reconnect=True,
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
            if fc != "FC03":
                _LOGGER.debug(
                    "solarman: ignoring %s range at addr=%s (Solarman V5 reads holding registers only)",
                    fc,
                    start,
                )
            # --- Per-range read with one retry on any transient failure ---
            try:
                words = sm.read_holding_registers(register_addr=start, quantity=count)
            except Exception as exc:
                _LOGGER.debug(
                    "solarman: addr=%s count=%s read failed (%s); reconnecting and retrying once",
                    start,
                    count,
                    exc,
                )
                with contextlib.suppress(Exception):
                    sm.disconnect()
                try:
                    sm = _make_client()
                    words = sm.read_holding_registers(register_addr=start, quantity=count)
                except Exception as exc2:
                    _LOGGER.debug(
                        "solarman: addr=%s count=%s retry failed (%s); skipping range",
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
        raise RuntimeError(
            "solarman: all ranges failed — logger unreachable or no registers read"
        )
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


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

async def read_word(hass, spec: RegisterSpec, cfg: dict, unit_id: int, address: int) -> int | None:
    """Read a single register and return its value, or ``None`` on any error.

    Reuses the protocol-level read helpers so callers can use it for
    pre-write verify reads and bit-RMW prior-value reads.
    """
    try:
        if spec.protocol == "solarman_v5":
            raw = await hass.async_add_executor_job(
                _read_solarman, cfg, [(unit_id, address, 1, "FC03")]
            )
        elif spec.protocol == "modbus_tcp":
            raw = await hass.async_add_executor_job(
                _read_modbus, cfg, [(unit_id, address, 1, "FC03")]
            )
        else:
            return None
        return raw.get(unit_id, {}).get(address)
    except Exception:
        _LOGGER.debug("read_word error unit=%s addr=%s", unit_id, address)
        return None


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
        await hass.async_add_executor_job(_write_solarman, cfg, writes)
    elif spec.protocol == "modbus_tcp":
        await hass.async_add_executor_job(_write_modbus, cfg, writes)
    else:
        raise ValueError(f"unsupported protocol: {spec.protocol}")


def _write_solarman(cfg: dict, writes: list[tuple[int, int, int]]) -> None:
    """Open one Solarman V5 connection and write all *(unit_id, address, value)* tuples.

    pysolarmanv5 write method:
      write_holding_registers(register_addr, values) → None
    (``mb_slave_id`` is fixed at connection time; unit_id is ignored here
    because Solarman V5 encodes the slave in the protocol header.)
    """
    from pysolarmanv5 import PySolarmanV5  # lazy import

    sm = PySolarmanV5(
        cfg["ip"],
        int(cfg["logger_serial"]),
        port=int(cfg.get("port", 8899)),
        mb_slave_id=int(cfg.get("slave_id", 1)),
        socket_timeout=8,
        auto_reconnect=False,
    )
    try:
        for _unit_id, address, value in writes:
            sm.write_holding_registers(register_addr=address, values=[value])
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
                raise RuntimeError(
                    f"modbus write error unit={unit_id} addr={address}: {result}"
                )
    finally:
        with contextlib.suppress(Exception):
            client.close()
