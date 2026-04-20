# Svitgrid — Home Assistant Integration

Custom component that connects a Home Assistant install to the [Svitgrid](https://svitgrid.com) cloud. Any inverter exposed to Home Assistant — Deye, Growatt, Anenji/EASUN/SMG-II, Victron, ESPHome-based, or anything else — becomes a Svitgrid-monitored device without running Svitgrid hardware.

## Status

**v0.1.0** — wire-protocol MVP. YAML config only; commands are acknowledged but not executed. The next release (v0.2.0) adds a config-flow UI and starts handling commands against real inverters.

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

## Troubleshooting

| Log line | Meaning | Fix |
|---|---|---|
| `Svitgrid bootstrap failed` | The mobile app didn't open a bootstrap window for this `device_id`, or it expired | Re-open the bootstrap window in the mobile app, restart HA |
| `signingKeyId is already registered with a different public key` | You changed `signing_key_id` but the server still has the old key registered | Pick a new `signing_key_id` — don't reuse IDs across rotations |
| `Too many bootstrap attempts` | 3 failed bootstraps in a row for this deviceId | Re-open the window in the mobile app |
| `B1 bootstrap mode: trusted-keys cache empty; sending signed ACK WITHOUT verifying admin signature` | Expected in v0.1.0 — the add-on doesn't yet fetch admin public keys. v0.2.0 fixes this. | Ignore |

## License

MIT.
