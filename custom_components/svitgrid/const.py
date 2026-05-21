"""Constants for the Svitgrid custom component."""

DOMAIN = "svitgrid"

# Timing (seconds)
READINGS_INTERVAL_S = 10
COMMAND_POLL_INTERVAL_S = 5

# HA Store
STORAGE_KEY = "svitgrid"
STORAGE_VERSION = 1

# Required canonical entity-map fields
REQUIRED_FIELDS = frozenset(
    {"batterySoc", "batteryPower", "batteryVoltage", "pv1Power", "gridPower", "loadPower"}
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
        "gridFrequency",
        "dailyPvEnergy",
        "dailyGridImportEnergy",
        "dailyGridExportEnergy",
        "dailyLoadEnergy",
        "inverterTemperature",
    }
)

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
DISPATCHABLE_COMMANDS = frozenset({"set_battery_charge"})

# Pairing flow
PAIRING_POLL_INTERVAL_S = 2          # HA polls /status this often
PAIRING_MAX_POLL_DURATION_S = 300    # Stop polling after this; matches server TTL
DEFAULT_API_BASE = "https://api-334146986852.us-central1.run.app"
