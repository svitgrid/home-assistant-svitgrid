# Changelog

## Unreleased

### Fixed
- **Some inverter models paired successfully, showed as configured in the app,
  and then reported nothing at all — indefinitely.** No error, no warning,
  nothing in the Home Assistant log above debug level. It affected 31 of the 70
  models Svitgrid now publishes: every SRNE, Megarevo, KSTAR and Afore, plus
  Solis 30K-5G. Three separate causes, all fixed together.

  Two of the decoding rules the cloud sends (`grid_sign_normalize` and
  `battery_power_from_vi`) were missing from this add-on, and hitting an unknown
  rule made the whole poll throw — which the harvest loop caught and hid. In the
  same change, 32-bit meter readings now honour the word order the model
  declares (`lowWordFirst`); the two Megarevo models put the low half first, and
  without this their six daily-energy totals came out word-swapped — a large,
  believable, wrong number rather than an error. These two had to ship together:
  fixing only the first would have turned a loud failure into a quiet one.

  Separately, models that read their data from *input* registers (function code
  4) were being read from *holding* registers instead — a different bank
  entirely. That is 16 Afore, 3 KSTAR and Solis 30K-5G, on 100% of their
  readings. The add-on now issues the right request, and refuses to guess if it
  ever meets a function code it cannot serve.

- **A model this add-on cannot decode, or one the cloud has no data for, now
  says so.** Previously it looked exactly like an inverter that was working
  fine: setup completed, the integration loaded, and nothing was ever published.
  The register spec is now validated when it is loaded, and any problem appears
  on the *Svitgrid Diagnostics* sensor and at error level in the log, naming the
  model. A spec that never arrives escalates to a warning after a few polls.

- **Solar generation showed 0 W forever on models that report one combined PV
  total instead of per-string power** — the four Huawei SUN2000 variants, and
  now `swatten_sih_th_10k` too. The add-on's payload builder only ever summed
  `pv1Power`..`pv6Power` into the `pvPower` figure the app displays; a model
  that reads a single `totalPvPower` register never had any of those fields to
  sum, so `pvPower` was silently defaulted to zero on every reading. It now
  falls back to the combined reading when no per-string figure is available,
  and still prefers the finer per-string sum when both are present.

### Fixed
- **Home Assistant logged a wall of "Detected blocking call" warnings whenever
  the Svitgrid integration was set up or reloaded.** Seven at a time, naming
  `listdir`, `read_text`, `load_default_certs` and `set_default_verify_paths`,
  all pointing at `mqtt_wake.py`. They were not cosmetic: both offenders ran
  directly on Home Assistant's event loop, so everything else in Home Assistant
  — every integration, the whole interface — was frozen for as long as they
  took. The wake-bell loop is started as an *eager* background task, meaning its
  first stretch of work runs immediately and inline rather than being scheduled
  for later, and that stretch contained two pieces of disk I/O: importing the
  MQTT library for the first time (which walks every installed package) and
  setting up TLS (which reads the system certificate store). Both now run on a
  worker thread. The TLS one also happened on every reconnect, not just at
  startup. Nothing about the MQTT connection itself changes.

### Fixed
- **Control actions stopped working after a restart, with no error anywhere.**
  Changing your charge schedule, battery settings or grid-charge switch from the
  app appeared to do nothing: the app sat spinning next to the setting you had
  just changed, the inverter never changed, and the only trace was a warning in
  the Home Assistant log — `Skipping command … not in trusted keys (cache has
  0)`. Your phone *was* approved; the integration had simply forgotten it.

  Two causes, both fixed. The list of approved phones was read back under the
  wrong name when the integration started, so it always came up empty; and
  every restart or reload overwrote the approved phones with the (usually
  shorter) list captured at the moment you first paired. Anything approved
  afterwards was lost the next time Home Assistant restarted, the add-on
  updated, or you flipped local mode or cloud sync — which is why control could
  work for a while and then quietly stop.

  Readings were never affected: telemetry is authenticated differently and kept
  arriving throughout, which is what made this so hard to spot from the app.

