"""Tests for mqtt_control.MqttControlState / apply_config."""

from __future__ import annotations

import json

from custom_components.svitgrid.mqtt_control import (
    MqttControlState,
    apply_config,
    effective_interval_s,
)


def test_defaults():
    state = MqttControlState()
    assert state.mqtt_primary is False
    assert state.interval_s is None
    assert state.bootstrapped is False


def test_valid_full_payload_sets_both_fields():
    state = MqttControlState()
    apply_config(state, json.dumps({"mqttPublishReadings": True, "ingestIntervalMs": 30000}))
    assert state.mqtt_primary is True
    assert state.interval_s == 30


def test_valid_payload_as_bytes():
    state = MqttControlState()
    apply_config(
        state, json.dumps({"mqttPublishReadings": True, "ingestIntervalMs": 60000}).encode()
    )
    assert state.mqtt_primary is True
    assert state.interval_s == 60


def test_valid_payload_as_dict():
    state = MqttControlState()
    apply_config(state, {"mqttPublishReadings": True, "ingestIntervalMs": 120000})
    assert state.mqtt_primary is True
    assert state.interval_s == 120


def test_malformed_json_is_noop():
    state = MqttControlState()
    state.mqtt_primary = True
    state.interval_s = 45
    apply_config(state, "not json{{{")
    assert state.mqtt_primary is True
    assert state.interval_s == 45


def test_non_dict_json_is_noop():
    state = MqttControlState()
    state.mqtt_primary = True
    state.interval_s = 45
    apply_config(state, json.dumps([1, 2, 3]))
    assert state.mqtt_primary is True
    assert state.interval_s == 45
    apply_config(state, json.dumps("just a string"))
    assert state.mqtt_primary is True
    assert state.interval_s == 45
    apply_config(state, json.dumps(42))
    assert state.mqtt_primary is True
    assert state.interval_s == 45


def test_empty_payload_leaves_state_unchanged():
    state = MqttControlState()
    state.mqtt_primary = True
    state.interval_s = 45
    state.bootstrapped = True
    apply_config(state, "{}")
    assert state.mqtt_primary is True
    assert state.interval_s == 45
    assert state.bootstrapped is True


def test_partial_payload_only_updates_present_field():
    state = MqttControlState()
    state.mqtt_primary = False
    state.interval_s = 45
    apply_config(state, json.dumps({"mqttPublishReadings": True}))
    assert state.mqtt_primary is True
    assert state.interval_s == 45  # unchanged, absent from payload

    state2 = MqttControlState()
    state2.mqtt_primary = True
    state2.interval_s = None
    apply_config(state2, json.dumps({"ingestIntervalMs": 15000}))
    assert state2.mqtt_primary is True  # unchanged, absent from payload
    assert state2.interval_s == 15


def test_ingest_interval_ms_division():
    state = MqttControlState()
    apply_config(state, json.dumps({"ingestIntervalMs": 5000}))
    assert state.interval_s == 5


def test_ingest_interval_ms_zero_or_negative_ignored():
    state = MqttControlState()
    state.interval_s = 10
    apply_config(state, json.dumps({"ingestIntervalMs": 0}))
    assert state.interval_s == 10
    apply_config(state, json.dumps({"ingestIntervalMs": -5000}))
    assert state.interval_s == 10


def test_ingest_interval_ms_wrong_type_ignored():
    state = MqttControlState()
    state.interval_s = 10
    apply_config(state, json.dumps({"ingestIntervalMs": "30000"}))
    assert state.interval_s == 10
    apply_config(state, json.dumps({"ingestIntervalMs": True}))
    assert state.interval_s == 10


def test_mqtt_publish_readings_false():
    state = MqttControlState()
    state.mqtt_primary = True
    apply_config(state, json.dumps({"mqttPublishReadings": False}))
    assert state.mqtt_primary is False


def test_bootstrapped_never_touched():
    state = MqttControlState()
    state.bootstrapped = True
    apply_config(state, json.dumps({"mqttPublishReadings": False, "ingestIntervalMs": 1000}))
    assert state.bootstrapped is True
    state2 = MqttControlState()
    state2.bootstrapped = False
    apply_config(state2, json.dumps({"mqttPublishReadings": True}))
    assert state2.bootstrapped is False


def test_update_check_is_ignored():
    """HACS owns installs and updates (hacs/default#13218), so a server-sent
    `updateCheck` no longer triggers anything; sibling fields still apply."""
    state = MqttControlState()
    apply_config(state, json.dumps({"updateCheck": True, "ingestIntervalMs": 30000}))
    assert state.interval_s == 30
    assert state.mqtt_primary is False


def test_integration_ships_no_self_updater():
    """The integration must never replace its own files or restart Home
    Assistant: HACS rejects a default-catalog entry that does
    (hacs/default#13218)."""
    import importlib.util

    for module in ("updater", "update"):
        assert importlib.util.find_spec(f"custom_components.svitgrid.{module}") is None


# ── Live mode (dynamic cadence plan 2) ──────────────────────────────────────

LIVE_CONFIG = {
    "mqttPublishReadings": True,
    "ingestIntervalMs": 30000,
    "ingestMode": "live",
    "live": {"checkSec": 5, "deltaW": 50, "deltaSocPct": 1, "heartbeatSec": 30},
}


def test_live_config_sets_live_mode():
    state = MqttControlState()
    apply_config(state, json.dumps(LIVE_CONFIG))
    assert state.live.mode == "live"
    assert state.live.params.check_s == 5
    assert effective_interval_s(state) == 5


def test_config_without_cadence_fields_keeps_live():
    state = MqttControlState()
    apply_config(state, json.dumps(LIVE_CONFIG))
    apply_config(state, json.dumps({"mqttPublishReadings": True}))
    assert state.live.mode == "live"


def test_interval_config_ends_live():
    state = MqttControlState()
    apply_config(state, json.dumps(LIVE_CONFIG))
    apply_config(state, json.dumps({"ingestIntervalMs": 300000, "ingestMode": "interval"}))
    assert state.live.mode == "interval"
    assert state.interval_s == 300
    assert effective_interval_s(state) == 300
