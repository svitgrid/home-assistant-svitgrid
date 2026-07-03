"""MQTT command-wake bell.

Subscribes to `devices/{firestoreEdgeDeviceId}/wake` on the Svitgrid broker.
When the cloud has a fresh command for us (user opened the app, scheduled
event fired, force-charge triggered, etc.), the API publishes one byte
to that topic. We catch it and set the shared `wake_event`, which the
command_poller awaits inside its sleep. The next poll cycle fires
immediately instead of waiting up to COMMAND_POLL_INTERVAL_S.

Why a wake-bell instead of long-poll HTTP:
- Sub-second command latency without a continuous HTTP poll
- One open TCP connection (TLS) instead of one HTTP roundtrip every N seconds
- Mirrors the edge connector + mobile harvester command-wake architecture

Token lifecycle:
- Server mints 24h JWTs via POST /api/v3/edge-devices/:id/mqtt-token
- We re-mint every ~12h preemptively (TOKEN_REMINT_INTERVAL_S)
- We ALWAYS re-mint on reconnect, regardless of how long the previous
  token has been alive — caches/proxies can deliver stale tokens, and the
  bridge sat in an expired-token loop for 75 min on 2026-05-19 because
  it only minted at startup (see feedback_jwt_remint_on_reconnect.md).

Fallback:
- If the broker is unreachable, we back off exponentially and retry
- command_poller continues its periodic HTTP poll regardless, so commands
  eventually arrive even with MQTT down. Just slower (poll interval, not
  sub-second).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import SvitgridApiClient

_LOGGER = logging.getLogger(__name__)

# Re-mint the JWT this often to stay well ahead of the 24h expiry.
TOKEN_REMINT_INTERVAL_S = 12 * 3600

# Reconnect backoff envelope.
BACKOFF_INITIAL_S = 5
BACKOFF_MAX_S = 300

# Time to wait for the MQTT CONNECT handshake before giving up.
CONNECT_TIMEOUT_S = 15


def _teardown_client(client: Any) -> None:
    """Stop paho's network thread and disconnect. Idempotent; never raises.

    MUST be called before any backoff sleep: paho's ``connect_async`` +
    ``loop_start`` spawns a network thread that auto-reconnects on its own,
    so a client left running during backoff floods the log with
    ``on_disconnect`` events against an unreachable broker.
    """
    if client is None:
        return
    try:
        client.loop_stop()
        client.disconnect()
    except Exception:  # noqa: BLE001
        _LOGGER.debug("MQTT cleanup error (non-fatal)", exc_info=True)


async def run_loop(
    *,
    hass: HomeAssistant,
    api_client: SvitgridApiClient,
    api_key: str,
    wake_event: asyncio.Event,
) -> None:
    """Long-running coroutine. Maintains an MQTT subscription to the
    wake topic; on every message, sets `wake_event` so the
    command_poller fires a poll immediately.

    Reconnect loop with exponential backoff; JWT re-minted on every
    reconnect. Exits when `hass.is_stopping` becomes True."""
    # Lazy import — paho-mqtt may not be installed in all HA setups, and
    # we don't want a module-level import error to break the rest of the
    # integration. The manifest declares it as a requirement.
    try:
        import paho.mqtt.client as paho  # noqa: PLC0415
    except ImportError:
        _LOGGER.error(
            "paho-mqtt not available — MQTT wake-bell disabled. "
            "Command latency falls back to the regular HTTP poll cadence."
        )
        return

    backoff_s = BACKOFF_INITIAL_S
    loop = asyncio.get_running_loop()

    while not hass.is_stopping:
        client: Any = None
        try:
            token_data = await api_client.get_mqtt_token(api_key)
            broker = token_data["broker"]
            host, port, topic = broker["host"], int(broker["port"]), broker["topic"]

            connected = asyncio.Event()
            disconnected = asyncio.Event()

            client = paho.Client(
                client_id=f"svitgrid-ha-{topic.replace('/', '_')}",
                protocol=paho.MQTTv311,
            )
            # Bridge auth — username is informational; password is the JWT.
            client.username_pw_set(username="edge-device", password=token_data["token"])
            client.tls_set()  # default system trust store

            def _on_connect(_c, _u, _flags, rc):
                if rc == 0:
                    client.subscribe(topic, qos=1)
                    loop.call_soon_threadsafe(connected.set)
                else:
                    _LOGGER.warning("MQTT CONNECT rc=%s", rc)
                    loop.call_soon_threadsafe(disconnected.set)

            def _on_message(_c, _u, msg):
                _LOGGER.debug("MQTT wake-bell topic=%s payload=%s",
                              msg.topic, msg.payload[:32])
                # Thread-safe set from paho's network thread → asyncio loop.
                loop.call_soon_threadsafe(wake_event.set)

            def _on_disconnect(_c, _u, rc):
                # DEBUG, not INFO: against an unreachable broker paho fires
                # this repeatedly. The user-visible signal is the single
                # "reconnecting"/"backing off" line logged below.
                _LOGGER.debug("MQTT disconnected rc=%s", rc)
                loop.call_soon_threadsafe(disconnected.set)

            client.on_connect = _on_connect
            client.on_message = _on_message
            client.on_disconnect = _on_disconnect

            # paho's `connect_async` + `loop_start` is the right pattern
            # for non-blocking connect (the network thread handles the
            # TCP+TLS handshake without holding the asyncio loop).
            client.connect_async(host, port, keepalive=60)
            client.loop_start()

            try:
                await asyncio.wait_for(connected.wait(), timeout=CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise ConnectionError(
                    f"MQTT CONNECT timeout after {CONNECT_TIMEOUT_S}s"
                ) from exc

            backoff_s = BACKOFF_INITIAL_S  # reset on successful connect
            _LOGGER.info(
                "MQTT wake-bell active: broker=%s:%s topic=%s",
                host, port, topic,
            )

            # Hold until disconnected OR remint time.
            try:
                await asyncio.wait_for(
                    disconnected.wait(), timeout=TOKEN_REMINT_INTERVAL_S,
                )
                _LOGGER.info("MQTT lost connection; reconnecting with fresh token")
            except asyncio.TimeoutError:
                _LOGGER.info("MQTT token re-mint due; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Stop the paho network thread BEFORE sleeping — otherwise it
            # keeps auto-reconnecting to the broker for the whole backoff
            # window, flooding the log with rc=7 disconnects (observed
            # 2026-07-03 against the torn-down staging broker).
            _teardown_client(client)
            client = None
            _LOGGER.warning(
                "MQTT wake-bell failed (%s); backing off %ss before retry",
                exc, backoff_s,
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, BACKOFF_MAX_S)
        finally:
            _teardown_client(client)

    _LOGGER.info("MQTT wake-bell loop stopped")
