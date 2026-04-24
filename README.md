# Svitgrid — Home Assistant Integration

Custom component that connects a Home Assistant install to the [Svitgrid](https://svitgrid.com) cloud. Any inverter exposed to Home Assistant — Deye, Growatt, Anenji/EASUN/SMG-II, Victron, ESPHome-based, or anything else — becomes a Svitgrid-monitored device without running Svitgrid hardware.

## Status

**v0.2.0** — real inverter control (experimental). Trusted-keys lifecycle now works end-to-end: admin public keys from the Svitgrid mobile app reach the add-on via the bootstrap response and `add_trusted_key` commands; signed commands from admins are verified locally before acting. The first Tier 1 executor (`smg_ii`) ships as experimental (see below). YAML-only config; native config flow UI comes in v0.3.0.

## Install (HACS custom repository)

1. In HACS, open **Integrations** → three-dot menu → **Custom repositories**.
2. Add `https://github.com/svitgrid/home-assistant-svitgrid` with category **Integration**.
3. Install **Svitgrid**, restart Home Assistant.

## Configuration

### Step 1 — Get your `device_id` from the Svitgrid mobile app

Open the Svitgrid app → your household's inverter → **Add Home Assistant integration**. The app will show a `device_id` and open a 10-minute bootstrap window. Pick a stable `signing_key_id` (any unique-ish string, e.g. `ha-<your-household-name>`). Complete the bootstrap in HA within 10 minutes or re-open the window.

### Step 2 — Add this block to `configuration.yaml`

```yaml
svitgrid:
  api_base: "https://api-334146986852.us-central1.run.app"   # staging
  device_id: "<paste from mobile app>"
  signing_key_id: "<your chosen id, e.g. ha-home>"
  entity_map:
    # Required (all 6 must be present)
    batterySoc: sensor.my_inverter_battery_soc
    batteryPower: sensor.my_inverter_battery_power
    batteryVoltage: sensor.my_inverter_battery_voltage
    pv1Power: sensor.my_inverter_pv1_power
    gridPower: sensor.my_inverter_grid_power
    loadPower: sensor.my_inverter_load_power
    # Optional — include only those your HA integration exposes
    pv2Power: sensor.my_inverter_pv2_power
    dailyPvEnergy: sensor.my_inverter_daily_pv
    gridVoltageL1: sensor.my_inverter_grid_voltage_l1
```

**Sign conventions** (the server expects):
- `batteryPower`: positive = charging, negative = discharging
- `gridPower`: positive = importing from grid, negative = exporting

If your HA integration uses the opposite convention, wrap the sensor with a `template` sensor that flips the sign.

### Step 3 — Restart Home Assistant

Check Settings → System → Logs. You should see:
```
INFO (MainThread) [custom_components.svitgrid] Bootstrapping Svitgrid integration
INFO (MainThread) [custom_components.svitgrid] Svitgrid bootstrap complete
INFO (MainThread) [custom_components.svitgrid] Readings publisher started
INFO (MainThread) [custom_components.svitgrid] Command poller started
```

Within 30 seconds, readings will show up in the Svitgrid mobile app.

## Executors (experimental — v0.2.0)

Without an `executor:` block in `configuration.yaml`, the add-on runs read-only: it pushes readings and acknowledges commands as "unsupported". This is the safe default and matches v0.1.0 behavior.

To enable actual inverter control, add an `executor:` block alongside the top-level `svitgrid:` config:

```yaml
svitgrid:
  # ...existing fields...
  executor:
    type: "smg_ii"                   # or "read_only" (default)
    modbus_hub: "my_modbus_hub"      # the name: field from your modbus: block
    modbus_slave: 1                  # Modbus unit ID (usually 1 for SMG-II)
    battery_nominal_voltage: 48      # in volts; used for power→current conversion
```

### SMG-II executor (experimental — unverified on real hardware)

Covers EASUN SMG-II, ISolar SMG-II, POW-HVM, Anenji 4kW/6kW, and other SMG-II clones.

Currently handles one command: `set_battery_charge` (chargePowerLimitW field). Writes to Modbus register 233 (inverter-side charging current, 0.1 A units). Conversion: `register_value = round(chargePowerLimitW / battery_nominal_voltage / 0.1)`.

**⚠️ Experimental — not yet validated on real hardware.** The register map comes from community references (`syssi/esphome-smg-ii`) and has passed all unit + integration tests against Svitgrid's staging server, but no one has confirmed that writing to register 233 on real SMG-II hardware produces the expected effect. If you try it and something looks wrong, remove the `executor:` block and nothing further happens; the add-on reverts to read-only. Please [open an issue](https://github.com/svitgrid/home-assistant-svitgrid/issues) with what you observed — your feedback is how we validate this.

Future releases will add Deye, Growatt, and more SMG-II commands (sell-power cap, charge window, working mode).

## Troubleshooting

| Log line | Meaning | Fix |
|---|---|---|
| `Svitgrid bootstrap failed` | The mobile app didn't open a bootstrap window for this `device_id`, or it expired | Re-open the bootstrap window in the mobile app, restart HA |
| `signingKeyId is already registered with a different public key` | You changed `signing_key_id` but the server still has the old key registered | Pick a new `signing_key_id` — don't reuse IDs across rotations |
| `Too many bootstrap attempts` | 3 failed bootstraps in a row for this deviceId | Re-open the window in the mobile app |
| `Skipping command — signingKeyId X not in trusted keys` | Admin key hasn't propagated yet. In v0.2.0 this usually means your HA installed BEFORE the admin key was registered in the household; re-bootstrap to pull the current trusted keys. | Restart HA, or re-add the integration via the mobile app |
| `Command X (set_battery_charge) dispatchable but no executor configured` | The add-on received a signed command from the Svitgrid app but you haven't added an `executor:` block to `configuration.yaml` | Add the executor block (see above) or leave it out to stay read-only |

## License

MIT.
