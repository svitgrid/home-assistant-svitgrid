"""Tests for the additive MQTT readings publisher (island-safe, fail-open)."""

from __future__ import annotations

import sys
import types

import pytest

from custom_components.svitgrid.mqtt_readings_publisher import (
    ReadingsMqttClient,
    readings_topic,
)


class TestReadingsTopic:
    def test_prefers_device_id(self):
        assert (
            readings_topic({"deviceId": "kBRkv3", "topic": "devices/edge-mac/wake"})
            == "devices/kBRkv3/readings"
        )

    def test_falls_back_to_wake_topic_swap(self):
        assert (
            readings_topic({"topic": "devices/kBRkv3/wake"}) == "devices/kBRkv3/readings"
        )

    def test_none_when_neither_usable(self):
        assert readings_topic({}) is None
        assert readings_topic({"topic": "devices/x/readings"}) is None
        assert readings_topic({"deviceId": ""}) is None


class _FakeInfo:
    def __init__(self, rc: int) -> None:
        self.rc = rc


class _FakeClient:
    def __init__(self, rc: int = 0) -> None:
        self.published: list[tuple[str, str, int]] = []
        self._rc = rc
        self.stopped = False

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return _FakeInfo(self._rc)


class TestPublish:
    def _connected_client(self, rc: int = 0) -> tuple[ReadingsMqttClient, _FakeClient]:
        c = ReadingsMqttClient(api_client=None, api_key="k")
        fake = _FakeClient(rc)
        c._client, c._topic, c._connected = fake, "devices/kBRkv3/readings", True
        return c, fake

    def test_publishes_qos1_to_topic(self):
        c, fake = self._connected_client()
        assert c.publish('{"inverterId":"i"}') is True
        assert fake.published == [("devices/kBRkv3/readings", '{"inverterId":"i"}', 1)]

    def test_false_when_not_connected(self):
        c = ReadingsMqttClient(api_client=None, api_key="k")
        assert c.publish("{}") is False

    def test_fail_open_on_publish_error(self):
        c, fake = self._connected_client()

        def boom(*_a, **_k):
            raise RuntimeError("broker gone")

        fake.publish = boom
        assert c.publish("{}") is False
        assert c._connected is False  # flagged for reconnect


class _FakeApi:
    def __init__(self, broker: dict) -> None:
        self._broker = broker

    async def get_mqtt_token(self, api_key: str) -> dict:
        return {"token": "jwt", "broker": self._broker}


def _install_fake_paho(monkeypatch, client_cls) -> None:
    """Register the whole paho.mqtt.client chain in sys.modules (paho-mqtt is
    not installed in the test venv, so the dotted import needs every level)."""
    pkg = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    client = types.ModuleType("paho.mqtt.client")
    client.Client = client_cls
    client.MQTTv311 = 4
    pkg.mqtt = mqtt
    mqtt.client = client
    monkeypatch.setitem(sys.modules, "paho", pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client)


@pytest.mark.asyncio
class TestEnsureConnected:
    async def test_connects_and_sets_topic(self, monkeypatch):
        created = {}

        class FakeClient:
            def __init__(self, client_id="", protocol=None):
                created["client_id"] = client_id
                self.on_connect = None

            def username_pw_set(self, username, password):
                created["username"] = username

            def tls_set(self):
                created["tls"] = True

            def connect_async(self, host, port, keepalive=60):
                created["host"], created["port"] = host, port

            def loop_start(self):
                # fire the connect callback like paho's network thread would
                self.on_connect(self, None, None, 0)

        _install_fake_paho(monkeypatch, FakeClient)
        api = _FakeApi({"host": "mqtt.svitgrid.app", "port": 8883, "deviceId": "kBRkv3"})
        c = ReadingsMqttClient(api_client=api, api_key="k")
        assert await c.ensure_connected() is True
        assert c._topic == "devices/kBRkv3/readings"
        assert created["username"] == "jwt"  # JWT in username field
        assert created["host"] == "mqtt.svitgrid.app"

    async def test_returns_false_when_topic_underivable(self, monkeypatch):
        _install_fake_paho(monkeypatch, object)
        api = _FakeApi({"host": "h", "port": 8883})  # no deviceId, no wake topic
        c = ReadingsMqttClient(api_client=api, api_key="k")
        assert await c.ensure_connected() is False
