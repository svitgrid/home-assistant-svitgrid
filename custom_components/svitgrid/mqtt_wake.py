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
from .mqtt_control import MqttControlState, apply_config

_LOGGER = logging.getLogger(__name__)

# Re-mint the JWT this often to stay well ahead of the 24h expiry.
TOKEN_REMINT_INTERVAL_S = 12 * 3600

# Reconnect backoff envelope.
BACKOFF_INITIAL_S = 5
BACKOFF_MAX_S = 300

# Time to wait for the MQTT CONNECT handshake before giving up.
CONNECT_TIMEOUT_S = 15


def _config_topic(broker: dict[str, Any], wake_topic: str) -> str:
    """Derive ``devices/<deviceId>/config`` the same way
    ``mqtt_readings_publisher.readings_topic`` derives the readings topic:
    prefer the authoritative ``broker['deviceId']``, falling back to
    swapping the wake topic's ``/wake`` suffix for older servers that don't
    return ``deviceId``.
    """
    did = broker.get("deviceId")
    if isinstance(did, str) and did:
        return f"devices/{did}/config"
    if wake_topic.endswith("/wake"):
        return wake_topic[: -len("/wake")] + "/config"
    return wake_topic + "/config"


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
    control: MqttControlState | None = None,
) -> None:
    """Long-running coroutine. Maintains an MQTT subscription to the
    wake topic; on every message, sets `wake_event` so the
    command_poller fires a poll immediately.

    Also (additively) subscribes `devices/{deviceId}/config` on the SAME
    connection. A message on that topic updates the shared `control` state
    (via `apply_config`) instead of firing the wake event — mirroring the
    edge firmware's server config-push (W4a/W4b). `control` is optional
    (defaults to None) so callers that don't yet wire a shared
    `MqttControlState` (Task 3) are unaffected; when None, config messages
    are still consumed off the topic (harmless) but no state is updated.

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
            cfg_topic = _config_topic(broker, topic)

            connected = asyncio.Event()
            disconnected = asyncio.Event()

            client = paho.Client(
                client_id=f"svitgrid-ha-{topic.replace('/', '_')}",
                protocol=paho.MQTTv311,
            )
            # Bridge auth — the broker (mosquitto-go-auth, parse_token=true,
            # jwt_userfield=Subject) reads the JWT from the MQTT USERNAME field
            # and parses it locally to derive the identity. The token MUST be
            # the username; the password is ignored. Sending username="edge-device"
            # with the JWT in the password made the broker parse "edge-device" as
            # a JWT ("token contains an invalid number of segments") and drop the
            # CONNECT with no CONNACK (rc=7) — the wake-bell never connected.
            # Matches the ESP32 firmware (.username = token, .password = "ignored").
            client.username_pw_set(username=token_data["token"], password="ignored")
            client.tls_set()  # default system trust store

            # Bind the per-iteration client/topic/events as default args so each
            # callback closes over *this* iteration's objects (the client is torn
            # down before the loop repeats, but binding removes any latent race
            # and satisfies flake8-bugbear B023).
            def _on_connect(
                _c,
                _u,
                _flags,
                rc,
                client=client,
                topic=topic,
                cfg_topic=cfg_topic,
                connected=connected,
                disconnected=disconnected,
            ):
                if rc == 0:
                    client.subscribe(topic, qos=1)
                    client.subscribe(cfg_topic, qos=1)
                    loop.call_soon_threadsafe(connected.set)
                else:
                    _LOGGER.warning("MQTT CONNECT rc=%s", rc)
                    loop.call_soon_threadsafe(disconnected.set)

            def _on_message(_c, _u, msg, cfg_topic=cfg_topic):
                if msg.topic == cfg_topic:
                    _LOGGER.debug("MQTT config push topic=%s payload=%s", msg.topic, msg.payload[:256])
                    if control is not None:
                        # Runs on paho's network thread, not the asyncio loop —
                        # safe under CPython's GIL because apply_config only
                        # assigns independent scalar fields (mqtt_primary,
                        # interval_s) one at a time; `bootstrapped` is never
                        # written here, only read/written on the loop thread
                        # (reading_sender.drain_once), so there's no shared
                        # field being mutated from both threads.
                        apply_config(control, msg.payload)
                    return
                _LOGGER.debug("MQTT wake-bell topic=%s payload=%s", msg.topic, msg.payload[:32])
                # Thread-safe set from paho's network thread → asyncio loop.
                loop.call_soon_threadsafe(wake_event.set)

            def _on_disconnect(_c, _u, rc, disconnected=disconnected):
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
            except TimeoutError as exc:
                raise ConnectionError(f"MQTT CONNECT timeout after {CONNECT_TIMEOUT_S}s") from exc

            backoff_s = BACKOFF_INITIAL_S  # reset on successful connect
            _LOGGER.info(
                "MQTT wake-bell active: broker=%s:%s topic=%s",
                host,
                port,
                topic,
            )

            # Hold until disconnected OR remint time.
            try:
                await asyncio.wait_for(
                    disconnected.wait(),
                    timeout=TOKEN_REMINT_INTERVAL_S,
                )
                _LOGGER.info("MQTT lost connection; reconnecting with fresh token")
            except TimeoutError:
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
                exc,
                backoff_s,
            )
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, BACKOFF_MAX_S)
        finally:
            _teardown_client(client)

    _LOGGER.info("MQTT wake-bell loop stopped")
