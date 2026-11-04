# Svitgrid for Home Assistant

A custom integration that connects Home Assistant to
[Svitgrid](https://svitgrid.app), a solar monitoring app. Home Assistant
becomes the station's collector: it reads your inverter and sends the readings
to the Svitgrid app, with no Svitgrid hardware.

It is a custom integration, not an add-on. After you install it, look for it
under **Settings → Devices & services**, not under **Add-ons**.

The full guide, with screenshots, is
[Home Assistant in the Svitgrid docs](https://svitgrid.app/docs/home-assistant).

## What it does

- **Reads your inverter**, either from sensors you already have in Home
  Assistant or over Modbus directly.
- **Controls your inverter** from the app: battery charge, work mode, solar
  sell, grid charge, generator force, generator port mode, and export limit.
- **Keeps data locally.** In local mode, Home Assistant stores readings and
  history and runs your schedule, with no internet needed.

The integration ships ready-made mappings for many models, including Deye,
Victron, Anenji/EASUN, Huawei, Solplanet, and Solis. If your model has no
mapping, you can map the entities yourself from the integration's options.

## Before you start

- Home Assistant 2024.1.0 or later, with [HACS](https://hacs.xyz) installed.
- The Svitgrid app on your phone, signed in.
- Your phone on the same Wi-Fi network as Home Assistant. The app then finds
  Home Assistant and fills in the pairing code for you.

## Install

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/svitgrid/home-assistant-svitgrid` with the type
   **Integration**.
3. Install **Svitgrid**.
4. Restart Home Assistant.

Install through HACS rather than copying the folder into `custom_components`.
HACS is what offers you new releases.

## Set up

1. In Home Assistant, go to **Settings → Devices & services**, click **Add
   integration**, and choose **Svitgrid**. Home Assistant shows a
   six-character pairing code. Leave the window open: the code expires in 5
   minutes.
2. In the Svitgrid app, start adding a station. When the app asks how to
   collect your data, choose **Home Assistant add-on**. Check that the pairing
   code matches, choose where your data lives, and tap **Create**.
3. Back in Home Assistant, the window asks you to connect a Svitgrid account.
   Click **Approve** only if you entered the code in the app yourself.

The app waits for the first reading and then opens your dashboard. This takes
about a minute.

## Troubleshooting

| What you see | What to do |
|---|---|
| **The pairing code expired.** | Close the window and add the integration again. Home Assistant shows a new code. |
| **The Svitgrid app paired this Home Assistant without an inverter profile.** | Update the Svitgrid app, then start the pairing again. |
| **The Svitgrid cloud refused the pairing.** | Start the pairing again for a new code. The message gives the reason. |
| **Could not reach the Svitgrid cloud.** | Check that Home Assistant has internet access. |
| The dashboard stays empty after setup. | Check that the Svitgrid entry under **Settings → Devices & services** shows no error, then check **Settings → System → Logs** for `custom_components.svitgrid`. |

If something else goes wrong,
[open an issue](https://github.com/svitgrid/home-assistant-svitgrid/issues)
with the log lines from `custom_components.svitgrid`.

## Updates

HACS shows each new release under **Settings → Updates**, with its release
notes. You choose when to install it and when to restart Home Assistant. See
[CHANGELOG.md](CHANGELOG.md) for what each release changes.

## Older YAML setups

Installs configured before pairing existed use a `svitgrid:` block in
`configuration.yaml`. Those installs keep working. New installs use the
pairing above, and this README no longer documents the YAML block.

## License

MIT.
