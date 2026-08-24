"""Constants for the Svitgrid custom component."""

DOMAIN = "svitgrid"

# Timing (seconds)
READINGS_INTERVAL_S = 10
# Initial Cadence interval before the server's first ingest response. Set to the
# 5-min idle cadence so the harvester aligns with an edge device's idle ingest
# (poll-cadence.ts idle = ingestIntervalMs 300_000) from the first sleep — no
# initial fast burst. The server still tunes cadence.interval_s on each push;
# the immediate first poll + eager drain keep first-ingest instant regardless.
CADENCE_DEFAULT_INTERVAL_S = 300
COMMAND_POLL_INTERVAL_S = 5
# Upper bound for the server-driven command-poll cadence (10 min). MQTT-wake
# delivers commands instantly; the HTTP poll is the slow fallback, so the
# server can park us up to this long. Matches the edge firmware / mobile
# clamp ceiling (600_000 ms).
COMMAND_POLL_CEILING_S = 600

# Floor between two trusted-key resyncs triggered by an unknown signing key.
# A legitimately-rejected command (revoked key, wrong household) repeats on
# every poll until it expires, so without a floor one would become a request
# per command per poll. Five minutes keeps recovery prompt while capping the
# worst case at ~12 requests/hour.
TRUSTED_KEY_RESYNC_MIN_INTERVAL_S = 300

# HA Store
STORAGE_KEY = "svitgrid"
STORAGE_VERSION = 1

# Synthetic device id for the pre-0.16.0 single-slot island key, which has no
# real device id.  Reserved: `enable_island` must never store a device under it
# (see command_poller), or a crafted client could hide behind the synthetic row
# and make its own key un-revocable.
LEGACY_ISLAND_DEVICE_ID = "__legacy__"

# Required canonical entity-map fields
REQUIRED_FIELDS = frozenset(
    {"batterySoc", "batteryPower", "batteryVoltage", "pv1Power", "gridPower", "loadPower"}
)

# The subset of API-required reading fields that MUST be sourced from a mapped
# HA entity before we POST. `pvPower` is API-required too, but the readings
# publisher defaults it to 0 for battery-only / no-solar systems (see
# readings_publisher.gate_payload), so it is intentionally NOT listed here.
#
# This set must mirror the server's InverterReadingSchema and go no further.
# Every field listed here beyond what the API actually requires is a way to
# throw away a reading the server would have accepted — and because the
# publisher is capture-then-drain, a discarded reading never reaches the local
# store either, so the in-HA panel goes blank alongside the app.
#
# `batterySoc` used to be listed and is NOT API-required (absent SOC means
# "unknown"; the app shows "Calculating"). One unavailable BMS sensor there
# silently blanked an entire install — including its perfectly good PV data.
# Removed 2026-08-05 (found on rostislav.dudka@gmail.com, Victron GX).
CORE_PAYLOAD_FIELDS = frozenset({"batteryPower", "batteryVoltage", "gridPower", "loadPower"})

# All recognized canonical fields (required + optional)
ALL_FIELDS = REQUIRED_FIELDS | frozenset(
    {
        "pv2Power",
        "pv3Power",
        "pv4Power",
        # Per-string voltage/current. `assemble_payload` renames these to the
        # API's canonical pvVoltageN / pvCurrentN on the way out, the same way
        # it renames pvNPower -> pvPowerN. Without them nothing could ever map
        # a per-string V/I sensor, so the app's "354.0 V . 1.0 A" subline was
        # blank for every HA household (prod census 2026-08-18: present on
        # 65 of 65 edge-firmware inverters, 0 of 7 HA ones).
        "pv1Voltage",
        "pv2Voltage",
        "pv3Voltage",
        "pv4Voltage",
        "pv1Current",
        "pv2Current",
        "pv3Current",
        "pv4Current",
        "batteryCurrent",
        "batteryTemperature",
        "gridVoltageL1",
        "gridVoltageL2",
        "gridVoltageL3",
        "gridPowerL1",
        "gridPowerL2",
        "gridPowerL3",
        "gridFrequency",
        "loadPowerL1",
        "loadPowerL2",
        "loadPowerL3",
        "dailyPvEnergy",
        "dailyGridImportEnergy",
        "dailyGridExportEnergy",
        "dailyLoadEnergy",
        "dailyBatteryChargeEnergy",
        "dailyBatteryDischargeEnergy",
        "dailyGeneratorEnergy",
        "inverterTemperature",
        "dailyLossesEnergy",
        "loadFrequency",
        "gridCurrentL1",
        "gridCurrentL2",
        "gridCurrentL3",
        "loadCurrentL1",
        "loadCurrentL2",
        "loadCurrentL3",
    }
)