### Added
- **Svitgrid now repairs its own list of approved phones.** Until now that list
  could only ever be *pushed* to your Home Assistant, and nothing re-sent it —
  so once it was lost, it was lost for good. The integration now asks Svitgrid
  for the approved list when it starts, and again if a control instruction
  arrives signed by a phone it doesn't recognise. A household already stuck in
  the state described above therefore fixes itself once this version is
  installed; no support request, no re-pairing.

  Two deliberate limits: a failed check (no internet, older Svitgrid server)
  leaves your existing list untouched rather than clearing it, and an answer of
  "no approved phones at all" is ignored for the same reason — removing a phone
  still works the way it always has, through the app.

## 0.21.0 — 2026-08-05

### Added
- **You are now told when this integration is still waiting to be approved.**
  When you add Svitgrid to a home that already has an approved phone, the new
  integration's key has to be approved from the app before Svitgrid will accept
  any control instruction from it — but nothing ever said so. Everything looked
  perfectly healthy (your readings kept arriving as normal) right up until the
  moment a control action mysteriously did nothing. The **Diagnostics** sensor on
  the Svitgrid device page now says `waiting for approval — open the Svitgrid app
  -> Household -> Your devices and approve this integration`, and the message
  clears itself the moment you approve. Readings were never affected by this and
  still aren't.

  Requires the matching server release; on older servers nothing changes and no
  warning is shown.

## 0.20.1 — 2026-08-05

### Fixed
- **One unavailable sensor no longer hides all of your data.** If a single
  sensor you had mapped stopped reporting — a battery state-of-charge that
  briefly went unavailable was enough — the integration threw away the *whole*
  reading, including perfectly good solar, grid and load values. Because
  readings are stored locally before they are uploaded, this blanked both the
  Svitgrid panel inside Home Assistant *and* the app at the same time, with no
  visible error: sensors looked fine on your own dashboards, and Svitgrid simply
  showed nothing. Battery state of charge is no longer treated as essential (it
  is optional for Svitgrid — the app shows "Calculating" while it is unknown),
  so a reading now goes through whenever the values Svitgrid genuinely requires
  are there.

### Added
- **Mapped sensors that go quiet are now named instead of silently ignored.**
  A sensor you mapped that stops producing a value used to just vanish from the
  data — a dead PV sensor read as a flat 0 W with nothing anywhere explaining
  why. The **Diagnostics** sensor on the Svitgrid device page now says which
  field and which entity is affected (`ok — but no value from mapped sensor(s):
  pv1Power (sensor.victron_pv_power)`), and one matching warning is written to
  the Home Assistant log each time that set changes.

## 0.20.0 — 2026-08-04

### Added
- **Cloud sync can now be switched on again from your phone on the same WiFi,
  with no internet involved.** Re-enabling uploads previously required a command
  delivered *through the cloud* — the very connection that was switched off — so
  a household that turned cloud sync off could be left unable to turn it back on.
  The Svitgrid app now sends that change straight to the add-on over your local
  network. Same security as every other local command: it only works with a
  paired device's local key and a valid signature, so being on the WiFi is not
  enough on its own.

## 0.19.0 — 2026-08-04

### Fixed
- **"Keep data in Home Assistant" no longer forces cloud sync off for good.**
  Choosing local (island) mode always switched cloud upload off, whatever you
  picked alongside it — so "store it here *and* send it to the cloud" could only
  ever be set while first pairing, and never changed afterwards. If you ended up
  local-only, the app showed nothing once you left the house, and the only ways
  out were to give up local mode or re-pair from scratch. Turning local mode on
  now respects your cloud-sync choice, and cloud sync can be switched on or off
  at any time from the app without re-pairing or disturbing local access.

  Your data was never lost while this was happening: the add-on kept collecting
  and storing everything in Home Assistant throughout — it simply wasn't
  uploading, so only the app's away-from-home view was affected. Once cloud sync
  is switched back on, the stored backlog (up to 48h) uploads on its own.

