"""Tests for mqtt_wake.run_loop.

Strategy: mock paho.mqtt.client.Client at the module level so we can:
- Drive the on_connect / on_message / on_disconnect callbacks synthetically
- Assert subscribe was called with the right topic
- Verify wake_event.set is invoked on message arrival
- Verify get_mqtt_token is re-called on reconnect (JWT re-mint)
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


# ─── paho.mqtt stub ────────────────────────────────────────────────────
#
# Inject a minimal fake paho package so mqtt_wake's lazy `import
# paho.mqtt.client` resolves without installing the real lib. The fake's
# Client class captures callbacks + exposes triggers the tests use.

def _install_paho_stub():
    """Returns (fake_module, FakeClient)."""

    class FakeClient:
        instances = []

        def __init__(self, client_id=None, protocol=None):
            self.client_id = client_id
            self.protocol = protocol
            self.username = None
            self.password = None
            self.tls_set_called = False
            self.connect_args = None
            self.loop_started = False
            self.loop_stopped = False
            self.subscriptions = []
            self.on_connect = None
            self.on_message = None
            self.on_disconnect = None
            FakeClient.instances.append(self)

        def username_pw_set(self, username=None, password=None):
            self.username = username
            self.password = password

        def tls_set(self, *args, **kwargs):
            self.tls_set_called = True

        def connect_async(self, host, port, keepalive=60):
            self.connect_args = (host, port, keepalive)

        def loop_start(self):
            self.loop_started = True

        def loop_stop(self):
            self.loop_stopped = True

        def subscribe(self, topic, qos=1):
            self.subscriptions.append((topic, qos))

        def disconnect(self):
            pass

        # Triggers used by tests:
        def trigger_connect(self, rc=0):
            self.on_connect(self, None, {}, rc)

        def trigger_message(self, topic="devices/abc/wake", payload=b"x"):
            msg = MagicMock()
            msg.topic = topic
            msg.payload = payload
            self.on_message(self, None, msg)

        def trigger_disconnect(self, rc=0):
            self.on_disconnect(self, None, rc)

    paho_mod = types.ModuleType("paho")
    paho_mqtt_mod = types.ModuleType("paho.mqtt")
    paho_client_mod = types.ModuleType("paho.mqtt.client")
    paho_client_mod.Client = FakeClient
    paho_client_mod.MQTTv311 = 4
    paho_mod.mqtt = paho_mqtt_mod
    paho_mqtt_mod.client = paho_client_mod
    sys.modules["paho"] = paho_mod
    sys.modules["paho.mqtt"] = paho_mqtt_mod
    sys.modules["paho.mqtt.client"] = paho_client_mod
    return paho_client_mod, FakeClient


@pytest.fixture
def paho_fake():
    """Install the fake paho stub for each test, clean up after."""
    paho_mod, FakeClient = _install_paho_stub()
    FakeClient.instances.clear()
    yield FakeClient
    sys.modules.pop("paho.mqtt.client", None)
    sys.modules.pop("paho.mqtt", None)
    sys.modules.pop("paho", None)


def _mock_hass_stops_after(reads: int) -> MagicMock:
    """hass mock whose is_stopping returns False for `reads` calls then True."""
    hass = MagicMock()
    counter = {"n": 0}

    def _is_stopping(_self):
        counter["n"] += 1
        return counter["n"] > reads

    type(hass).is_stopping = property(_is_stopping)
    return hass


@pytest.fixture
def token_payload():
    return {
        "token": "eyJfake.jwt.payload",
        "expiresAt": "2026-05-23T12:00:00Z",
        "broker": {
            "host": "mqtt.svitgrid.app",
            "port": 8883,
            "topic": "devices/abc123/wake",
        },
    }


@pytest.mark.asyncio
async def test_connects_subscribes_and_signals_wake_on_message(paho_fake, token_payload):
    """Happy path: connect → subscribe → message arrives → wake_event set."""
    from custom_components.svitgrid.mqtt_wake import run_loop

    api = MagicMock()
    api.get_mqtt_token = AsyncMock(return_value=token_payload)
    hass = _mock_hass_stops_after(1)  # one iteration
    wake_event = asyncio.Event()

    async def _drive():
        # Wait briefly for the FakeClient to exist, then drive callbacks.
        for _ in range(40):
            if paho_fake.instances:
                break
            await asyncio.sleep(0.01)
        client = paho_fake.instances[-1]
        client.trigger_connect(rc=0)
        await asyncio.sleep(0)  # let connected.set propagate
        client.trigger_message()
        await asyncio.sleep(0)
        client.trigger_disconnect(rc=0)  # exit the inner wait
        await asyncio.sleep(0)

    await asyncio.gather(
        run_loop(hass=hass, api_client=api, api_key="k", wake_event=wake_event),
        _drive(),
    )

    api.get_mqtt_token.assert_awaited_once_with("k")
    assert paho_fake.instances, "paho Client never instantiated"
    client = paho_fake.instances[0]
    assert client.tls_set_called is True
    assert client.connect_args == ("mqtt.svitgrid.app", 8883, 60)
    assert client.subscriptions == [("devices/abc123/wake", 1)]
    assert client.loop_started is True
    assert client.loop_stopped is True
    assert wake_event.is_set(), "wake_event should be set after on_message"


@pytest.mark.asyncio
async def test_remints_token_on_reconnect(paho_fake, token_payload):
    """Disconnect → reconnect path calls get_mqtt_token AGAIN with a fresh JWT."""
    from custom_components.svitgrid.mqtt_wake import run_loop

    api = MagicMock()
    api.get_mqtt_token = AsyncMock(side_effect=[
        token_payload,  # first mint
        {**token_payload, "token": "eyJ_second_jwt"},  # re-mint
    ])
    hass = _mock_hass_stops_after(2)  # two iterations
    wake_event = asyncio.Event()

    async def _drive():
        # First connect + disconnect
        for _ in range(40):
            if paho_fake.instances:
                break
            await asyncio.sleep(0.01)
        client1 = paho_fake.instances[-1]
        client1.trigger_connect(rc=0)
        await asyncio.sleep(0)
        client1.trigger_disconnect(rc=0)
        # Wait for second Client to be instantiated by the reconnect.
        for _ in range(100):
            if len(paho_fake.instances) >= 2:
                break
            await asyncio.sleep(0.01)
        client2 = paho_fake.instances[-1]
        client2.trigger_connect(rc=0)
        await asyncio.sleep(0)
        client2.trigger_disconnect(rc=0)
        await asyncio.sleep(0)

    await asyncio.gather(
        run_loop(hass=hass, api_client=api, api_key="k", wake_event=wake_event),
        _drive(),
    )

    # Token was minted twice (initial + on reconnect)
    assert api.get_mqtt_token.await_count == 2
    # Second client used the re-minted token
    assert paho_fake.instances[1].password == "eyJ_second_jwt"


@pytest.mark.asyncio
async def test_token_mint_failure_backs_off(paho_fake, monkeypatch):
    """If get_mqtt_token raises, the loop logs + sleeps (exp backoff) and retries."""
    from custom_components.svitgrid.api_client import SvitgridApiError
    from custom_components.svitgrid import mqtt_wake

    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    api = MagicMock()
    api.get_mqtt_token = AsyncMock(side_effect=SvitgridApiError("503 unavailable"))
    hass = _mock_hass_stops_after(2)
    wake_event = asyncio.Event()

    await mqtt_wake.run_loop(
        hass=hass, api_client=api, api_key="k", wake_event=wake_event,
    )

    # Two failed iterations → two backoff sleeps (initial 5s, then 10s)
    assert sleeps[:2] == [
        float(mqtt_wake.BACKOFF_INITIAL_S),
        float(mqtt_wake.BACKOFF_INITIAL_S * 2),
    ]


@pytest.mark.asyncio
async def test_returns_silently_if_paho_unavailable(monkeypatch, token_payload):
    """No paho-mqtt installed → log error and return without crashing."""
    # Ensure paho is NOT importable.
    for k in list(sys.modules):
        if k.startswith("paho"):
            sys.modules.pop(k)
    # Force ImportError for paho.mqtt.client
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "paho.mqtt.client" or name.startswith("paho.mqtt"):
            raise ImportError("paho not installed (test stub)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    from custom_components.svitgrid.mqtt_wake import run_loop

    api = MagicMock()
    api.get_mqtt_token = AsyncMock(return_value=token_payload)
    hass = _mock_hass_stops_after(99)  # would loop forever if not for ImportError exit
    wake_event = asyncio.Event()

    # Should return without error, without calling get_mqtt_token.
    await run_loop(hass=hass, api_client=api, api_key="k", wake_event=wake_event)
    api.get_mqtt_token.assert_not_awaited()