# Ordered (field, human label) list — the single source of truth for which
# canonical fields can be mapped to a Home Assistant sensor and what we call
# them in the UI. Both the manual pairing step (config flow) and the options
# (edit) flow derive their forms from this list, so the two can never drift.
# Grouped: battery → PV strings → grid → load → daily energy → temps.
# The key set MUST equal ALL_FIELDS (locked by tests/test_const.py).
MAPPABLE_FIELDS: list[tuple[str, str]] = [
    ("batterySoc", "Battery state of charge (%)"),
    ("batteryPower", "Battery power (W — positive = charging)"),
    ("batteryVoltage", "Battery voltage (V)"),
    ("batteryCurrent", "Battery current (A — positive = charging)"),
    ("batteryTemperature", "Battery temperature (°C)"),
    ("pv1Power", "PV string 1 power (W)"),
    ("pv2Power", "PV string 2 power (W)"),
    ("pv3Power", "PV string 3 power (W)"),
    ("pv4Power", "PV string 4 power (W)"),
    ("pv1Voltage", "PV string 1 voltage (V)"),
    ("pv1Current", "PV string 1 current (A)"),
    ("pv2Voltage", "PV string 2 voltage (V)"),
    ("pv2Current", "PV string 2 current (A)"),
    ("pv3Voltage", "PV string 3 voltage (V)"),
    ("pv3Current", "PV string 3 current (A)"),
    ("pv4Voltage", "PV string 4 voltage (V)"),
    ("pv4Current", "PV string 4 current (A)"),
    ("gridPower", "Grid power (W — positive = import)"),
    ("gridVoltageL1", "Grid voltage L1 (V)"),
    ("gridVoltageL2", "Grid voltage L2 (V)"),
    ("gridVoltageL3", "Grid voltage L3 (V)"),
    # Per-phase powers (L1..L3): the API folds these scalars into its
    # canonical phaseGridPowers / phaseLoads arrays at ingest — same path as
    # gridVoltageL1..L3 → phaseVoltages. L1 must be mapped for the fold to
    # apply (a gap would shift phases); L2/L3 optional.
    ("gridPowerL1", "Grid power L1 (W — positive = import)"),
    ("gridPowerL2", "Grid power L2 (W — positive = import)"),
    ("gridPowerL3", "Grid power L3 (W — positive = import)"),
    ("gridFrequency", "Grid frequency (Hz)"),
    ("loadPower", "Load power (W)"),
    ("loadPowerL1", "Load power L1 (W)"),
    ("loadPowerL2", "Load power L2 (W)"),
    ("loadPowerL3", "Load power L3 (W)"),
    ("dailyPvEnergy", "Daily PV production (kWh)"),
    ("dailyGridImportEnergy", "Daily grid import (kWh)"),
    ("dailyGridExportEnergy", "Daily grid export (kWh)"),
    ("dailyLoadEnergy", "Daily load energy (kWh)"),
    ("dailyBatteryChargeEnergy", "Daily battery charge energy (kWh)"),
    ("dailyBatteryDischargeEnergy", "Daily battery discharge energy (kWh)"),
    ("dailyGeneratorEnergy", "Daily generator energy (kWh)"),
    ("inverterTemperature", "Inverter temperature (°C)"),
    ("dailyLossesEnergy", "Daily losses energy (kWh)"),
    ("loadFrequency", "Load frequency (Hz)"),
    ("gridCurrentL1", "Grid current L1 (A)"),
    ("gridCurrentL2", "Grid current L2 (A)"),
    ("gridCurrentL3", "Grid current L3 (A)"),
    ("loadCurrentL1", "Load current L1 (A)"),
    ("loadCurrentL2", "Load current L2 (A)"),
    ("loadCurrentL3", "Load current L3 (A)"),
]

# Source tag on pushed readings. Must match a value in Plan A's reading
# `source` enum, currently {android-foreground, android-background,
# ios-foreground, ios-background, edge}. `edge` is the closest fit;
# `integrationType: home_assistant` on the device doc is what distinguishes
# the HA add-on from the ESP32 edge connector in analytics. (Server-side
# follow-up: add a dedicated `home_assistant` enum value.)
READING_SOURCE = "edge"

