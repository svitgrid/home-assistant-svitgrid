# Changelog

## 0.13.0 — 2026-07-10

### Changed
- **Svitgrid panel history chart — pick your own time span.** The old fixed
  7d/30d/90d/365d buttons are replaced with a **Day / Month / Year / All-time**
  selector that changes the bar granularity to match: hourly bars for a day,
  daily bars for a month, monthly bars for a year, and yearly bars for all time.
  Step through periods with the ‹ / › arrows or tap the date to jump to any
  day, month or year. Monthly and yearly totals are rolled up locally from the
  add-on's stored daily history, so this works in island mode with no cloud
  round-trip. The Sources and Trends views remain for Month/Year/All-time; the
  Day view shows the hourly profile directly (the old tap-a-bar drill-down is
  no longer needed).

## 0.12.0 — 2026-07-07

### Added
- **Island mode — keep your energy data on your own Home Assistant.** The add-on
  can now serve the Svitgrid app directly over your LAN with no cloud round-trip:
  your live dashboard, charts, history and financial reports are computed on Home
  Assistant from its own stored readings. Turn it on/off from the app (or via the
  `enable_island` / `disable_island` commands); a cloud-sync toggle decides whether
  readings still flow to the cloud so forecasts, arbitrage and smart schedules keep
  working.
  - **Local history endpoint** (`GET /api/svitgrid/history`) serving hourly, daily
    and **5-minute** buckets computed live from raw readings — including the
    current, still-in-progress hour and day — with per-phase grid voltage preserved
    in the buckets. (Pairs with the Svitgrid app 1.0.12 Day-chart update.)
  - **Local financial settlement** (`settlement-input`) — pure per-hour
    import/export energy deltas with meter-reset handling, so the app's green-tariff,
    cooperative, business and active-consumer (РДН) reports compute locally.
  - **LAN trust keys** — add/revoke endpoints that pair the app's device key to the
    add-on over the local network (trust-on-first-use with proof-of-possession), so
    signed control commands work with no internet.
- **Configurable harvest cadence** (`GET`/`PUT /api/svitgrid/cadence`), with the
  polling floor lowered to 5 seconds.
- **Change the inverter connection in place** (`set_harvest_config`) — probes the
  new connection before applying it, and the change now persists across restarts.

### Fixed
- HA-Solarman battery sign is normalized on the local path — charging vs
  discharging is no longer inverted on the branded panel.
- The MQTT wake client is torn down before back-off, stopping a reconnect flood.
- Daily generator runtime is now included in the daily-counter rollup.
- Island enable/config changes reload the integration idempotently and no longer
  cancel the reading poller mid-apply.

## 0.11.0 — 2026-07-02

### Added
- Automatic updates: the integration now keeps itself on the latest GitHub release
  and restarts Home Assistant to apply it. Toggle it off under
  Settings → Devices & Services → Svitgrid → Configure → Settings.

## 0.10.1 — 2026-06-30

### Changed
- Default `api_base` for new installs is now `https://api.svitgrid.app` (prod), promoted from staging as part of the platform's 100%-to-prod cutover. Existing installs keep their stored value and are moved by the server-issued `set_cloud_endpoint` command (prod is already on the endpoint allow-list).

## 0.10.0 — 2026-06-28

### Added
- **Direct Modbus harvesting (no separate inverter integration needed).** The add-on can now talk to a supported inverter directly over its own protocol — Solarman V5 (data-logger sticks) or raw Modbus TCP (Victron, Huawei, Solplanet) — decoding readings itself instead of only relaying existing Home Assistant sensor entities. Set it up from the Svitgrid phone app: choose **Home Assistant → Direct**, run the usual inverter-discovery wizard (scan the inverter's IP, pick the model, set the Modbus slave id / optional port), and that connection is handed to the add-on through pairing. A **manual** "Set up direct inverter connection" option is also available in the integration's **Configure** menu for the no-phone path.
- **Reachability check before finishing setup.** When a direct-harvest connection is handed off, the add-on does one quick Modbus read at the given address before completing — if Home Assistant can't reach the inverter (wrong address, different network), setup stops with a clear error instead of silently collecting no data.
- **Inverter control over direct Modbus.** For supported Deye / Sunsynk / Sol-Ark hybrids, the add-on can now execute control commands directly — work mode, force generator, solar sell, grid-charge toggle, generator-port mode, max sell-power, and time-of-use battery-charge windows — each written and then read back to confirm it applied.

### Notes
- Only `deye_sg04lp3` is hardware-verified; every other model's register addresses are best-effort starting points. The reachability check proves connectivity, not that the register map is correct — verify a new model against live data before trusting its readings/writes.
- Requires the Svitgrid API with the direct-harvest pairing fields (register-spec endpoint + `harvestConfig` on pairing claim/finalize).

## 0.9.1 — 2026-06-25

### Fixed
- **Pre-flight probe before `set_cloud_endpoint` apply.** Mirrors the firmware sub-project D5 probe semantics: before mutating ConfigEntry + reloading, the integration now hits `/api/v3/me` on the target endpoint with the existing api_key. If the new endpoint can't authenticate the integration, the migration is rejected (ACK returns `reason="probe_failed"`) instead of mutating to a dead URL. Closes a cutover-breaker discovered during the v0.9.0 live smoke on 2026-06-25 — the HA Test household migrated successfully but every subsequent ACK to prod returned 401 because the household's trusted-keys list hadn't been synced. Layered defense: even after the sync gap is fixed server-side, future sync gaps WILL recur, so the probe stays.

### Added
- **Tier-1 telemetry (battery temperature/current, inverter temperature, grid frequency)** mapped from Deye hybrid Solarman presets and surfaced in the Details panel.
- **Tier-2 daily energy tiles (battery charged/discharged, generator)** collected from the same Deye Solarman presets and shown in the panel.

## 0.9.0 — 2026-06-25

### Added
- Runtime cloud-endpoint switch: the integration now handles a server-issued `set_cloud_endpoint` executor command, validating the target URL against an allow-list (`api-staging.svitgrid.app`, `api.svitgrid.app`), updating the ConfigEntry's `api_base`, and reloading the integration in-place. Mirrors the edge-device firmware behaviour from svitgrid sub-project D — lets Svitgrid migrate an HA-paired household between staging and prod without the user touching the HA UI.

### Changed
- Default `api_base` for new installs is now `https://api-staging.svitgrid.app` (was the raw Cloud Run hostname `https://api-334146986852.us-central1.run.app`). Existing installs keep their stored value.

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