### Added
- **`set_cloud_ingest` command.** Turns the cloud sender on or off on its own,
  without touching your local-mode key or local access. This is what makes the
  setting changeable after pairing. Older add-ons don't understand it, so the
  app only offers the switch once you're on 0.19.0 or newer.

## 0.18.0 — 2026-07-25

### Added
- **Settings sync: TOU/work-mode register mirror for Solarman/Modbus inverters
  (5-min cadence, hash-gated).** Direct-harvest inverters (native Solarman V5
  or Modbus TCP, not the relay/HA-entity path) now mirror their TOU/work-mode
  config register block to the cloud every 5 minutes, so the app can show and
  reason about the inverter's actual on-device schedule. A hash of the
  register block is cached per inverter; an unchanged block is skipped, and a
  30-minute heartbeat re-uploads even unchanged registers so the cloud copy
  never silently goes stale. A failed upload never marks the cache — the next
  cycle retries with the same block. Preset/relay-only and MQTT-only
  inverters are never read or uploaded for this.

## 0.17.1 — 2026-07-23

### Fixed
- **Failed uploads now back off instead of retrying every 5 seconds.** If the
  cloud rejected a reading — or the integration's API key had been revoked
  (for example after re-pairing from another device) — the uploader retried
  the same request every 5 seconds around the clock, silently generating tens
  of thousands of pointless requests per day. Now: a rejected API key pauses
  uploads for 15 minutes at a time (with a log message telling you to re-pair
  if it persists), other failures back off progressively (10s doubling to a
  5-minute cap, resetting on the first success), and a reading the server has
  permanently refused ten times is set aside instead of blocking newer
  readings behind it forever. Temporary server outages are unaffected:
  readings still queue locally and the full backlog (up to 48h) uploads when
  the connection recovers.

## 0.17.0 — 2026-07-20

### Added
- **Remove a device's local-mode access.** The Svitgrid app now lists every
  device holding local (island) access and lets you remove any of them.
  Previously a device that had ever paired kept access permanently — there was
  no way to revoke it. A device paired before 0.16.0 appears as an
  unidentified entry and can also be removed.

## 0.16.0 — 2026-07-20

### Changed
- **Island mode: each device keeps its own key.** Setting up island mode on a
  second device no longer signs the first device out.

### Fixed
- **Charts now use your Home Assistant timezone instead of UTC.** The Day
  chart's hourly profile was drawn on UTC hours while its axis reads as local
  time, so a household at UTC+3 saw its whole curve shifted three hours early —
  a solar peak at 08:00 rendered at 05:00. The same mismatch meant "a day" was
  fetched as the UTC day: the first three local hours of every day were missing
  from the chart and three hours of the next morning were folded onto its right
  edge. The panel now asks for your LOCAL midnight-to-midnight window, and
  each hourly/5-minute bucket carries the local hour it belongs to.

  The mobile app is unaffected by this release: it already compensates for the
  UTC window on its side, so the local window is opt-in (`local_day=1`) and
  only the panel opts in. Switching the endpoint under the app would have
  blanked its charts every night between 00:00 and 03:00, and the add-on
  auto-updates long before an app release could catch up. The new `localHour`
  field is sent to every client regardless, so the app can adopt it on its own
  schedule.
- **Month and Year bars bucket energy to local days.** `readings_daily` was
  keyed by UTC date, misattributing a few hours of energy across midnight on
  every bar. Existing rows are re-keyed once, in place, from the retained
  hourly data; days too old to re-derive are left untouched rather than
  dropped. One known cosmetic seam: at the very bottom of your history (the
  ~2-year hourly-retention floor) a small extra bar remains, overlapping the
  first re-keyed day by your UTC offset — 3 hours for Ukraine. Removing it
  would mean deleting real data that can no longer be rebuilt, so it stays.