# Internal commands the add-on handles itself (never dispatched to an executor).
ADD_TRUSTED_KEY_COMMAND = "add_trusted_key"
REVOKE_TRUSTED_KEY_COMMAND = "revoke_trusted_key"
# Runtime cloud-endpoint switch (sub-project E) — operator flips a
# household's migratedToProd flag, the D4 Cloud Function enqueues this
# command, the integration validates the URL + reloads with new api_base.
# Internal (no admin signature required) — the URL allow-list IS the
# trust boundary, and the command can only retarget the integration to
# a Svitgrid-controlled endpoint.
SET_CLOUD_ENDPOINT_COMMAND = "set_cloud_endpoint"
# Island mode switch — flip cloud_ingest_enabled + seed the island key.
# Internal (no admin signature required) — the command is RBAC-gated at
# the API level (household owner/admin + paid entitlement); the command
# channel itself is the trust boundary here.
ENABLE_ISLAND_COMMAND = "enable_island"
DISABLE_ISLAND_COMMAND = "disable_island"
# Cloud-sender switch, orthogonal to island mode — flips cloud_ingest_enabled
# and NOTHING else (no island key seeded, no island routing touched).
#
# enable_island/disable_island each pin cloud ingest to one value, so between
# them they can only express "island without cloud" and "cloud without island".
# The third real state — data kept in Home Assistant AND uploaded, which is
# what a user who wants the app to work away from home needs — was reachable
# only at pairing time and could never be set or unset afterwards. This command
# is that missing control. Internal (no admin signature required), same trust
# posture as enable_island: RBAC-gated at the API level.
SET_CLOUD_INGEST_COMMAND = "set_cloud_ingest"

# Commands that act on the INTEGRATION itself rather than on one inverter.
#
# The local command endpoint (POST /api/svitgrid/commands) otherwise routes
# everything to a per-inverter WriteExecutor keyed on payload.inverterId — so
# an integration-level command has no executor and would 404. These are
# branched off before that lookup and applied to the ConfigEntry.
#
# This is what makes cloud sync recoverable over the LAN alone: re-enabling it
# through the cloud command channel needs the very connection that was turned
# off, which is circular in the one mode designed for the cloud being
# unreachable.
INTEGRATION_COMMANDS = frozenset({SET_CLOUD_INGEST_COMMAND})
# Direct-Modbus harvest connection change (SP-D follow-up) — updates
# ip/port/slaveId on a harvest_config entry. Internal (no admin signature
# required) — RBAC-gated at the API level (household owner/admin), same
# trust posture as set_cloud_endpoint/enable_island. Command-poller probes
# the new endpoint's TCP reachability before applying (fail-closed).
SET_HARVEST_CONFIG_COMMAND = "set_harvest_config"
# Switch an inverter between relay (edge-forwarded) and native (direct
# Modbus harvest) read sources. Internal (no admin signature required) —
# RBAC-gated at the API level (household owner/admin), same trust posture
# as set_harvest_config/set_cloud_endpoint. Command-poller probes the
# Modbus endpoint's TCP reachability before switching to native (fail-closed);
# switching back to relay does not probe.
SET_READ_SOURCE_COMMAND = "set_read_source"
# "Refresh now" — the app queues this (API inverter-refresh-now) to force an
# immediate reading, device-targeted like the edge firmware's poll_now. On HA
# it's a no-op ACK: the readings publisher already republishes on its own short
# cadence (floor 5s), so there's nothing to force — but it MUST be handled
# internally (no admin signature) so it doesn't fall through to the signature
# gate and get dropped as "unsigned", which would leave pendingCommandCount
# stuck > 0 and the poller re-fetching + re-skipping it every cycle.
POLL_NOW_COMMAND = "poll_now"

INTERNAL_COMMANDS = frozenset(
    {
        ADD_TRUSTED_KEY_COMMAND,
        REVOKE_TRUSTED_KEY_COMMAND,
        SET_CLOUD_ENDPOINT_COMMAND,
        ENABLE_ISLAND_COMMAND,
        DISABLE_ISLAND_COMMAND,
        SET_CLOUD_INGEST_COMMAND,
        SET_HARVEST_CONFIG_COMMAND,
        SET_READ_SOURCE_COMMAND,
        POLL_NOW_COMMAND,
    }
)

