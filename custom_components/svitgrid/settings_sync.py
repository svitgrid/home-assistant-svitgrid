"""Settings-sync gate: FNV hash, upload decision logic, and the settings reads.

Which models have settings, and which registers hold them, lives in
``settings_read_plan``. The reads here (``read_config_registers``,
``read_holding_blocks``, ``read_settings_snapshot``) are the impure/async
part: they reach the device through ``harvest.transport``, or for an SMG II
inverter through the EyBond collector's session.

Only the Deye-family read is POSTed to the cloud. Every other kind is read
and cached for the island settings view, and never leaves the house from
here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from .api_client import SettingsSyncRejected
from .const import DOMAIN
from .harvest import transport
from .harvest.device_lock import logger_lock
from .settings_read_plan import (
    SettingsReadPlan,
    build_snapshot_body,
    config_range_for_model,
    settings_read_plan_for,
    settings_snapshot_kind_for,
)

_LOGGER = logging.getLogger(__name__)

# Until an inverter's settings are read once, a failed read is retried after
# these delays instead of the full interval. The app lands the owner on the
# settings page right after pairing; four retries fit inside 65 s. After the
# last one the loop keeps its regular interval, so a logger that cannot serve
# the config block is not polled every 30 s for ever.
_FIRST_READ_RETRY_S = (5, 10, 20, 30)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cloud_ingest_enabled(entry) -> bool:
    """The entry's cloud-ingest flag, read the same way setup reads it."""
    options = getattr(entry, "options", None) or {}
    return bool(entry.data.get("cloud_ingest_enabled", options.get("cloud_ingest_enabled", True)))


def find_settings_snapshot(hass, inverter_id: str) -> dict | None:
    """The last successful settings read of ``inverter_id`` across config entries."""
    for state in (getattr(hass, "data", {}).get(DOMAIN) or {}).values():
        if not isinstance(state, dict):
            continue
        snapshot = (state.get("settings_snapshots") or {}).get(inverter_id)
        if snapshot is not None:
            return snapshot
    return None


def find_known_inverter(hass, inverter_id: str) -> dict | None:
    """The inverter ``inverter_id`` as some config entry lists it, or None."""
    for state in (getattr(hass, "data", {}).get(DOMAIN) or {}).values():
        if not isinstance(state, dict):
            continue
        for inv in (state.get("entry_data") or {}).get("inverters") or []:
            if inv.get("inverter_id") == inverter_id:
                return inv
    return None


def known_inverter_model_id(inv: dict) -> str | None:
    """The model id of an entry's inverter: its harvest config's, else its own."""
    return (inv.get("harvest_config") or {}).get("model_id") or inv.get("model_id")


def missing_snapshot_error(hass, inverter_id: str) -> str:
    """Why the settings view has no snapshot for ``inverter_id``.

    ``unknown_inverter`` when no entry lists it; ``nothing_to_configure``
    when its model has no snapshot kind (the app's generic screen, and
    SolaX), so the app never waits for a read that cannot come; else
    ``not_read_yet``, which includes a PI30 inverter this add-on cannot read.
    """
    inv = find_known_inverter(hass, inverter_id)
    if inv is None:
        return "unknown_inverter"
    if settings_snapshot_kind_for(known_inverter_model_id(inv)) is None:
        return "nothing_to_configure"
    return "not_read_yet"


def settings_snapshot_response(snapshot: dict) -> dict:
    """The settings view's 200 body for a cached snapshot.

    A Deye snapshot (no ``kind``, as every row stored before other brands
    were read) answers ``{"inverterId", "modelId", "startRegister",
    "registers", "readAt"}``, unchanged. Any other kind answers
    ``{"inverterId", "modelId", "readAt"}`` plus its brand body, the keys the
    harvester would POST to the cloud's ``sync-settings`` route.
    """
    if snapshot.get("kind", "deye") == "deye":
        return {
            "inverterId": snapshot["inverter_id"],
            "modelId": snapshot["model_id"],
            "startRegister": snapshot["start_register"],
            "registers": list(snapshot["registers"]),
            "readAt": snapshot["read_at"],
        }
    return {
        "inverterId": snapshot["inverter_id"],
        "modelId": snapshot["model_id"],
        **snapshot["body"],
        "readAt": snapshot["read_at"],
    }


