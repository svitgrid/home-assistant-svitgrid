"""Tests for the additive MQTT readings publisher (island-safe, fail-open)."""

from __future__ import annotations

import asyncio
import sys
import threading
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
        assert readings_topic({"topic": "devices/kBRkv3/wake"}) == "devices/kBRkv3/readings"

    def test_none_when_neither_usable(self):
        assert readings_topic({}) is None
        assert readings_topic({"topic": "devices/x/readings"}) is None
        assert readings_topic({"deviceId": ""}) is None


class _FakeInfo:
    def __init__(self, rc: int, mid: int = 1) -> None:
        self.rc = rc
        self.mid = mid


class _FakeClient:
    def __init__(self, rc: int = 0, mid: int = 1) -> None:
        self.published: list[tuple[str, str, int]] = []
        self._rc = rc
        self._mid = mid
        self.stopped = False
        self.on_publish = None

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload, qos))
        return _FakeInfo(self._rc, self._mid)


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


@pytest.mark.asyncio
class TestPublishAndWait:
    """publish_and_wait resolves only on a real PUBACK (paho's on_publish)."""

    def _connected_client(
        self, rc: int = 0, mid: int = 1
    ) -> tuple[ReadingsMqttClient, _FakeClient]:
        c = ReadingsMqttClient(api_client=None, api_key="k")
        fake = _FakeClient(rc=rc, mid=mid)
        c._client, c._topic, c._connected = fake, "devices/kBRkv3/readings", True
        # Mirrors what ensure_connected does on a real connect: capture the
        # running loop and register the PUBACK callback on the paho client.
        c._loop = asyncio.get_running_loop()
        fake.on_publish = c._on_publish
        return c, fake

    async def test_resolves_true_on_puback_from_another_thread(self):
        c, fake = self._connected_client(mid=42)

        task = asyncio.ensure_future(c.publish_and_wait('{"inverterId":"i"}'))
        # Wait until publish_and_wait has registered its future under mid=42
        # (deterministic instead of a blind sleep — mirrors the polling
        # pattern in tests/test_mqtt_wake.py).
        for _ in range(200):
            if 42 in c._pending:
                break
            await asyncio.sleep(0.001)
        assert 42 in c._pending, "publish_and_wait never registered the pending future"

        # Simulate paho's network thread firing on_publish on PUBACK — this
        # is the real thread->asyncio bridge under test, not a same-thread
        # shortcut.
        def _fire_from_paho_thread() -> None:
            fake.on_publish(fake, None, 42)

        thread = threading.Thread(target=_fire_from_paho_thread)
        thread.start()
        thread.join(timeout=2)

        assert await task is True
        assert fake.published == [("devices/kBRkv3/readings", '{"inverterId":"i"}', 1)]
        assert 42 not in c._pending, "pending entry must be cleaned up"

    async def test_false_on_timeout_when_no_puback_arrives(self):
        c, _fake = self._connected_client(mid=7)
        assert await c.publish_and_wait("{}", timeout=0.05) is False
        assert 7 not in c._pending, "pending entry must be cleaned up on timeout"

    async def test_false_when_not_connected(self):
        c = ReadingsMqttClient(api_client=None, api_key="k")
        assert await c.publish_and_wait("{}") is False

    async def test_false_on_nonzero_rc(self):
        c, fake = self._connected_client(rc=1, mid=9)
        assert await c.publish_and_wait("{}") is False
        assert fake.published == [("devices/kBRkv3/readings", "{}", 1)]
        assert not c._pending, "no future should be registered for a failed publish"
