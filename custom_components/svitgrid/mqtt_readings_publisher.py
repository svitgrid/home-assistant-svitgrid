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
        self._loop: asyncio.AbstractEventLoop | None = None
        # PUBACK-wait registry: paho `mid` -> the asyncio.Future awaited by
        # publish_and_wait. Only ever mutated on the asyncio loop thread —
        # _on_publish (paho's network thread) never touches this dict
        # directly, it hops via call_soon_threadsafe(self._resolve, mid)
        # first, so no lock is needed.
        self._pending: dict[int, asyncio.Future[bool]] = {}

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
            client.on_publish = self._on_publish
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
            await asyncio.wait_for(connected.wait(), timeout=CONNECT_TIMEOUT_S)
            self._client, self._topic, self._connected = client, topic, True
            self._loop = loop
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

    async def publish_and_wait(self, payload: str, timeout: float = 5.0) -> bool:  # noqa: ASYNC109
        """Publish one reading (QoS 1) and wait for the broker's PUBACK.

        Returns True iff the broker actually acknowledges the publish
        (paho's ``on_publish`` fires for this ``mid``) within ``timeout``
        seconds. Fail-open in every other case — not connected, a publish
        error, or no PUBACK in time all return False rather than raise, so
        the caller can fall back to HTTP without special-casing exceptions.
        """
        if not self._connected or self._client is None or self._topic is None:
            return False
        try:
            info = self._client.publish(self._topic, payload, qos=1)
        except Exception:  # noqa: BLE001 — never break the sender
            self._connected = False
            return False
        if getattr(info, "rc", 1) != 0:
            return False
        mid = info.mid
        loop = self._loop or asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        self._pending[mid] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except Exception:  # noqa: BLE001 — timeout (or any other wait error): fail-open
            return False
        finally:
            self._pending.pop(mid, None)

    def _on_publish(self, _client: Any, _userdata: Any, mid: int, *_args: Any) -> None:
        """paho network-thread callback on PUBACK. Never touches ``_pending``
        directly — hops to the asyncio loop via ``call_soon_threadsafe`` so
        the dict is only ever mutated on the loop thread. Accepts ``*_args``
        for paho v2's extra ``reason_code``/``properties`` positional args.
        """
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._resolve, mid)

    def _resolve(self, mid: int) -> None:
        """Runs on the asyncio loop thread. Defensive about unknown/duplicate
        mids (already resolved, already popped, or never registered)."""
        future = self._pending.get(mid)
        if future is not None and not future.done():
            future.set_result(True)

    def stop(self) -> None:
        client, self._client, self._connected = self._client, None, False
        if client is not None:
            with contextlib.suppress(Exception):
                client.loop_stop()
                client.disconnect()
