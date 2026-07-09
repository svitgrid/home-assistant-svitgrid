"""Svitgrid custom component — B1 MVP.

Wires everything together:
  - Reads `svitgrid:` block from configuration.yaml
  - Validates entity_map has all 5 required fields
  - Bootstraps if first-run; loads saved state otherwise
  - Starts readings publisher + command poller as long-running tasks
  - Unregisters cleanly on hass stop
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .activity import ActivityTracker
from .api_client import SvitgridApiClient
from .battery_sign import preset_is_discharge_positive
from .bootstrap import run_first_time
from .cloud_endpoint_handler import probe_endpoint_auth
from .command_poller import run_loop as run_command_loop
from .const import (
    COMMAND_POLL_INTERVAL_S,
    CONF_AUTO_UPDATE,
    DOMAIN,
    HOURLY_RETENTION_S,
    RAW_RETENTION_S,
    READINGS_DB_FILE,
    READINGS_DB_SUBDIR,
    READINGS_INTERVAL_S,
    REQUIRED_FIELDS,
    ROLLUP_INTERVAL_S,
)
from .executors import create_executor
from .executors.yaml_dispatcher import YamlDispatcher
from .harvest.engine import run_direct_harvest_loop
from .harvest.event_scheduler_loop import run_event_scheduler_loop
from .harvest.register_spec import RegisterSpec
from .harvest.spec_cache import load_spec
from .harvest.write_executor import WriteExecutor
from .http_views import register_views
from .island_event_store import IslandEventStore
from .keystore import SvitgridKeystore
from .lifecycle import DEPROVISIONED, LifecycleState
from .mqtt_wake import run_loop as run_mqtt_wake_loop
from .panel import register_panel, remove_panel
from .preset_refresh import refresh_entry_inverters
from .reading_sender import Cadence, run_sender_loop
from .reading_store import ReadingStore
from .readings_publisher import run_loop as run_readings_loop
from .update import SvitgridUpdateCoordinator
from .updater import read_installed_version

_LOGGER = logging.getLogger(__name__)


async def _start_local_store(
    hass: HomeAssistant,
    api_client,
    api_key: str,
    activity=None,
    active_ids=None,
    cloud_ingest_enabled: bool = True,
    discharge_positive_ids=None,
):
    """Create the per-entry local store, seed the shared lifecycle holder from
    persisted state, register the read views once, and (only when the device is
    still active) start the sender + rollup timer. Returns (store, cadence,
    sender_task, cancel_rollup, lifecycle).

    active_ids: set of inverter_id strings currently in the active config.
    Orphaned readings_raw rows (for inverter ids NOT in active_ids) are pruned
    BEFORE the sender starts so they cannot block the new inverter's queue
    (head-of-line blocking after a re-pair that changes the inverter id).

    cloud_ingest_enabled: when False (pure island mode), the cloud sender
    (run_sender_loop) is NOT spawned. The local store, rollup timer, and all
    harvest/readings loops run regardless — only the cloud upload path is gated.

    When the persisted lifecycle is not active (paused/deprovisioned), the
    sender loop and rollup timer are NOT started; their slots are returned as
    None. Views/panel registration is always performed by the caller path."""
    from datetime import UTC, datetime, timedelta

    from homeassistant.helpers.event import async_track_time_interval

    db_dir = hass.config.path(READINGS_DB_SUBDIR)
    # os.makedirs's 2nd positional arg is `mode`, not `exist_ok`; bind exist_ok
    # as a keyword via partial so a pre-existing dir doesn't raise.
    from functools import partial

    await hass.async_add_executor_job(partial(os.makedirs, db_dir, exist_ok=True))
    store = ReadingStore(hass, os.path.join(db_dir, READINGS_DB_FILE))

    # Prune orphaned rows BEFORE the sender starts (re-pair queue poison fix).
    if active_ids is not None:
        pruned = await store.prune_inverters_not_in(active_ids)
        if pruned:
            _LOGGER.info(
                "Pruned %d orphaned readings_raw row(s) for inverters not in active config %s",
                pruned,
                active_ids,
            )

    cadence = Cadence()

    # Register the read HTTP views once per hass. A second config entry would
    # otherwise crash on duplicate view registration. SP1 limitation: the panel
    # serves the FIRST entry's store.
    hass.data.setdefault(DOMAIN, {})
    if not hass.data[DOMAIN].get("_views_registered"):
        register_views(hass, store)
        hass.data[DOMAIN]["_views_registered"] = True

    # Seed the shared lifecycle holder from persisted state. The activity
    # tracker (if any) is mirrored into the status sensor on lifecycle changes.
    persisted = await store.get_lifecycle()
    lifecycle = LifecycleState(
        state=persisted["state"],
        reason=persisted["reason"],
        since=persisted["since"],
        activity=activity,
    )

    if lifecycle.state == DEPROVISIONED:
        # Only the terminal deprovisioned state skips the sender and rollup
        # timer. Paused devices must still run loops so the command poller can
        # detect an operator re-enable.
        return store, cadence, None, None, lifecycle

    # Gate the cloud sender on the per-entry flag. The harvest engine, local-store
    # writes, and rollup timer all run regardless — only cloud upload is skipped.
    # (The command poller stays spawned even in pure island — harmless; SP3 may
    # gate it if needed.)
    sender_task = None
    if cloud_ingest_enabled:
        sender_task = hass.async_create_background_task(
            run_sender_loop(
                hass=hass,
                store=store,
                api_client=api_client,
                api_key=api_key,
                cadence=cadence,
                lifecycle=lifecycle,
                discharge_positive_ids=discharge_positive_ids,
            ),
            name="svitgrid_reading_sender",
        )

    async def _rollup_tick(_now=None):
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            await store.rollup(now_iso)
            await store.prune(now_iso, RAW_RETENTION_S, HOURLY_RETENTION_S)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("rollup/prune failed")

    cancel_rollup = async_track_time_interval(
        hass, _rollup_tick, timedelta(seconds=ROLLUP_INTERVAL_S)
    )

    return store, cadence, sender_task, cancel_rollup, lifecycle


def _validate_entity_map(value: dict) -> dict:
    missing = REQUIRED_FIELDS - set(value.keys())
    if missing:
        raise vol.Invalid(f"entity_map missing required fields: {sorted(missing)}")
    return value


async def apply_cloud_endpoint_change(
    hass: HomeAssistant,
    entry: ConfigEntry,
    new_api_base: str,
) -> bool:
    """Pre-flight probe + apply a `set_cloud_endpoint` command's URL.

    Phase 1: probe the new endpoint with our api_key (GET /api/v3/me).
    If the probe returns non-200, log a distinctive ERROR and return False
    without mutating anything — prevents silent dead-in-water state when the
    new env doesn't have our api_key/trustedDevices synced.

    Phase 2 (probe OK): update ConfigEntry data in-place and schedule a
    reload on the event loop. Returns True.

    Why reload is scheduled, not awaited: the caller is the command-poller
    task spawned by `async_setup_entry`. Awaiting `async_reload` from inside
    it would deadlock — the reload's unload step blocks waiting for the poller
    task to finish, which is the very task awaiting the reload. Scheduling via
    `async_create_task` lets the current poll iteration return cleanly, then
    the reload runs and the new api_base takes effect.

    No-op (returns True) when the new URL equals the current one — a
    redundant migration command (e.g. a re-fired Cloud Function on a
    same-state flag flip) must not bounce a healthy integration.

    URL VALIDATION and the AUTH PROBE IS THE CALLER'S RESPONSIBILITY for
    command_poller Arm 1c (which calls probe_endpoint_auth before this helper).
    This helper also probes internally so it remains correct when called from
    other paths."""
    current = entry.data.get("api_base")
    if current == new_api_base:
        _LOGGER.info(
            "set_cloud_endpoint: already on %s, skipping reload",
            new_api_base,
        )
        return True

    # Pre-flight auth probe — mirrors firmware D5's ce_apply_url semantics.
    session = aiohttp_client.async_get_clientsession(hass)
    api_key = entry.data.get("api_key", "")
    probe_ok = await probe_endpoint_auth(session, api_key=api_key, new_api_base=new_api_base)
    if not probe_ok:
        _LOGGER.error(
            "set_cloud_endpoint probe failed — new endpoint %s did not accept "
            "our api_key (HTTP non-200). Migration aborted; integration stays "
            "on %s. Ensure the target environment has the api_key registered "
            "and households/{id}/trustedDevices synced before retrying.",
            new_api_base,
            current,
        )
        return False

    new_data = {**entry.data, "api_base": new_api_base}
    hass.config_entries.async_update_entry(entry, data=new_data)
    _LOGGER.info(
        "set_cloud_endpoint: api_base %s -> %s, reloading entry",
        current,
        new_api_base,
    )
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener: reload the entry so a new entity_map takes effect.

    A cadence-only change (flagged by the cadence PUT handler) is applied to the
    in-memory holder directly and needs no reload — reloading would re-run setup
    unnecessarily. The flag is consumed (popped) so any later non-cadence update
    still reloads."""
    if hass.data.get(DOMAIN, {}).pop("_cadence_only_update", False):
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _inverters_from_entry(entry: ConfigEntry) -> list[dict]:
    """Return the per-inverter config list. v2+ entries store `inverters`;
    options may override a single inverter's entity_map (legacy edit flow).
    Always returns a list of dicts with keys: inverter_id, entity_map,
    command_recipes, command_config, brand, model, phases, has_battery,
    pv_strings, preset_id."""
    invs = entry.data.get("inverters")
    if invs:
        result = [dict(i) for i in invs]
        # Legacy options.entity_map applied to the FIRST inverter only (the
        # pre-multi edit flow wrote a flat entity_map). New per-inverter edits
        # write entry.data directly (Task 9), so this is back-compat only.
        opt_map = entry.options.get("entity_map")
        if opt_map is not None and result:
            result[0]["entity_map"] = dict(opt_map)
        return result
    return []


