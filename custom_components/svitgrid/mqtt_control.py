"""Shared MQTT control state, updated from `devices/{deviceId}/config` pushes.

Mirrors the ESP32 edge firmware's `apply_control_config` (W4a/W4b): the server
pushes `{"mqttPublishReadings": bool, "ingestIntervalMs": number}` over MQTT
whenever the device's config changes (tier change, cadence change, etc.), and
this module is the single place that parses that payload into a plain,
mutable, shared-by-reference state object.

Unlike the edge firmware, we are lenient about missing fields: a field that
is absent from the payload leaves the current value untouched rather than
resetting it. HA's config-push (`buildDeviceConfig`/`buildHarvesterConfig`)
always includes both fields today, but there is no reason to punish a future
partial payload by silently disabling MQTT-primary or forgetting the cadence.

`apply_config` is deliberately paranoid: it is invoked from the MQTT
wake-bell's `on_message` callback, so a malformed/attacker-controlled/corrupt
payload must NEVER raise into that path. Every failure mode is a no-op.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass
class MqttControlState:
    """Plain mutable holder, shared by reference between the wake client
    (writer, via `apply_config`) and the reading sender (reader, Task 3)."""

    mqtt_primary: bool = False
    interval_s: int | None = None
    bootstrapped: bool = False


def apply_config(
    state: MqttControlState,
    payload: str | bytes | dict,
    on_update_check=None,
) -> None:
    """Parse a `devices/{deviceId}/config` MQTT payload and update `state`
    in place. Never raises — a malformed/non-JSON/non-dict payload, or one
    with fields of the wrong type, is a no-op (missing/invalid fields leave
    the current value as-is). Does not touch `state.bootstrapped`.

    `on_update_check` (v0.15.3): optional zero-arg callback fired when the
    payload carries `"updateCheck": true` — the server-side "check for an
    integration update NOW" nudge (wired to the update coordinator's refresh
    in __init__). Strictly `is True` (no truthy coercion), and the callback
    is fail-open: an exception inside it is swallowed and never blocks the
    sibling config fields from applying.
    """
    try:
        data = payload if isinstance(payload, dict) else json.loads(payload)
    except Exception:  # noqa: BLE001 — malformed payload is a no-op, never raise
        _LOGGER.debug("mqtt config: payload not valid JSON, ignoring", exc_info=True)
        return

    if not isinstance(data, dict):
        _LOGGER.debug("mqtt config: payload is not a JSON object, ignoring")
        return

    if "mqttPublishReadings" in data:
        state.mqtt_primary = bool(data["mqttPublishReadings"])

    if "ingestIntervalMs" in data:
        interval_ms = data["ingestIntervalMs"]
        if isinstance(interval_ms, (int, float)) and not isinstance(interval_ms, bool) and interval_ms > 0:
            state.interval_s = int(interval_ms / 1000)

    if data.get("updateCheck") is True and on_update_check is not None:
        try:
            on_update_check()
        except Exception:  # noqa: BLE001 — nudge is best-effort, never break config apply
            _LOGGER.debug("mqtt config: on_update_check callback raised", exc_info=True)
