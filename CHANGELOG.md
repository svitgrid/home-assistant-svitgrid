# Changelog

## 0.8.1
- **Back off when the server rejects a reading.** If `/ingest/reading` returns a 4xx (e.g. the inverter is missing required sensors, so the payload is incomplete), the publisher now parks at the 30-minute ceiling interval instead of re-POSTing the same rejected payload every 60 seconds. It keeps retrying slowly and recovers automatically once the missing sensors are mapped — but stops hammering the API (and your network) with requests it will keep refusing. Transient 5xx / network errors still retry at the normal cadence. Most installs already skip incomplete payloads via local gating; this is the safety net for older configs and any future schema divergence.

## 0.8.0
- **Full inverter fleet in the pairing picker.** Added 19 preset profiles so the "Марка та модель інвертора" dropdown covers every model the app supports — Deye SG04LP1 / **SG05LP1-EU** / SG01LP1 16K / SG05LP3 (with battery/work-mode control), Deye GB-S20K and SUN-60K-G03 (read-only), all 8 Victron MultiPlus-II / Quattro-II, the 3 Huawei SUN2000 commercial strings, and both Solplanet ASW-LT. Deye low-voltage hybrids ship the force-charge / work-mode / solar-sell / grid-charge commands; HV (GB-S20K), grid-tie, Victron, Huawei and Solplanet ship read-only until their registers/entities are hardware-verified (their entity maps are best-guess starting points the user remaps in the config flow). A coverage test now fails CI if a supported model is missing a preset.

## 0.7.0
- **Per-phase grid and load power on 3-phase systems.** New mappable fields `gridPowerL1..L3` and `loadPowerL1..L3` (alongside the existing `gridVoltageL1..L3`); the API folds them into its canonical `phaseVoltages` / `phaseGridPowers` / `phaseLoads` arrays at ingest, lighting up the per-phase grid card and load split in the app. 3-phase Deye Solarman presets (SG04LP3 v4, SG01HP3 v2, SG01HP3-50K v2) now map them from the Solarman `deye_p3` profile sensors (`Grid Lx Power`, `Load Lx Power`). Existing installs: open **Configure → Edit inverter** and map the new fields (or re-apply the preset). Requires API with per-phase scalar folding (2026-06-10) — on older APIs the new fields are stripped server-side (harmless).

## 0.6.0
- Diagnostics sensor (status line + recent ingest log); ingest gate skips empty payloads with visible reason.

## 0.5.1
- **Fix: fresh pairings published no readings.** Since 0.5.0 the config entry is created at version 2, so the v1→v2 migration that wraps a pairing's `entity_map` into the `inverters` list never ran — leaving the entry with no inverters, so the readings publisher never started ("no inverters configured; nothing to publish"). Pairing finalize now writes the `inverters` list directly. Existing installs that added an inverter via **Configure → Add inverter** were unaffected; anyone who only paired needs this update (or can re-add the inverter from the Configure page).

## 0.5.0
- Multiple inverters per add-on: add inverters from the integration's **Configure** page (Add / Edit / Remove inverter). Each inverter publishes its own readings and is independently controllable. No second pairing code required. Requires the Svitgrid API endpoint POST /api/v1/ha/inverters.

## 0.4.1
- Per-string PV power sensors: fixed name mismatch between add-on (`pv1Power`) and API (`pvPower1`); add-on now emits canonical names so per-string values are correctly ingested.