# Inverter-control commands dispatched to the configured executor.
# P2A A5: expanded from {set_battery_charge} to cover all 4 commands the
# API sends. YamlDispatcher routes each to a recipe-defined HA service.
# If the preset has no recipe for one, dispatcher raises UnsupportedCommandError
# and the poller ACKs as 'unsupported' (same outcome as before, but with
# a clearer error message).
DISPATCHABLE_COMMANDS = frozenset(
    {
        "set_battery_charge",
        "set_work_mode",
        "set_solar_sell",
        "set_grid_charge_toggle",
        "set_gen_force",
        "set_gen_port_mode",
        "set_sell_power_cap",
        # SMG II settings screen (Anenji/EASUN over an EyBond collector). A
        # name missing here is rejected before any executor is consulted,
        # and that failure looks identical to a broken executor -- see
        # tests/test_smg_settings_executor_wiring.py.
        "read_inverter_settings",
        "set_inverter_setting",
    }
)

# Pairing flow
PAIRING_POLL_INTERVAL_S = 2  # HA polls /status this often
PAIRING_MAX_POLL_DURATION_S = 300  # Stop polling after this; matches server TTL
# Prod is the canonical environment (2026-06-30 cutover). New installs pair
# against prod; existing installs keep their stored api_base and are moved by
# the server-issued set_cloud_endpoint command (see cloud_endpoint_handler).
DEFAULT_API_BASE = "https://api.svitgrid.app"

# ── Local readings store (Sub-project 1) ──────────────────────────────
READINGS_DB_SUBDIR = "svitgrid"
READINGS_DB_FILE = "readings.db"

BACKFILL_CAP_S = 48 * 3600  # don't backfill readings older than this
RAW_RETENTION_S = 14 * 86400  # prune raw rows older than 14 days
HOURLY_RETENTION_S = 2 * 365 * 86400  # prune hourly rows older than ~2 years
SENDER_TICK_S = 5  # sender wake interval when caught up
ROLLUP_INTERVAL_S = 3600  # roll-up + prune cadence
INGEST_BATCH_MAX = 50  # cloud batch endpoint cap

# Fields aggregated into long-term roll-ups. Raw rows keep the FULL payload
# (every field) for RAW_RETENTION_S; only these are summarized for the long tail.
INSTANTANEOUS_FIELDS = frozenset(
    {
        "batterySoc",
        "batteryPower",
        "batteryVoltage",
        "batteryCurrent",
        "batteryTemperature",
        "gridPower",
        "gridFrequency",
        "loadPower",
        "pvPower",
        "inverterTemperature",
        "loadFrequency",
        # Per-phase grid voltage — averaged into buckets so the dashboard Grid
        # Voltage chart has data in island mode (the mobile bucket mapper folds
        # these into `phaseVoltages`). Without them here, rollup.aggregate()
        # silently drops grid voltage even though the raw readings carry it.
        "gridVoltageL1",
        "gridVoltageL2",
        "gridVoltageL3",
    }
)
DAILY_COUNTER_FIELDS = frozenset(
    {
        "dailyPvEnergy",
        "dailyGridImportEnergy",
        "dailyGridExportEnergy",
        "dailyLoadEnergy",
        "dailyBatteryChargeEnergy",
        "dailyBatteryDischargeEnergy",
        "dailyGeneratorEnergy",
        "dailyLossesEnergy",
        # Harvested (SP-B direct-Modbus) for 1-phase Deye/Sunsynk models whose
        # register spec defines a generator-runtime register (address 83, see
        # packages/inverter_protocol/register-specs/*.json) — without this in
        # DAILY_COUNTER_FIELDS, rollup.aggregate() silently drops the decoded
        # field before it reaches readings_daily.energy.
        "dailyGeneratorRuntime",
    }
)
PEAK_FIELDS = frozenset({"pvPower", "loadPower"})

# ── auto-update ────────────────────────────────────────────────────────
GITHUB_REPO = "svitgrid/home-assistant-svitgrid"
GITHUB_LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_USER_AGENT = "svitgrid-ha-integration"

UPDATE_CHECK_INTERVAL_S = 12 * 3600  # how often to poll GitHub for a new release
RESTART_GUARD_WINDOW_S = 60  # defer auto-restart if a command ran this recently
CONF_AUTO_UPDATE = "auto_update"  # entry-options key; default True