def _parse_read_at(value) -> datetime | None:
    """An ISO 8601 instant with a zone, or None. A naive time is refused:
    nothing says which clock it was read on."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


async def seed_onboarding_settings(
    hass,
    inverter_id: str,
    *,
    model_id,
    start_register,
    registers,
    read_at,
) -> str | None:
    """Seed the settings the app read while onboarding identified the inverter.

    An island station with cloud sync off cannot receive them through the API,
    so the app hands them to the add-on, and the settings view serves them
    until the settings loop's own first read replaces them.

    Returns None once seeded, or why not: ``unknown_inverter`` (no entry
    harvests it natively), ``model_mismatch``, ``bad_range`` (not the model's
    config block), ``bad_registers``, ``bad_read_at``, or ``already_read``.
    The loop reads after setup, so a read it already made is fresher than the
    phone's and is never replaced.
    """
    state = None
    harvested_model = None
    for candidate in (getattr(hass, "data", {}).get(DOMAIN) or {}).values():
        if not isinstance(candidate, dict):
            continue
        for inv in (candidate.get("entry_data") or {}).get("inverters") or []:
            if inv.get("inverter_id") != inverter_id:
                continue
            eligible = _eligible_for_settings_sync(inv)
            if eligible is not None:
                state, harvested_model = candidate, eligible[0]
                break
        if state is not None:
            break
    if state is None:
        return "unknown_inverter"
    if model_id != harvested_model:
        return "model_mismatch"

    # A partial or mis-aligned block maps to zeroed settings; the server
    # refuses one for the same reason.
    expected = config_range_for_model(harvested_model)
    if (
        expected is None
        or not isinstance(registers, list)
        or start_register != expected[0]
        or len(registers) != expected[1]
    ):
        return "bad_range"
    if not all(type(v) is int and 0 <= v <= 0xFFFF for v in registers):
        return "bad_registers"
    parsed = _parse_read_at(read_at)
    if parsed is None:
        return "bad_read_at"

    snapshots = state.setdefault("settings_snapshots", {})
    if inverter_id in snapshots:
        return "already_read"
    snapshot = {
        "inverter_id": inverter_id,
        "model_id": harvested_model,
        "start_register": expected[0],
        "registers": list(registers),
        "read_at": parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    snapshots[inverter_id] = snapshot
    store = state.get("store")
    if store is not None:
        try:
            await store.set_settings_snapshot(snapshot)
        except Exception:  # noqa: BLE001 — the in-memory cache still serves
            _LOGGER.exception(
                "settings_sync: could not persist the onboarding read of %s", inverter_id
            )
    _LOGGER.info("settings_sync: seeded the onboarding read of inverter %s", inverter_id)
    return None


# How long a settings-sync-rejected (4xx) inverter is skipped before the
# next POST attempt. A 403 (unbound/evicted device api-key) or other
# validation rejection is permanent — the request would be rejected forever
# — so retrying every 5-min tick just hammers the server. See item 4,
# 2026-07-25 final review ("permanent 403 loop for unbound devices").
SETTINGS_SYNC_BACKOFF_S = 6 * 60 * 60  # 6 hours


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
    hass,
    cfg: dict,
    model_id: str,
    chunk_size: int = 25,
    *,
    errors_out: list[str] | None = None,
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

    All chunk ranges are passed to the transport in a SINGLE call — one TCP
    connection, not one per chunk. ``transport._read_solarman`` /
    ``transport._read_modbus`` already accept a list of ``(unit_id, start,
    count, fc)`` ranges and read every one of them inside one connection
    (this is exactly what ``transport.read_raw``'s ``plan_ranges`` output
    relies on), so splitting the 63-register config block into
    ``chunk_size``-sized pieces only needs to change how many registers are
    requested per range — not how many connections are opened. The
    full-batch-or-none guard is unchanged: every address in the requested
    range must be present in the response, or the whole read is discarded.

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
        errors_out: When given, the reason a read failed is appended to it,
            so the caller can log it at the level it chooses.

    The transport call holds the logger's lock (``harvest.device_lock``), so
    it never opens a connection while the harvest loop has one open.

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
        if errors_out is not None:
            errors_out.append(f"unsupported model {model_id}")
        return None
    start, count = config_range

    words = await read_holding_blocks(
        hass, cfg, (config_range,), chunk_size=chunk_size, errors_out=errors_out
    )
    if words is None:
        return None
    return [words[addr] for addr in range(start, start + count)]


async def read_holding_blocks(
    hass,
    cfg: dict,
    blocks,
    *,
    optional=(),
    chunk_size: int = 25,
    errors_out: list[str] | None = None,
) -> dict[int, int] | None:
    """Read ``(start, count)`` holding-register blocks, all or nothing.

    The read ``read_config_registers`` describes, for any list of blocks:
    every block in ``chunk_size`` pieces, all in ONE transport call under the
    logger's lock, and ``None`` unless every address of every block answered.

    ``optional`` are single registers read in the same call whose absence
    does not fail the read (MUST's edition and pack words: the firmware
    posts its body without them).

    Returns the words keyed by address, optional ones only when they
    answered.
    """
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")

    def fail(reason: str) -> None:
        if errors_out is not None:
            errors_out.append(reason)

    protocol = cfg.get("protocol", "solarman_v5")
    if protocol == "solarman_v5":
        read_fn = transport._read_solarman
    elif protocol == "modbus_tcp":
        read_fn = transport._read_modbus
    else:
        _LOGGER.debug("read_holding_blocks: unsupported protocol %s", protocol)
        fail(f"unsupported protocol {protocol}")
        return None

    unit_id = int(cfg.get("slave_id", 1))

    # Build every chunk's range up front and read them all in ONE transport
    # call (one TCP connection) — see read_config_registers' docstring.
    ranges: list[tuple[int, int, int, str]] = []
    for start, count in blocks:
        offset = 0
        while offset < count:
            chunk_count = min(chunk_size, count - offset)
            ranges.append((unit_id, start + offset, chunk_count, "FC03"))
            offset += chunk_count
    for address in optional:
        ranges.append((unit_id, address, 1, "FC03"))

    try:
        async with logger_lock(cfg):
            raw = await hass.async_add_executor_job(read_fn, cfg, ranges)
    except Exception as exc:  # noqa: BLE001 — any transport failure aborts the read
        _LOGGER.debug("read_holding_blocks: batched read of %s failed: %s", list(blocks), exc)
        fail(str(exc) or type(exc).__name__)
        return None

    slot = raw.get(unit_id, {}) if raw else {}
    words: dict[int, int] = {}
    for start, count in blocks:
        for addr in range(start, start + count):
            if addr not in slot:
                _LOGGER.debug(
                    "read_holding_blocks: short/incomplete batched read at addr=%s "
                    "count=%s (missing register %s) — aborting, no partial result",
                    start,
                    count,
                    addr,
                )
                fail(f"incomplete read, register {addr} missing")
                return None
            words[addr] = slot[addr]
    for address in optional:
        if address in slot:
            words[address] = slot[address]
    return words


# The EyBond collector transport (`eybond_at.setup.EYBOND_PROTOCOL`), spelled
# out so this module does not import the collector stack.
_EYBOND_PROTOCOL = "eybond_at"


def _is_eybond(cfg: dict) -> bool:
    return cfg.get("protocol") == _EYBOND_PROTOCOL


def _eligible_for_settings_sync(inv: dict) -> tuple[str, dict] | None:
    """Return ``(model_id, harvest_config)`` if this inverter should be
    settings-synced, else ``None``.

    Eligible = a direct native ``harvest_config`` (solarman_v5, modbus_tcp,
    or an EyBond collector) whose ``model_id`` has a settings read plan
    (``settings_read_plan_for``). Relay/preset inverters (HA-entity
    ``entity_map``, no ``harvest_config`` at all) and MQTT-only inverters are
    never eligible — they carry no register address space to mirror, so they
    must never POST.
    """
    hc = inv.get("harvest_config")
    if not hc:
        return None
    protocol = hc.get("protocol", "solarman_v5")
    if protocol not in ("solarman_v5", "modbus_tcp", _EYBOND_PROTOCOL):
        return None
    model_id = hc.get("model_id")
    if not model_id or settings_read_plan_for(model_id, hc, _is_eybond(hc)) is None:
        return None
    return model_id, hc


async def read_settings_snapshot(
    hass,
    plan: SettingsReadPlan,
    inverter_id: str,
    model_id: str,
    cfg: dict,
    *,
    eybond_hub=None,
    errors_out: list[str] | None = None,
) -> dict | None:
    """Read one non-Deye inverter's settings into a snapshot, or None.

    The snapshot is stored as ``{"inverter_id", "model_id", "kind", "body",
    "read_at"}``, where ``body`` is the brand body in the contract's own
    camelCase keys (``settings_read_plan`` describes each one). A Deye
    snapshot keeps its older shape and is read by ``sync_inverter_once``.

    An SMG II inverter is read through the EyBond collector that reports its
    serial on ``eybond_hub``; every other kind over the harvest transport.
    """

    def fail(reason: str) -> None:
        if errors_out is not None:
            errors_out.append(reason)

    if plan.kind == "smg":
        if eybond_hub is None:
            fail("the EyBond hub is not running")
            return None
        from .executors.smg_settings_executor import EybondSmgSettingsExecutor

        executor = EybondSmgSettingsExecutor(
            hub=eybond_hub, inverter_serial=cfg.get("inverter_serial")
        )
        try:
            body = await executor.read_sync_body()
        except Exception as exc:  # noqa: BLE001 — any collector failure aborts the read
            fail(str(exc) or type(exc).__name__)
            return None
        if body is None:
            fail("no settings body for this protocol number, or a short block")
            return None
    else:
        words = await read_holding_blocks(
            hass,
            cfg,
            plan.blocks,
            optional=plan.extra.get("optional", ()),
            errors_out=errors_out,
        )
        if words is None:
            return None
        body = build_snapshot_body(plan, words)
        if body is None:
            fail(f"the {plan.kind} read cannot make a settings body")
            return None
    return {
        "inverter_id": inverter_id,
        "model_id": model_id,
        "kind": plan.kind,
        "body": body,
        "read_at": _utc_now_iso(),
    }


def _note_read_failure(
    inverter_id: str, model_id: str, cfg: dict, errors: list[str], failing: set[str] | None
) -> None:
    """Log a failed settings read: WARNING once per failure streak, then DEBUG."""
    reason = "; ".join(errors) or "unknown error"
    if failing is None or inverter_id not in failing:
        _LOGGER.warning(
            "settings_sync: could not read the settings of inverter %s (model %s) "
            "at %s:%s: %s. Retrying; repeat failures are logged at debug level",
            inverter_id,
            model_id,
            cfg.get("ip"),
            cfg.get("port"),
            reason,
        )
        if failing is not None:
            failing.add(inverter_id)
    else:
        _LOGGER.debug(
            "settings_sync: settings read of inverter %s failed again: %s",
            inverter_id,
            reason,
        )


def _note_read_success(inverter_id: str, failing: set[str] | None) -> None:
    """End a failure streak, if one was running."""
    if failing is not None and inverter_id in failing:
        failing.discard(inverter_id)
        _LOGGER.info("settings_sync: settings of inverter %s read again", inverter_id)


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
    backoff_until: dict[str, float] | None = None,
    cloud_ingest_enabled: bool = True,
    snapshots: dict[str, dict] | None = None,
    failing: set[str] | None = None,
    eybond_hub=None,
) -> bool:
    """One settings-sync cycle for a single already-eligible inverter.

    Read -> cache -> hash -> ``should_upload`` -> POST.

    Only a Deye-family inverter goes past the cache. Any other kind
    (LuxPower, MUST, SMG II, Solis) is read with ``read_settings_snapshot``,
    cached in ``snapshots``, and never POSTed: the cloud upload from this
    add-on stays Deye-only. ``eybond_hub`` is the running EyBond hub, which
    an SMG II read needs.

    ``snapshots`` maps ``inverter_id`` to the last successful read (for a
    Deye: ``inverter_id``, ``model_id``, ``start_register``, ``registers``,
    ``read_at``; for any other kind see ``read_settings_snapshot``) and is
    replaced on EVERY successful read, whether or not a
    POST follows — the island settings view serves it.

    With ``cloud_ingest_enabled`` False the cycle stops after the cache: no
    POST, and the 4xx backoff (which only guards POSTs) does not skip the
    read. Island mode keeps settings on the LAN.

    ``failing`` holds the inverters whose last read failed. A read failure is
    a WARNING for the first failure of a streak and DEBUG for the rest, so a
    fast retry does not repeat the warning; without the set, every failure
    warns.

    ``cache`` maps
    ``inverter_id -> (last_hash, last_uploaded_monotonic)`` and is updated
    ONLY when the POST returns True (2xx) — a failed POST, or a cycle where
    ``should_upload`` said no, leaves the cache untouched so the next cycle
    re-evaluates from the same last-known-good state (bootstrap/changed/
    heartbeat clauses all still apply next time).

    ``backoff_until`` maps ``inverter_id -> monotonic timestamp`` until which
    this inverter is skipped entirely (no register read, no POST) after a
    4xx ``SettingsSyncRejected`` — permanent rejections (e.g. an unbound
    device api-key) would otherwise retry every 5-min tick forever (the A2
    permanent-403-loop gap). A 5xx / transport error is unaffected and still
    retries next cycle as before.

    Returns True iff a POST was sent and accepted.
    """
    now = now_monotonic_fn()

    plan = settings_read_plan_for(model_id, cfg, _is_eybond(cfg))
    if plan is None:  # pragma: no cover — caller already filtered this
        return False
    if plan.kind != "deye":
        read_errors: list[str] = []
        snapshot = await read_settings_snapshot(
            hass, plan, inverter_id, model_id, cfg, eybond_hub=eybond_hub, errors_out=read_errors
        )
        if snapshot is None:
            _note_read_failure(inverter_id, model_id, cfg, read_errors, failing)
            return False
        _note_read_success(inverter_id, failing)
        if snapshots is not None:
            snapshots[inverter_id] = snapshot
        return False

    if cloud_ingest_enabled and backoff_until is not None:
        until = backoff_until.get(inverter_id)
        if until is not None and now < until:
            _LOGGER.debug(
                "settings_sync: inverter %s still in 4xx backoff (until=%.0f, now=%.0f), skipping",
                inverter_id,
                until,
                now,
            )
            return False

    errors: list[str] = []
    registers = await read_config_registers(hass, cfg, model_id, errors_out=errors)
    if registers is None:
        _note_read_failure(inverter_id, model_id, cfg, errors, failing)
        return False
    _note_read_success(inverter_id, failing)

    config_range = config_range_for_model(model_id)
    if config_range is None:  # pragma: no cover — caller already filtered this
        return False
    start, _count = config_range

    if snapshots is not None:
        snapshots[inverter_id] = {
            "inverter_id": inverter_id,
            "model_id": model_id,
            "start_register": start,
            "registers": list(registers),
            "read_at": _utc_now_iso(),
        }

    if not cloud_ingest_enabled:
        _LOGGER.debug(
            "settings_sync: cloud ingest is off; settings of inverter %s stay on the LAN",
            inverter_id,
        )
        return False

    new_hash = registers_hash(registers)
    cached_hash, last_uploaded = cache.get(inverter_id, (0, 0))

    if not should_upload(new_hash, cached_hash, last_uploaded, now, heartbeat_s):
        return False

    try:
        ok = await api_client.sync_settings(
            api_key=api_key,
            inverter_id=inverter_id,
            model_id=model_id,
            start_register=start,
            registers=registers,
        )
    except SettingsSyncRejected as exc:
        if backoff_until is not None:
            backoff_until[inverter_id] = now + SETTINGS_SYNC_BACKOFF_S
        _LOGGER.warning(
            "settings_sync: inverter %s rejected (HTTP %s) — backing off for %ss",
            inverter_id,
            exc.status,
            SETTINGS_SYNC_BACKOFF_S,
        )
        return False

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
    backoff_until: dict[str, float] | None = None,
    cloud_ingest_enabled: bool = True,
    snapshots: dict[str, dict] | None = None,
    failing: set[str] | None = None,
    eybond_hub=None,
) -> None:
    """One pass over every inverter on an entry: sync each eligible one.

    Ineligible inverters (relay/preset — no ``harvest_config`` — or a
    ``harvest_config`` whose model has no settings read plan) are skipped
    with zero reads and zero POSTs; this is the enforcement point for
    "preset/MQTT-only inverters must never POST". A per-inverter exception is
    caught and logged so one bad inverter can't stall the rest of the tick.

    ``backoff_until`` (per-inverter 4xx backoff — see ``sync_inverter_once``)
    is threaded through unchanged so it persists across ticks within the same
    loop instance, same as ``cache``. ``eybond_hub`` reaches the SMG II
    inverters on an EyBond collector.
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
                backoff_until=backoff_until,
                cloud_ingest_enabled=cloud_ingest_enabled,
                snapshots=snapshots,
                failing=failing,
                eybond_hub=eybond_hub,
            )
        except Exception:  # noqa: BLE001 — one inverter's failure must not block others
            _LOGGER.exception("settings_sync: tick failed for inverter %s", inverter_id)