- **DST days are handled exactly.** A local day is 23, 24, or 25 hours long
  across a transition, and the windows are computed from local midnights so no
  hour is lost or double-counted. On a fall-back day the repeated local hour is
  averaged in the Day chart rather than summed (it used to render as a 2x
  spike).

## 0.15.2 — 2026-07-15

### Fixed
- **"Refresh now" no longer gets silently dropped.** The app's poll-now
  ("Refresh now") command has no admin signature, so the command poller was
  discarding it as an "unsigned non-internal command" — which also left it
  un-acknowledged, so the device's pending-command counter stayed above zero and
  the poller re-fetched and re-skipped the same command every cycle. `poll_now`
  is now handled internally as a no-op that acknowledges success: the readings
  publisher already republishes on its own short cadence (≥5s), so there's
  nothing to force, but the acknowledgement clears the counter and stops the
  skip-loop. (No on-demand re-read yet; that would require waking the readings
  loop — tracked as a follow-up.)

## 0.15.1 — 2026-07-15

### Fixed
- **First reading now lands within seconds of setup, not minutes.** On a fresh
  install (or any restart) the add-on starts on the 5-minute idle cadence, and
  the readings publisher used to spend that whole window collecting samples
  before storing its first reading — so the app's "Waiting for data from Home
  Assistant" screen could sit there for ~5 minutes. The publisher now always
  captures and stores an immediate single-snapshot reading on its first pass
  (mirroring the edge connector's boot reading), then settles into the normal
  cadence on later iterations. While still waiting for that first reading,
  incomplete snapshots (source sensors not yet populated — common for the first
  few seconds after an HA restart) are retried every few seconds instead of
  parking a full cadence, so data appears as soon as the sensors come online.
  After the first reading the add-on also adopts the server's real cadence
  within seconds (it no longer commits to the cold-start default while the
  server response is still in flight), so readings keep flowing without a gap.

## 0.15.0 — 2026-07-15

### Changed
- **Readings now go over MQTT first, HTTPS as a safety net.** The add-on
  publishes each reading to the Svitgrid broker and waits for the broker to
  confirm it (QoS-1 acknowledgement); only readings the broker does **not**
  confirm are sent over HTTPS instead. This cuts redundant cloud round-trips
  while guaranteeing nothing is lost — any unconfirmed, disconnected, or
  timed-out reading still falls back to the HTTPS upload. The first upload
  after each restart always goes over HTTPS to bootstrap control.
- **Control now arrives over MQTT.** The reporting cadence and the
  MQTT-primary enable flag are delivered on `devices/{id}/config` (in addition
  to the existing HTTPS-response path), so a healthy install no longer needs
  the periodic HTTPS call to learn its settings.
- Off by default; Svitgrid enables MQTT-primary per install (same allowlist as
  the edge connector). Island mode is unaffected (publishing rides the cloud
  sender, which never runs in island mode).

## 0.14.0 — 2026-07-13

### Added
- **Faster cloud reads via MQTT.** The add-on now also publishes each
  cloud-sent reading to the Svitgrid broker (in addition to the normal HTTPS
  upload), so the app can serve fresh data straight from the cache instead of
  waiting on the periodic sync. It's additive and best-effort — if the broker
  is briefly unreachable, the HTTPS upload is still the source of truth, so
  nothing is lost. Off by default; Svitgrid enables it per install.
- **Island mode is unaffected.** Publishing rides on the cloud sender, which
  never runs in island mode, so a local-only install sends nothing to the
  broker.

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
- **Toggle series on the hourly chart.** Click a legend entry (Solar / House /
  Battery / Grid) to hide or show that line; the chart rescales to what's
  visible and your choice is remembered per browser.
- **Per-metric bar colors.** The Month/Year/All-time Energy bar chart now colors
  bars by the selected metric (Generated amber, Consumed violet, Imported blue,
  Exported cyan, Battery charge/discharge green, Losses gray) instead of a flat
  gold, matching the mobile app's palette.

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
