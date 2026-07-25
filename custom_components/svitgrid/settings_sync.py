"""Settings-sync gate: model ranges, FNV hash, upload decision logic.

Mostly a pure module — no HA imports, no I/O — except for
``read_config_registers``, which performs the actual chunked device read via
``harvest.transport`` and is the one impure/async function here.
"""

from __future__ import annotations

import asyncio
import logging
import time

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

    Raises:
        ValueError: if ``chunk_size`` is not positive. A zero/negative
            chunk_size would never advance ``offset`` and silently hang the
            HA event loop forever — a caller bug, not a device-read failure,
            so it's raised rather than swallowed into ``None``.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")

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


def _eligible_for_settings_sync(inv: dict) -> tuple[str, dict] | None:
    """Return ``(model_id, harvest_config)`` if this inverter should be
    settings-synced, else ``None``.

    Eligible = a direct native ``harvest_config`` (solarman_v5 or modbus_tcp)
    whose ``model_id`` has a known register range. Relay/preset inverters
    (HA-entity ``entity_map``, no ``harvest_config`` at all) and MQTT-only
    inverters are never eligible — they carry no register address space to
    mirror, so they must never POST.
    """
    hc = inv.get("harvest_config")
    if not hc:
        return None
    protocol = hc.get("protocol", "solarman_v5")
    if protocol not in ("solarman_v5", "modbus_tcp"):
        return None
    model_id = hc.get("model_id")
    if not model_id or config_range_for_model(model_id) is None:
        return None
    return model_id, hc


async def sync_inverter_once(
    hass,
    api_client,
    api_key: str,
    inverter_id: str,
    model_id: str,
    cfg: dict,
    cache: dict[str, tuple[int, int]],
    *,
    heartbeat_s: int = 1800,
    now_monotonic_fn=time.monotonic,
) -> bool:
    """One settings-sync cycle for a single already-eligible inverter.

    Read -> hash -> ``should_upload`` -> POST. ``cache`` maps
    ``inverter_id -> (last_hash, last_uploaded_monotonic)`` and is updated
    ONLY when the POST returns True (2xx) — a failed POST, or a cycle where
    ``should_upload`` said no, leaves the cache untouched so the next cycle
    re-evaluates from the same last-known-good state (bootstrap/changed/
    heartbeat clauses all still apply next time).

    Returns True iff a POST was sent and accepted.
    """
    registers = await read_config_registers(hass, cfg, model_id)
    if registers is None:
        _LOGGER.debug(
            "settings_sync: register read failed for inverter %s (model %s), skipping",
            inverter_id,
            model_id,
        )
        return False

    config_range = config_range_for_model(model_id)
    if config_range is None:  # pragma: no cover — caller already filtered this
        return False
    start, _count = config_range

    new_hash = registers_hash(registers)
    cached_hash, last_uploaded = cache.get(inverter_id, (0, 0))
    now = now_monotonic_fn()

    if not should_upload(new_hash, cached_hash, last_uploaded, now, heartbeat_s):
        return False

    ok = await api_client.sync_settings(
        api_key=api_key,
        inverter_id=inverter_id,
        model_id=model_id,
        start_register=start,
        registers=registers,
    )
    if ok:
        cache[inverter_id] = (new_hash, now)
    else:
        _LOGGER.warning(
            "settings_sync: POST failed/rejected for inverter %s; cache left "
            "untouched, will retry next cycle",
            inverter_id,
        )
    return bool(ok)


async def settings_sync_tick(
    hass,
    api_client,
    api_key: str,
    inverters: list[dict],
    cache: dict[str, tuple[int, int]],
    *,
    heartbeat_s: int = 1800,
    now_monotonic_fn=time.monotonic,
) -> None:
    """One pass over every inverter on an entry: sync each eligible one.

    Ineligible inverters (relay/preset — no ``harvest_config`` — or a
    ``harvest_config`` whose model has no ``CONFIG_RANGES`` entry) are
    skipped with zero reads and zero POSTs; this is the enforcement point for
    "preset/MQTT-only inverters must never POST". A per-inverter exception is
    caught and logged so one bad inverter can't stall the rest of the tick.
    """
    for inv in inverters:
        eligible = _eligible_for_settings_sync(inv)
        if eligible is None:
            continue
        model_id, cfg = eligible
        inverter_id = inv.get("inverter_id")
        if not inverter_id:
            continue
        try:
            await sync_inverter_once(
                hass,
                api_client,
                api_key,
                inverter_id,
                model_id,
                cfg,
                cache,
                heartbeat_s=heartbeat_s,
                now_monotonic_fn=now_monotonic_fn,
            )
        except Exception:  # noqa: BLE001 — one inverter's failure must not block others
            _LOGGER.exception("settings_sync: tick failed for inverter %s", inverter_id)


async def settings_sync_loop(
    hass,
    entry,
    *,
    lifecycle=None,
    interval_s: int = 300,
    heartbeat_s: int = 1800,
    api_client=None,
    sleep_fn=asyncio.sleep,
    now_monotonic_fn=time.monotonic,
) -> None:
    """Every ``interval_s`` (default 300s / 5 min), settings-sync every
    register-capable inverter on this config entry.

    Lifecycle: exits when ``hass.is_stopping`` is True or (when provided)
    ``lifecycle.active`` becomes False — same contract as the other
    long-running loops (readings publisher, direct-harvest, command poller).

    ``api_client`` defaults to a fresh ``SvitgridApiClient`` sharing hass's
    shared aiohttp session (same construction pattern used elsewhere in
    ``__init__.py``); pass one explicitly in tests. Re-reads
    ``entry.data.get("inverters")`` every tick so a live options/entry update
    (add/remove/edit inverter, switch read source) takes effect on the next
    cycle without a reload. The per-inverter hash/heartbeat cache lives only
    for the lifetime of this loop instance — a reload/restart resets it,
    which just re-triggers each inverter's bootstrap clause once more on the
    next tick (one harmless extra POST, not a correctness issue).
    """
    if api_client is None:
        from homeassistant.helpers import aiohttp_client

        from .api_client import SvitgridApiClient

        session = aiohttp_client.async_get_clientsession(hass)
        api_client = SvitgridApiClient(session, api_base=entry.data["api_base"])

    api_key = entry.data["api_key"]
    cache: dict[str, tuple[int, int]] = {}

    _LOGGER.info("Settings-sync loop started for entry %s", entry.entry_id)
    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        inverters = entry.data.get("inverters") or []
        await settings_sync_tick(
            hass,
            api_client,
            api_key,
            inverters,
            cache,
            heartbeat_s=heartbeat_s,
            now_monotonic_fn=now_monotonic_fn,
        )
        await sleep_fn(interval_s)
    _LOGGER.info("Settings-sync loop stopped for entry %s", entry.entry_id)
