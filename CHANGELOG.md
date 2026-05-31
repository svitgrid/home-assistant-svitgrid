# Changelog

## 0.5.0
- Multiple inverters per add-on: add inverters from the integration's **Configure** page (Add / Edit / Remove inverter). Each inverter publishes its own readings and is independently controllable. No second pairing code required. Requires the Svitgrid API endpoint POST /api/v1/ha/inverters.

## 0.4.1
- Per-string PV power sensors: fixed name mismatch between add-on (`pv1Power`) and API (`pvPower1`); add-on now emits canonical names so per-string values are correctly ingested.
