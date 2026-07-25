"""Settings-sync gate: model ranges, FNV hash, upload decision logic.

Mostly a pure module — no HA imports, no I/O — except for
``read_config_registers``, which performs the actual chunked device read via
``harvest.transport`` and is the one impure/async function here.
"""

from __future__ import annotations

import logging

from .harvest import transport

_LOGGER = logging.getLogger(__name__)

# Config register ranges: model family → (start, count)
# 3-phase and 1-phase models share the same register count (63 each)
CONFIG_RANGES = {
    # 3-phase models: registers 115-177 (63 registers)
    'deye_sg04lp3': (115, 63),
    'deye_sg01hp3': (115, 63),
    'deye_sg01hp3_50k': (115, 63),
    'deye_sg01hp3_30k': (115, 63),
    'deye_sg02hp3_80k': (115, 63),
    'deye_sg05lp3': (115, 63),
    'deye_gb_s20k': (115, 63),
    'sunsynk_3phase': (115, 63),
    'sunsynk_3phase_15k': (115, 63),
    # 1-phase models: registers 217-279 (63 registers)
    'deye_sg03lp1': (217, 63),
    'deye_sg01lp1': (217, 63),
    'deye_sg02lp1': (217, 63),
    'deye_sun_g3': (217, 63),
    'deye_sg04lp1': (217, 63),
    'deye_sg05lp1': (217, 63),
    'deye_sg01lp1_16k': (217, 63),
    'sunsynk_1phase': (217, 63),
    'sunsynk_1phase_12k': (217, 63),
    'sunsynk_1phase_16k': (217, 63),
    'deye_sg01lp1_us': (217, 63),
    'solark_5k': (217, 63),
    'solark_12k': (217, 63),
    'solark_15k': (217, 63),
    'solark_18k': (217, 63),
}


def config_range_for_model(model_id: str) -> tuple[int, int] | None:
    """Return (start, count) register range for a model, or None if unsupported."""
    return CONFIG_RANGES.get(model_id)


def registers_hash(regs: list[int]) -> int:
    """FNV-1a hash of register list (byte-folded).

    Identical to firmware `settings_registers_hash` and server algorithm.
    Each 16-bit register is folded into low byte then high byte.
    Uses 32-bit unsigned integer arithmetic throughout.
    """
    h = 2166136261
    for v in regs:
        # Low byte: XOR and multiply, then mask to 32-bit unsigned
        h = ((h ^ (v & 0xFF)) * 16777619) & 0xFFFFFFFF

        # High byte: XOR and multiply, then mask to 32-bit unsigned
        h = ((h ^ ((v >> 8) & 0xFF)) * 16777619) & 0xFFFFFFFF
    return h


def should_upload(
    new_hash: int,
    cached_hash: int,
    last_uploaded_monotonic: int,
    now_monotonic: int,
    heartbeat_s: int = 1800,
) -> bool:
    """Determine if config registers should be uploaded to the server.

    Four clauses (OR):
    1. Bootstrap: last_uploaded_monotonic == 0 (never uploaded)
    2. Changed: new_hash != cached_hash (register state changed)
    3. Clock skew (fail-safe): now_monotonic < last_uploaded_monotonic (system clock went backward)
    4. Heartbeat: now_monotonic >= last_uploaded_monotonic + heartbeat_s (periodic refresh)
    """
    return (
        last_uploaded_monotonic == 0  # Bootstrap (never uploaded)
        or new_hash != cached_hash  # Changed
        or now_monotonic < last_uploaded_monotonic  # Clock skew (fail-safe)
        or now_monotonic >= last_uploaded_monotonic + heartbeat_s  # Heartbeat
    )


async def read_config_registers(
    hass, cfg: dict, model_id: str, chunk_size: int = 25
) -> list[int] | None:
    """Read a model's config-register block in small chunks and stitch them.

    Some new-gen Deye loggers cap single-frame batch reads; the config block
    (63 registers) is small enough for readings-style 1-12-register batches
    to work reliably, but a single 63-register frame can fail forever on
    those loggers. So this reads the block in ``chunk_size``-sized pieces
    and concatenates them in address order.

    Guard: ANY chunk failure or short read (fewer registers than requested)
    aborts the whole read and returns ``None`` — never a partial list. A
    partial config once wiped a customer's TOU configuration, so a clean
    failure here is strictly preferable to guessing with incomplete data.

    Dispatches directly to the low-level ``transport._read_solarman`` /
    ``transport._read_modbus`` helpers (the same ones ``transport.read_raw``
    uses) via ``hass.async_add_executor_job``, since we already have an
    explicit ``(start, count)`` range and don't need a full ``RegisterSpec``.

    Args:
        hass: Home Assistant instance, used to run the blocking transport
            call off the event loop.
        cfg: Transport config dict — same shape consumed elsewhere in
            harvest/ (``ip``, ``port``, ``logger_serial``, ``slave_id``,
            ``protocol``).
        model_id: Inverter model id, looked up in ``CONFIG_RANGES``.
        chunk_size: Max registers requested per transport call (default 25).

    Returns:
        The full list of ``count`` registers in address order, or ``None``
        if the model is unsupported, any chunk read raised, or any chunk
        returned fewer registers than requested.
    """
    config_range = config_range_for_model(model_id)
    if config_range is None:
        _LOGGER.debug("read_config_registers: unsupported model %s", model_id)
        return None
    start, count = config_range

    protocol = cfg.get("protocol", "solarman_v5")
    if protocol == "solarman_v5":
        read_fn = transport._read_solarman
    elif protocol == "modbus_tcp":
        read_fn = transport._read_modbus
    else:
        _LOGGER.debug("read_config_registers: unsupported protocol %s", protocol)
        return None

    unit_id = int(cfg.get("slave_id", 1))

    registers: list[int] = []
    offset = 0
    while offset < count:
        chunk_count = min(chunk_size, count - offset)
        chunk_start = start + offset
        ranges = [(unit_id, chunk_start, chunk_count, "FC03")]

        try:
            raw = await hass.async_add_executor_job(read_fn, cfg, ranges)
        except Exception as exc:  # noqa: BLE001 — any transport failure aborts the read
            _LOGGER.debug(
                "read_config_registers: chunk read failed at addr=%s count=%s: %s",
                chunk_start,
                chunk_count,
                exc,
            )
            return None

        slot = raw.get(unit_id, {}) if raw else {}
        chunk_values: list[int] = []
        for addr in range(chunk_start, chunk_start + chunk_count):
            if addr not in slot:
                _LOGGER.debug(
                    "read_config_registers: short chunk at addr=%s count=%s "
                    "(missing register %s) — aborting, no partial result",
                    chunk_start,
                    chunk_count,
                    addr,
                )
                return None
            chunk_values.append(slot[addr])

        registers.extend(chunk_values)
        offset += chunk_count

    return registers
