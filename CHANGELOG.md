# Changelog

## 0.5.1
- **Fix: fresh pairings published no readings.** Since 0.5.0 the config entry is created at version 2, so the v1→v2 migration that wraps a pairing's `entity_map` into the `inverters` list never ran — leaving the entry with no inverters, so the readings publisher never started ("no inverters configured; nothing to publish"). Pairing finalize now writes the `inverters` list directly. Existing installs that added an inverter via **Configure → Add inverter** were unaffected; anyone who only paired needs this update (or can re-add the inverter from the Configure page).

## 0.5.0
- Multiple inverters per add-on: add inverters from the integration's **Configure** page (Add / Edit / Remove inverter). Each inverter publishes its own readings and is independently controllable. No second pairing code required. Requires the Svitgrid API endpoint POST /api/v1/ha/inverters.

## 0.4.1
- Per-string PV power sensors: fixed name mismatch between add-on (`pv1Power`) and API (`pvPower1`); add-on now emits canonical names so per-string values are correctly ingested.