def _migrate_v1_to_v2(data: dict) -> dict:
    """Wrap legacy scalar fields into a single-element inverters list."""
    new = {
        k: data[k]
        for k in (
            "api_base",
            "api_key",
            "edge_device_id",
            "household_id",
            "signing_key_id",
            "private_key_pem",
            "public_key_hex",
            "trusted_keys",
        )
        if k in data
    }
    new["inverters"] = [
        {
            "inverter_id": data.get("hardware_id"),
            "entity_map": data.get("entity_map") or {},
            "command_recipes": data.get("commands") or [],
            "command_config": {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8},
            "brand": data.get("brand"),
            "model": data.get("model"),
            "phases": data.get("phases"),
            "has_battery": data.get("has_battery"),
            "pv_strings": data.get("pv_strings"),
            "preset_id": data.get("preset_id"),
        }
    ]
    return new


def _initial_cadence_seconds(entry_data: dict) -> int:
    """Clamp the persisted harvest interval into the harvest clamp bounds."""
    from .const import CADENCE_DEFAULT_INTERVAL_S
    from .readings_publisher import _INTERVAL_CEILING_S, _INTERVAL_FLOOR_S

    raw = entry_data.get("harvest_interval_seconds", CADENCE_DEFAULT_INTERVAL_S)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return CADENCE_DEFAULT_INTERVAL_S
    return max(_INTERVAL_FLOOR_S, min(_INTERVAL_CEILING_S, v))


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 (scalar single-inverter) entries to v2 (inverters list)."""
    if entry.version >= 2:
        return True
    new_data = _migrate_v1_to_v2(dict(entry.data))
    hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    _LOGGER.info(
        "Migrated Svitgrid entry %s to v2 (%d inverter(s))",
        entry.entry_id,
        len(new_data["inverters"]),
    )
    return True


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("api_base"): cv.url,
                vol.Required("device_id"): cv.string,
                vol.Required("signing_key_id"): cv.string,
                vol.Required("entity_map"): vol.All(
                    {cv.string: cv.entity_id}, _validate_entity_map
                ),
                vol.Optional("readings_interval_seconds", default=READINGS_INTERVAL_S): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=300)
                ),
                vol.Optional(
                    "command_poll_interval_seconds", default=COMMAND_POLL_INTERVAL_S
                ): vol.All(vol.Coerce(int), vol.Range(min=2, max=60)),
                vol.Optional("executor"): vol.Schema(
                    {
                        vol.Optional("type", default="read_only"): vol.In(["read_only", "smg_ii"]),
                        vol.Optional("modbus_hub"): cv.string,
                        vol.Optional("modbus_slave", default=1): vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=247)
                        ),
                        vol.Optional("battery_nominal_voltage", default=48.0): vol.All(
                            vol.Coerce(float), vol.Range(min=12.0, max=600.0)
                        ),
                    }
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Svitgrid integration from YAML config."""
    conf = config.get(DOMAIN)
    if not conf:
        return True  # no svitgrid: block — nothing to do

    api_base = conf["api_base"]
    device_id = conf["device_id"]
    signing_key_id = conf["signing_key_id"]
    entity_map = conf["entity_map"]
    # readings_interval_seconds is now governed by the shared Cadence holder
    # (sender-driven) rather than a static config value.
    command_interval = conf["command_poll_interval_seconds"]

    session = aiohttp_client.async_get_clientsession(hass)
    api_client = SvitgridApiClient(session, api_base=api_base)
    keystore = SvitgridKeystore(hass)

    state = await keystore.load()
    if state is None:
        try:
            state = await run_first_time(
                api_client=api_client,
                keystore=keystore,
                device_id=device_id,
                signing_key_id=signing_key_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Svitgrid bootstrap failed. Integration will not start. "
                "Check device_id and that the mobile app opened a bootstrap window."
            )
            return False

    # In-memory cache populated from the keystore (which is written by
    # bootstrap + updated live via add_trusted_key / revoke_trusted_key
    # commands handled in command_poller.process_command).
    trusted_public_keys_hex: dict[str, str] = dict(state.trusted_public_keys_hex)

    # Build executor from YAML config (or None for read_only / missing block)
    executor_config = conf.get("executor") or {}
    executor = create_executor(executor_config, hass)

    # Inverter ID — B1 uses device_id as a placeholder. B2's config flow
    # resolves the real inverter ID from the bootstrap response.
    inverter_id = device_id

    # YAML path has no ActivityTracker; pass None as the activity mirror.
    # active_ids contains the single inverter_id used by the YAML config.
    store, cadence, sender_task, cancel_rollup, lifecycle = await _start_local_store(
        hass, api_client, state.api_key, None, active_ids={inverter_id}
    )
    await register_panel(hass)

    readings_task = None
    poller_task = None

    if lifecycle.state != DEPROVISIONED:
        readings_task = hass.async_create_background_task(
            run_readings_loop(
                hass=hass,
                store=store,
                cadence=cadence,
                inverter_id=inverter_id,
                entity_map=entity_map,
                lifecycle=lifecycle,
            ),
            name="svitgrid_readings_publisher",
        )

        poller_task = hass.async_create_background_task(
            run_command_loop(
                hass=hass,
                api_client=api_client,
                keystore=keystore,
                trusted_public_keys_hex=trusted_public_keys_hex,
                executor_version="0.2.0",
                integration_version=read_installed_version(Path(__file__).parent),
                executors_by_inverter=({inverter_id: executor} if executor else {}),
                interval_s=command_interval,
                lifecycle=lifecycle,
                store=store,
            ),
            name="svitgrid_command_poller",
        )
    else:
        _LOGGER.warning(
            "Svitgrid device is %s (reason=%s); readings/command loops not started.",
            lifecycle.state,
            lifecycle.reason,
        )

    async def _on_stop(_event: Event) -> None:
        if cancel_rollup:
            cancel_rollup()
        for task in (readings_task, poller_task, sender_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *[t for t in (readings_task, poller_task, sender_task) if t],
            return_exceptions=True,
        )

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].update(
        {
            "session": session,
            "api_client": api_client,
            "keystore": keystore,
            "executor": executor,
            "trusted_public_keys_hex": trusted_public_keys_hex,
            "readings_task": readings_task,
            "poller_task": poller_task,
            "store": store,
            "sender_task": sender_task,
            "cancel_rollup": cancel_rollup,
            "lifecycle": lifecycle,
        }
    )
    _LOGGER.info("Svitgrid integration started (device_id=%s)", device_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Svitgrid integration from a config entry.

    Iterates the per-inverter list from `_inverters_from_entry`, spawning one
    readings loop and (when recipes are present) one YamlDispatcher per inverter.
    A single command poller and MQTT wake loop are shared across all inverters.
    """
    data = entry.data
    session = aiohttp_client.async_get_clientsession(hass)
    api_client = SvitgridApiClient(session, api_base=data["api_base"])
    api_key = data["api_key"]
    activity = ActivityTracker()
    inverters = _inverters_from_entry(entry)

    # Seed the keystore blob from entry data.
    # Purpose 1 (keystore-population fix, SP2): if entry.data["island_key"] is
    #   set (island pairing), write it into the keystore blob.  At config-flow
    #   finalize time the keystore blob didn't exist yet (fresh install), so the
    #   async_set_island_key call there was a no-op.  This is the authoritative
    #   write that closes the gap: after async_setup_entry completes,
    #   keystore.async_get_island_key() reliably returns the generated key.
    # Purpose 2 (island auth, SP1): http_views.py reads
    #   hass.data[DOMAIN]["keystore"] to validate X-Island-Key.  Storing the
    #   keystore instance here makes that lookup work for config-entry paths
    #   (the YAML path stores it too, but the entry path historically did not).
    # ORDER: keystore.save() must run BEFORE any call to async_set_island_key so
    #   the blob exists.  save() accepts island_key=None (non-island), in which
    #   case it preserves whatever island_key was already stored in the blob.
    keystore = SvitgridKeystore(hass)
    _trusted_keys_raw = data.get("trusted_keys") or []
    _trusted_key_ids = [tk["keyId"] for tk in _trusted_keys_raw]
    _trusted_public_keys_hex = {
        tk["keyId"]: tk["publicKeyHex"] for tk in _trusted_keys_raw if "publicKeyHex" in tk
    }
    await keystore.save(
        api_key=data["api_key"],
        public_key_hex=data["public_key_hex"],
        private_key_pem=data["private_key_pem"],
        signing_key_id=data["signing_key_id"],
        trusted_key_ids=_trusted_key_ids,
        trusted_public_keys_hex=_trusted_public_keys_hex,
        # Explicit pass: non-None writes the island key; None preserves existing.
        island_key=data.get("island_key"),
    )
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["keystore"] = keystore

    if not inverters:
        _LOGGER.warning(
            "Config entry %s has no inverters configured; nothing to publish.",
            entry.entry_id,
        )

    readings_tasks: dict[str, asyncio.Task] = {}
    executors_by_inverter: dict[str, YamlDispatcher] = {}
    store = None
    event_store = None
    sender_task = None
    cancel_rollup = None
    lifecycle = None
    # Hoisted so it is accessible outside the `if inverters:` block (used when
    # spawning the island event scheduler in the `if loops_active:` block below).
    cloud_ingest_enabled = entry.data.get(
        "cloud_ingest_enabled",
        entry.options.get("cloud_ingest_enabled", True),
    )

    if inverters:
        active_ids = {inv["inverter_id"] for inv in inverters}
        # Inverters whose battery power we normalize to Svitgrid's charge-positive
        # convention at capture (HA Solarman) — the sender re-inverts these before
        # upload so the server's existing negation is unchanged. See battery_sign.py.
        discharge_positive_ids = {
            inv["inverter_id"]
            for inv in inverters
            if preset_is_discharge_positive(inv.get("preset_id"))
        }
        store, cadence, sender_task, cancel_rollup, lifecycle = await _start_local_store(
            hass,
            api_client,
            api_key,
            activity,
            active_ids=active_ids,
            cloud_ingest_enabled=cloud_ingest_enabled,
            discharge_positive_ids=discharge_positive_ids,
        )
        cadence.interval_s = _initial_cadence_seconds(dict(entry.data))
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN]["cadence"] = cadence
        hass.data[DOMAIN]["cadence_entry_id"] = entry.entry_id
        await register_panel(hass)

        # Island event store — always constructed so Task 2's SvitgridEventsView
        # (which reads hass.data[DOMAIN]["event_store"]) is always populated.
        # Shares the same DB directory as the readings store; the directory was
        # already created by _start_local_store above.
        _db_dir = hass.config.path(READINGS_DB_SUBDIR)
        event_store = IslandEventStore(os.path.join(_db_dir, "island_events.db"), hass=hass)
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN]["event_store"] = event_store

    # Only deprovisioned (terminal) skips all loops. Paused devices still start
    # loops so the command poller can detect an operator re-enable. The
    # panel/views/sensors are always set up so the user can see lifecycle status.
    loops_active = lifecycle is not None and lifecycle.state != DEPROVISIONED

    # Refresh entity_map from preset before starting the publisher so the
    # publisher always uses the most up-to-date field mappings. Skip when
    # deprovisioned (no loops will start anyway). Fail-open: never blocks setup.
    if loops_active:

        async def _fetch_preset(pid):
            return await api_client.get_preset(pid)

        try:
            _inv_list = list(entry.data.get("inverters") or [])
            new_inverters, changed = await refresh_entry_inverters(
                _inv_list, _fetch_preset, _LOGGER.info
            )
            if changed:
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "inverters": new_inverters}
                )
                # Re-read inverters from the now-updated entry so the publisher
                # uses the refreshed entity_map.
                inverters = _inverters_from_entry(entry)
        except Exception:  # never block setup — fail-open
            _LOGGER.exception("entity_map preset refresh failed")

    command_task = None
    mqtt_wake_task = None
    scheduler_task = None

    if loops_active:
        for inv in inverters:
            inverter_id = inv["inverter_id"]
            harvest_config = inv.get("harvest_config")
            if harvest_config:
                # Direct-Modbus harvest path (SP-B): poll the inverter itself
                # via the register spec instead of reading HA entities. Load the
                # spec once at setup (SP-D revisits periodic refresh) into a tiny
                # mutable holder the loop re-reads each tick. Fail-open: a failed
                # load leaves spec=None and the loop idles until a spec exists.
                spec_holder = SimpleNamespace(spec=None)
                try:
                    spec_dict, _changed = await load_spec(
                        api_client.get_register_spec,
                        harvest_config["model_id"],
                        cached=None,
                    )
                    if spec_dict is not None:
                        spec_holder.spec = RegisterSpec.from_dict(spec_dict)
                except Exception:  # never block setup — fail-open
                    _LOGGER.exception(
                        "harvest spec load/parse failed for inverter %s; loop "
                        "will idle until a spec is available",
                        inverter_id,
                    )
                readings_tasks[inverter_id] = hass.async_create_background_task(
                    run_direct_harvest_loop(
                        hass=hass,
                        store=store,
                        cadence=cadence,
                        inverter_id=inverter_id,
                        cfg=harvest_config,
                        spec_holder=spec_holder,
                        lifecycle=lifecycle,
                        activity=activity,
                    ),
                    name=f"svitgrid_harvest_{inverter_id}",
                )
                # Direct-harvest inverters get the native WriteExecutor, which
                # turns signed control commands into register writes. It REUSES
                # the same spec_holder built above for the read loop, so writes
                # share the live spec (and any later refresh). This replaces the
                # YamlDispatcher relay path — an inverter has exactly one executor.
                executors_by_inverter[inverter_id] = WriteExecutor(
                    hass=hass,
                    spec_holder=spec_holder,
                    cfg=harvest_config,
                )
            else:
                entity_map = dict(inv.get("entity_map") or {})
                if not entity_map:
                    _LOGGER.warning(
                        "Inverter %s has an empty entity_map — it will not publish readings.",
                        inverter_id,
                    )
                readings_tasks[inverter_id] = hass.async_create_background_task(
                    run_readings_loop(
                        hass=hass,
                        store=store,
                        cadence=cadence,
                        inverter_id=inverter_id,
                        entity_map=entity_map,
                        activity=activity,
                        lifecycle=lifecycle,
                        discharge_positive=preset_is_discharge_positive(inv.get("preset_id")),
                    ),
                    name=f"svitgrid_readings_{inverter_id}",
                )
                # Relay (HA-entity) inverters use the recipe-driven YamlDispatcher.
                recipes = inv.get("command_recipes") or []
                if recipes:
                    executors_by_inverter[inverter_id] = YamlDispatcher(
                        hass=hass,
                        commands=recipes,
                        config=dict(inv.get("command_config") or {}),
                    )

        wake_event = asyncio.Event()
        command_task = hass.async_create_background_task(
            run_command_loop(
                hass=hass,
                api_client=api_client,
                # Pass the keystore built + saved above (also stored in
                # hass.data[DOMAIN]["keystore"] for X-Island-Key validation).
                # enable_island seeds the island key via keystore.async_set_island_key;
                # with keystore=None it hit the fail-closed guard and rejected every
                # enable_island command (reason=keystore_unavailable), so the
                # "convert existing HA household to island" switch never applied.
                # entry_data still carries the trust material for the signed-command
                # path; the keystore.load() branch reads the same fields it was saved from.
                keystore=keystore,
                entry_data=dict(data),
                wake_event=wake_event,
                activity=activity,
                executors_by_inverter=executors_by_inverter,
                lifecycle=lifecycle,
                store=store,
                entry=entry,
                integration_version=read_installed_version(Path(__file__).parent),
            ),
            name="svitgrid_command_poller",
        )

        mqtt_wake_task = hass.async_create_background_task(
            run_mqtt_wake_loop(
                hass=hass,
                api_client=api_client,
                api_key=api_key,
                wake_event=wake_event,
            ),
            name="svitgrid_mqtt_wake",
        )

        # Island event scheduler — spawned ONLY in pure island mode (cloud-sync off).
        # With cloud_ingest_enabled=True the cloud engine handles calendar events;
        # running both would double-fire commands.
        if not cloud_ingest_enabled and store is not None and event_store is not None:
            scheduler_task = hass.async_create_background_task(
                run_event_scheduler_loop(
                    hass=hass,
                    store=store,
                    event_store=event_store,
                    # executor_for is the dict's bound .get method — captures
                    # executors_by_inverter by reference so any updates made
                    # during setup are visible to the running loop.
                    executor_for=executors_by_inverter.get,
                    tz=hass.config.time_zone,
                ),
                name="svitgrid_event_scheduler",
            )
            _LOGGER.info("Island event scheduler started (pure island mode)")
    elif lifecycle is not None:
        _LOGGER.warning(
            "Svitgrid device is %s (reason=%s); readings/command/wake loops not "
            "started for entry %s.",
            lifecycle.state,
            lifecycle.reason,
            entry.entry_id,
        )

    update_coordinator = SvitgridUpdateCoordinator(
        hass,
        session=aiohttp_client.async_get_clientsession(hass),
        install_dir=Path(__file__).parent,
        activity=activity,
        get_auto_update=lambda: entry.options.get(CONF_AUTO_UPDATE, True),
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api_client": api_client,
        "readings_tasks": readings_tasks,
        "command_task": command_task,
        "mqtt_wake_task": mqtt_wake_task,
        "scheduler_task": scheduler_task,
        "executors_by_inverter": executors_by_inverter,
        "activity": activity,
        "entry_data": dict(data),
        "store": store,
        "sender_task": sender_task,
        "cancel_rollup": cancel_rollup,
        "lifecycle": lifecycle,
        "update_coordinator": update_coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(
        entry, ["sensor", "binary_sensor", "update"]
    )
    hass.async_create_background_task(
        update_coordinator.async_refresh(), name="svitgrid_update_first_check"
    )
    _LOGGER.info(
        "Svitgrid started from config entry %s with %d inverter(s)",
        entry.entry_id,
        len(inverters),
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Cancel background tasks when the user removes the integration."""
    await hass.config_entries.async_unload_platforms(entry, ["sensor", "binary_sensor", "update"])
    remove_panel(hass)
    state = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if state is None:
        return True
    for task in (state.get("readings_tasks") or {}).values():
        if task and not task.done():
            task.cancel()
    for key in ("command_task", "mqtt_wake_task", "sender_task", "scheduler_task"):
        task = state.get(key)
        if task and not task.done():
            task.cancel()
    cancel_rollup = state.get("cancel_rollup")
    if cancel_rollup:
        cancel_rollup()
    # Release the IslandEventStore (and its SQLite handle) on final unload.
    # A subsequent async_setup_entry re-creates it, so the reload path is safe.
    hass.data.get(DOMAIN, {}).pop("event_store", None)
    return True
