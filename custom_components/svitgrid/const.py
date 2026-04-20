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
