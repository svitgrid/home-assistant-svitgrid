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
from dataclasses import dataclass, field

from .live_gate import LiveControl, parse_live_control

_LOGGER = logging.getLogger(__name__)


@dataclass
class MqttControlState:
    """Plain mutable holder, shared by reference between the wake client
    (writer, via `apply_config`) and the reading sender (reader, Task 3)."""

    mqtt_primary: bool = False
    interval_s: int | None = None
    bootstrapped: bool = False
    # Live or interval reporting, set by every channel that carries the
    # server's cadence: this config push, the HTTP ingest ack and the command
    # poll response (apply_cadence).
    live: LiveControl = field(default_factory=LiveControl)


def apply_cadence(state: MqttControlState, data: dict) -> None:
    """Apply the cadence fields of one server message (`ingestIntervalMs`,
    `ingestMode`, `live`) to `state`. A message without them changes nothing."""
    state.live = parse_live_control(data, state.live)
    if state.live.interval_s is not None and state.live.interval_s > 0:
        state.interval_s = state.live.interval_s


def effective_interval_s(state: MqttControlState) -> int | None:
    """How often the read loops sample: every `checkSec` in Live (the sender's
    gate decides what leaves), else the server's ingest interval."""
    if state.live.live:
        return state.live.params.check_s
    return state.interval_s


def apply_config(
    state: MqttControlState,
    payload: str | bytes | dict,
) -> None:
    """Parse a `devices/{deviceId}/config` MQTT payload and update `state`
    in place. Never raises — a malformed/non-JSON/non-dict payload, or one
    with fields of the wrong type, is a no-op (missing/invalid fields leave
    the current value as-is). Does not touch `state.bootstrapped`.
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

    apply_cadence(state, data)