def _eybond_hub_of(hass, entry):
    """The entry's running EyBond hub, or None.

    Read every tick: setup starts the hub after this loop, and stores it in
    ``hass.data`` only once setup finishes. Until then an SMG II read fails
    and takes the fast first-read retry like any other failed read.
    """
    state = (getattr(hass, "data", {}).get(DOMAIN) or {}).get(entry.entry_id)
    return state.get("eybond_hub") if isinstance(state, dict) else None


async def _seed_snapshots(store, eligible: dict[str, str], snapshots: dict[str, dict]) -> None:
    """Load persisted reads for inverters with none in memory.

    A read persisted for a different model is skipped: after a model change
    its registers describe another address map.
    """
    for inverter_id, model_id in eligible.items():
        if inverter_id in snapshots:
            continue
        try:
            snapshot = await store.get_settings_snapshot(inverter_id)
        except Exception:  # noqa: BLE001 — a missing seed only delays the view
            _LOGGER.exception("settings_sync: could not load saved settings of %s", inverter_id)
            continue
        if snapshot is not None and snapshot.get("model_id") == model_id:
            snapshots[inverter_id] = snapshot


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
    snapshots: dict[str, dict] | None = None,
    store=None,
    cloud_upload: bool = True,
) -> None:
    """Every ``interval_s`` (default 300s / 5 min), settings-sync every
    register-capable inverter on this config entry.

    First read: until every eligible inverter has been read once in this run,
    a tick that leaves one unread is followed by a short delay from
    ``_FIRST_READ_RETRY_S`` instead of ``interval_s``, and that retry reads
    only the inverters still unread. Once the delays run out, or every
    inverter has been read, the loop keeps ``interval_s``.

    Cloud ingest: the entry's ``cloud_ingest_enabled`` flag is re-read every
    tick. While it is off, reads are cached and never POSTed. Reads of any
    kind but Deye are never POSTed at all.

    ``cloud_upload=False`` is a true island: reads are cached and never
    POSTed whatever the flag says, and the loop never touches the entry's
    ``api_key`` or ``api_base``, which a local-only entry does not have.

    ``snapshots`` receives every successful read (see ``sync_inverter_once``);
    pass the dict the island settings view reads. With a ``store``, each new
    read is persisted, and on the first tick an inverter with no read yet is
    seeded from the store, so the view can answer straight after a restart.
    A seeded read does not count as a read of this run.

    Lifecycle: exits when ``hass.is_stopping`` is True or (when provided)
    ``lifecycle.active`` becomes False — same contract as the other
    long-running loops (readings publisher, direct-harvest, command poller).

    ``api_client`` defaults to a fresh ``SvitgridApiClient`` sharing hass's
    shared aiohttp session (same construction pattern used elsewhere in
    ``__init__.py``); pass one explicitly in tests. Re-reads
    ``entry.data.get("inverters")`` every tick so a live options/entry update
    (add/remove/edit inverter, switch read source) takes effect on the next
    cycle without a reload. The per-inverter hash/heartbeat cache AND the
    per-inverter 4xx-backoff cache both live only for the lifetime of this
    loop instance — a reload/restart resets them, which just re-triggers
    each inverter's bootstrap clause (and, for a still-403ing device, one
    more rejected POST before backing off again) once more on the next tick
    — not a correctness issue.
    """
    if cloud_upload and api_client is None:
        from homeassistant.helpers import aiohttp_client

        from .api_client import SvitgridApiClient

        session = aiohttp_client.async_get_clientsession(hass)
        api_client = SvitgridApiClient(session, api_base=entry.data["api_base"])

    api_key = entry.data["api_key"] if cloud_upload else None
    cache: dict[str, tuple[int, int]] = {}
    backoff_until: dict[str, float] = {}
    if snapshots is None:
        snapshots = {}
    failing: set[str] = set()
    read_this_run: set[str] = set()
    first_retries = 0
    retrying = False
    seeded = store is None

    _LOGGER.info("Settings-sync loop started for entry %s", entry.entry_id)
    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        inverters = entry.data.get("inverters") or []
        eligible = {
            inv["inverter_id"]: model_and_cfg[0]
            for inv in inverters
            if inv.get("inverter_id")
            and (model_and_cfg := _eligible_for_settings_sync(inv)) is not None
        }
        # A removed inverter must not keep answering on the LAN.
        for stale_id in [i for i in snapshots if i not in eligible]:
            del snapshots[stale_id]
        if not seeded:
            seeded = True
            await _seed_snapshots(store, eligible, snapshots)

        due = inverters
        if retrying:
            due = [inv for inv in inverters if inv.get("inverter_id") not in read_this_run]
        before = dict(snapshots)
        await settings_sync_tick(
            hass,
            api_client,
            api_key,
            due,
            cache,
            heartbeat_s=heartbeat_s,
            now_monotonic_fn=now_monotonic_fn,
            backoff_until=backoff_until,
            cloud_ingest_enabled=cloud_upload and _cloud_ingest_enabled(entry),
            snapshots=snapshots,
            failing=failing,
            eybond_hub=_eybond_hub_of(hass, entry),
        )
        for inv_id, snapshot in snapshots.items():
            if before.get(inv_id) is snapshot:
                continue
            read_this_run.add(inv_id)
            if store is not None:
                try:
                    await store.set_settings_snapshot(snapshot)
                except Exception:  # noqa: BLE001 — the in-memory cache still serves
                    _LOGGER.exception("settings_sync: could not persist settings of %s", inv_id)

        unread = any(inv_id not in read_this_run for inv_id in eligible)
        retrying = unread and first_retries < len(_FIRST_READ_RETRY_S)
        if retrying:
            next_sleep_s = min(_FIRST_READ_RETRY_S[first_retries], interval_s)
            first_retries += 1
        else:
            next_sleep_s = interval_s
        await sleep_fn(next_sleep_s)
    _LOGGER.info("Settings-sync loop stopped for entry %s", entry.entry_id)
