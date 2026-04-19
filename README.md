# Svitgrid — Home Assistant Integration

Custom component that connects a Home Assistant install to the Svitgrid cloud, turning any HA-exposed solar inverter into a Svitgrid-monitored device.

## Status

Pre-release (v0.1.0). YAML config only, no UI yet. Commands are acknowledged but not executed — the add-on proves the wire protocol, next release adds inverter control.

## Install (HACS custom repository)

1. HACS → Integrations → three-dot menu → Custom repositories.
2. Add `https://github.com/svitgrid/home-assistant-svitgrid` with category *Integration*.
3. Install "Svitgrid", restart Home Assistant.
4. See [configuration](#configuration).
