"""Constants for the Svitgrid custom component."""

DOMAIN = "svitgrid"

# Timing (seconds)
READINGS_INTERVAL_S = 10
COMMAND_POLL_INTERVAL_S = 5
# Upper bound for the server-driven command-poll cadence (10 min). MQTT-wake
# delivers commands instantly; the HTTP poll is the slow fallback, so the
# server can park us up to this long. Matches the edge firmware / mobile
# clamp ceiling (600_000 ms).
COMMAND_POLL_CEILING_S = 600

# HA Store
STORAGE_KEY = "svitgrid"
STORAGE_VERSION = 1

# Required canonical entity-map fields
REQUIRED_FIELDS = frozenset(
    {"batterySoc", "batteryPower", "batteryVoltage", "pv1Power", "gridPower", "loadPower"}
)

# The subset of API-required reading fields that MUST be sourced from a mapped
# HA entity before we POST. `pvPower` is API-required too, but the readings
# publisher defaults it to 0 for battery-only / no-solar systems (see
# readings_publisher.gate_payload), so it is intentionally NOT listed here.
CORE_PAYLOAD_FIELDS = frozenset(
    {"batterySoc", "batteryPower", "batteryVoltage", "gridPower", "loadPower"}
)

# All recognized canonical fields (required + optional)
ALL_FIELDS = REQUIRED_FIELDS | frozenset(
    {
        "pv2Power",
        "pv3Power",
        "pv4Power",
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
        "inverterTemperature",
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
    ("inverterTemperature", "Inverter temperature (°C)"),
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
INTERNAL_COMMANDS = frozenset({ADD_TRUSTED_KEY_COMMAND, REVOKE_TRUSTED_KEY_COMMAND})

# Inverter-control commands dispatched to the configured executor.
# P2A A5: expanded from {set_battery_charge} to cover all 4 commands the
# API sends. YamlDispatcher routes each to a recipe-defined HA service.
# If the preset has no recipe for one, dispatcher raises UnsupportedCommandError
# and the poller ACKs as 'unsupported' (same outcome as before, but with
# a clearer error message).
DISPATCHABLE_COMMANDS = frozenset({
    "set_battery_charge",
    "set_work_mode",
    "set_solar_sell",
    "set_grid_charge_toggle",
})

# Pairing flow
PAIRING_POLL_INTERVAL_S = 2          # HA polls /status this often
PAIRING_MAX_POLL_DURATION_S = 300    # Stop polling after this; matches server TTL
DEFAULT_API_BASE = "https://api-334146986852.us-central1.run.app"
