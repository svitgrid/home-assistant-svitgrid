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

# Source tag on pushed readings (accepted by Plan A's readings endpoint).
# The integrationType field on the device doc distinguishes HA from ESP32 already.
READING_SOURCE = "edge-device"

# Internal commands the add-on handles itself (never dispatched to an executor).
ADD_TRUSTED_KEY_COMMAND = "add_trusted_key"
REVOKE_TRUSTED_KEY_COMMAND = "revoke_trusted_key"
INTERNAL_COMMANDS = frozenset({ADD_TRUSTED_KEY_COMMAND, REVOKE_TRUSTED_KEY_COMMAND})

# Inverter-control commands dispatched to the configured executor.
DISPATCHABLE_COMMANDS = frozenset({"set_battery_charge"})
