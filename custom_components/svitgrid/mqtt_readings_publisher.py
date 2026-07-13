"""Publish readings to the Svitgrid MQTT broker (`devices/<deviceId>/readings`).

ADDITIVE to the HTTP cloud send in ``reading_sender.drain_once`` — the broker
consumer folds these into Redis for warm app reads, exactly like the edge
connector's MQTT publish. Fail-open in every path: if the broker is
unreachable or paho is missing, publishing is skipped and the HTTP send (which
already happened) is the source of truth.

ISLAND-SAFE BY CONSTRUCTION: this is only ever driven from ``drain_once``, the
*cloud* sender. In pure island mode the cloud sender never runs (readings are
local-only), so island readings never reach this publisher.

Connection mirrors ``mqtt_wake.py``: the JWT is sent in the MQTT USERNAME field
(mosquitto-go-auth ``jwt_userfield=Subject``), TLS via the system trust store.
Topic derivation mirrors the edge firmware: prefer the authoritative
``broker.deviceId`` the API returns, fall back to swapping the wake topic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10


def readings_topic(broker: dict[str, Any]) -> str | None:
    """Build ``devices/<deviceId>/readings`` from an mqtt-token ``broker`` block.

    Prefers ``broker['deviceId']`` (the authoritative cloud/Firestore id, and
    the only id the broker ACL authorizes this executor to publish under).
    Falls back to swapping the wake topic's ``/wake`` → ``/readings`` for older
    servers that don't return ``deviceId``. Returns ``None`` if neither is
    usable — never guess.
    """
    did = broker.get("deviceId")
    if isinstance(did, str) and did:
        return f"devices/{did}/readings"
    topic = broker.get("topic")
    if isinstance(topic, str) and topic.endswith("/wake"):
        return topic[: -len("/wake")] + "/readings"
    return None


class ReadingsMqttClient:
    """Long-lived MQTT publish client. Every method is fail-open — a broker
    outage degrades to "HTTP only", never an exception into the sender."""

    def __init__(self, api_client: Any, api_key: str) -> None:
        self._api = api_client
        self._api_key = api_key
        self._client: Any = None
        self._topic: str | None = None
        self._connected = False

    async def ensure_connected(self) -> bool:
        """Idempotent: connect (mint token + open TLS) if not already up.
        Returns True when ready to publish."""
        if self._connected and self._client is not None:
            return True
        try:
            import paho.mqtt.client as paho  # noqa: PLC0415
        except ImportError:
            _LOGGER.info("paho-mqtt not available — MQTT readings publish disabled")
            return False
        try:
            token_data = await self._api.get_mqtt_token(self._api_key)
            broker = token_data["broker"]
            topic = readings_topic(broker)
            if topic is None:
                _LOGGER.warning("mqtt-token missing deviceId/wake topic — readings publish off")
                return False
            host, port = broker["host"], int(broker["port"])
            loop = asyncio.get_running_loop()
            connected = asyncio.Event()

            client = paho.Client(
                client_id=f"svitgrid-ha-pub-{topic.replace('/', '_')}",
                protocol=paho.MQTTv311,
            )
            # JWT in the USERNAME field (see mqtt_wake.py) — password ignored.
            client.username_pw_set(username=token_data["token"], password="ignored")
            client.tls_set()

            def _on_connect(_c: Any, _u: Any, _f: Any, rc: int) -> None:
                if rc == 0:
                    loop.call_soon_threadsafe(connected.set)
                else:
                    _LOGGER.warning("MQTT readings CONNECT rc=%s", rc)

            client.on_connect = _on_connect
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            await asyncio.wait_for(connected.wait(), timeout=CONNECT_TIMEOUT_S)
            self._client, self._topic, self._connected = client, topic, True
            _LOGGER.info("MQTT readings publish active: topic=%s", topic)
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open observability path
            _LOGGER.debug("MQTT readings connect failed (will retry): %s", exc)
            self._connected = False
            return False

    def publish(self, reading_json: str) -> bool:
        """Publish one reading (QoS 1). Fail-open: returns False (never raises)
        if not connected or the publish errors, and flags for reconnect."""
        if not self._connected or self._client is None or self._topic is None:
            return False
        try:
            info = self._client.publish(self._topic, reading_json, qos=1)
            return getattr(info, "rc", 1) == 0
        except Exception:  # noqa: BLE001 — never break the sender
            self._connected = False
            return False

    def stop(self) -> None:
        client, self._client, self._connected = self._client, None, False
        if client is not None:
            with contextlib.suppress(Exception):
                client.loop_stop()
                client.disconnect()
