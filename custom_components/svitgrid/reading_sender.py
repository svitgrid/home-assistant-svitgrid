"""Outbound buffer drain + adaptive-cadence holder (Sub-project 1)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .api_client import DeviceEvicted, ReadingRejected
from .battery_sign import flip_battery_sign
from .const import BACKFILL_CAP_S, CADENCE_DEFAULT_INTERVAL_S, INGEST_BATCH_MAX, SENDER_TICK_S
from .mqtt_control import MqttControlState
from .mqtt_readings_publisher import ReadingsMqttClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class Cadence:
    """Shared produce-cadence the sender updates and the publisher reads."""

    interval_s: int = CADENCE_DEFAULT_INTERVAL_S


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def _http_send(
    *,
    store,
    api_client,
    api_key: str,
    now_iso: str,
    cadence: Cadence,
    lifecycle,
    keys: list[tuple[str, str]],
    readings: list[Any],
    publisher,
) -> tuple[int, bool]:
    """POST ``readings`` (aligned with ``keys``) via the HTTP batch ingest,
    map the per-item results back onto the store, handle DeviceEvicted /
    ReadingRejected / transient-5xx / ``stopped`` responses, best-effort
    mirror cloud-accepted readings over MQTT (fire-and-forget, additive), and
    update ``cadence`` from the response. Shared by both the bootstrap/full
    HTTP path and the MQTT-primary fallback path (Task 3) so this logic is
    never duplicated.

    Returns ``(sent_count, response_ok)`` where ``response_ok`` is True iff
    the cloud returned a real, parseable response (i.e. not DeviceEvicted,
    not ReadingRejected, not a transient-5xx ``None``) — the caller uses this
    to flip ``control.bootstrapped`` once the cloud path is proven this
    session, mirroring the edge firmware's ``g_http_ingest_confirmed_this_boot``.
    """
    try:
        body = await api_client.push_readings_batch(api_key=api_key, readings=readings)
    except DeviceEvicted as exc:
        if lifecycle is not None:
            lifecycle.deprovision(str(exc), now_iso)
            await _maybe(store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since))
        return 0, False
    except ReadingRejected:
        await _maybe(store.mark_failed(keys, now_iso))
        return 0, False

    if body is None:  # transient 5xx
        await _maybe(store.mark_failed(keys, now_iso))
        return 0, False

    if isinstance(body, dict) and body.get("stopped"):
        _LOGGER.warning(
            "cloud reports device stopped (%s); leaving %d row(s) pending",
            body.get("stoppedReason"),
            len(keys),
        )
        if lifecycle is not None:
            lifecycle.pause(str(body.get("stoppedReason") or "stopped"), now_iso)
            await _maybe(store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since))
        return 0, True

    # Map per-item results back to rows (results are returned in input order).
    results = body.get("results") if isinstance(body, dict) else None
    sent_keys: list[tuple[str, str]] = []
    failed_keys: list[tuple[str, str]] = []
    if isinstance(results, list) and len(results) == len(keys):
        for key, res in zip(keys, results, strict=False):
            (sent_keys if res.get("ok") else failed_keys).append(key)
    else:
        # No per-item detail → 2xx means the cloud accepted the batch.
        sent_keys = keys

    if sent_keys:
        await _maybe(store.mark_sent(sent_keys))
    if failed_keys:
        await _maybe(store.mark_failed(failed_keys, now_iso))

    # Additive MQTT publish of the cloud-accepted readings. ISLAND-SAFE: this is
    # the cloud sender, which never runs for island-mode entries, so island
    # readings never reach here. Best-effort — the HTTP batch above is the
    # source of truth; a broker outage just means no warm-Redis copy. Gated on
    # the server's `mqttPublishReadings` flag (same gate as the edge connector).
    if (
        publisher is not None
        and sent_keys
        and isinstance(body, dict)
        and body.get("mqttPublishReadings")
        and await publisher.ensure_connected()
    ):
        sent_set = set(sent_keys)
        for key, reading in zip(keys, readings, strict=False):
            if key in sent_set:
                publisher.publish(json.dumps(reading))

    interval_ms = body.get("ingestIntervalMs") if isinstance(body, dict) else None
    if isinstance(interval_ms, (int, float)) and interval_ms > 0:
        cadence.interval_s = int(interval_ms / 1000)

    return len(sent_keys), True


async def drain_once(
    *,
    store,
    api_client,
    api_key: str,
    now_iso: str,
    cadence: Cadence,
    batch_max: int = INGEST_BATCH_MAX,
    cap_s: int = BACKFILL_CAP_S,
    lifecycle=None,
    discharge_positive_ids: set[str] | None = None,
    publisher=None,
    control: MqttControlState | None = None,
) -> int:
    """Drain at most one batch. Returns number of rows marked 'sent'.

    ``discharge_positive_ids`` names inverters whose battery power we normalized
    to Svitgrid's charge-positive convention at capture (HA Solarman). We
    re-invert those before upload so the cloud keeps receiving the raw
    discharge-positive value its `home_assistant_solarman` handler negates —
    the server contract is unchanged. See battery_sign.py.

    ``control`` (Task 3) is the shared ``MqttControlState`` updated by the
    MQTT wake client from `devices/{deviceId}/config`. When it says the
    server wants MQTT-primary AND this session already proved the HTTP path
    once (bootstrapped) AND the readings publisher is connected, each row is
    published QoS-1 and PUBACK-confirmed (`publish_and_wait`); rows the broker
    acknowledges are marked sent WITHOUT going over HTTP, and only the
    un-acknowledged rows fall back to the HTTP batch. Every other case (first
    drain this session / flag off / publisher absent or unreachable) uses the
    HTTP batch for every row, exactly as before Task 3 — and, on a real HTTP
    response, flips ``control.bootstrapped`` so later drains may go
    MQTT-primary. A reading is NEVER dropped: un-acked rows always still go
    over HTTP, and the store row stays 'pending' until either path marks it
    sent.
    """
    # Config-over-MQTT is the cadence source for MQTT-primary installs: in
    # steady state the HTTP response (the other cadence source, set in
    # _http_send below) rarely runs, so a cadence pushed on
    # `devices/{id}/config` would otherwise never reach the shared Cadence.
    # Applied up front so it takes effect regardless of which branch below
    # actually sends this batch. The HTTP-response update further down still
    # runs on every real HTTP round-trip (bootstrap/fallback source) and may
    # overwrite this in the same cycle — both derive from the same server
    # cadence logic, so they agree.
    if control is not None and control.interval_s is not None and control.interval_s > 0:
        cadence.interval_s = control.interval_s

    # Age out anything beyond the backfill cap so it never clogs the queue.
    await _maybe(store.skip_aged(now_iso, cap_s))

    rows = await _maybe(store.get_sendable(now_iso, cap_s, batch_max))
    if not rows:
        return 0

    flip_ids = discharge_positive_ids or set()
    keys = [(r["inverter_id"], r["ts"]) for r in rows]
    readings = [
        flip_battery_sign(r["payload"]) if r["inverter_id"] in flip_ids else r["payload"]
        for r in rows
    ]

    mqtt_primary_ready = (
        control is not None
        and control.mqtt_primary
        and control.bootstrapped
        and publisher is not None
        and await publisher.ensure_connected()
    )

    if mqtt_primary_ready:
        acked_keys: list[tuple[str, str]] = []
        unacked_keys: list[tuple[str, str]] = []
        unacked_readings: list[Any] = []
        for key, reading in zip(keys, readings, strict=False):
            if await publisher.publish_and_wait(json.dumps(reading)):
                acked_keys.append(key)
            else:
                unacked_keys.append(key)
                unacked_readings.append(reading)

        if acked_keys:
            await _maybe(store.mark_sent(acked_keys))

        http_sent = 0
        if unacked_keys:
            http_sent, _ok = await _http_send(
                store=store,
                api_client=api_client,
                api_key=api_key,
                now_iso=now_iso,
                cadence=cadence,
                lifecycle=lifecycle,
                keys=unacked_keys,
                readings=unacked_readings,
                publisher=publisher,
            )
        return len(acked_keys) + http_sent

    # Bootstrap (first drain this session) or MQTT-primary not ready: HTTP for
    # every row, as before Task 3. A real cloud response proves the HTTP path
    # is up this session, so later drains may switch to MQTT-primary.
    sent_count, response_ok = await _http_send(
        store=store,
        api_client=api_client,
        api_key=api_key,
        now_iso=now_iso,
        cadence=cadence,
        lifecycle=lifecycle,
        keys=keys,
        readings=readings,
        publisher=publisher,
    )
    if control is not None and response_ok:
        control.bootstrapped = True
    return sent_count


async def run_sender_loop(
    *,
    hass: HomeAssistant,
    store,
    api_client,
    api_key: str,
    cadence: Cadence,
    tick_s: int = SENDER_TICK_S,
    lifecycle=None,
    discharge_positive_ids: set[str] | None = None,
    control: MqttControlState | None = None,
) -> None:
    # Additive MQTT readings publisher, owned by this loop so its paho network
    # thread is torn down when the loop exits (task cancel on unload). Construction
    # is cheap — it does NOT connect until drain_once first sees the server's
    # `mqttPublishReadings` flag (or, post-Task-3, until control.mqtt_primary is
    # set). Only reached here for cloud installs (this loop isn't spawned in
    # island mode).
    publisher = ReadingsMqttClient(api_client=api_client, api_key=api_key)
    wait_for_data = getattr(store, "wait_for_data", None)
    try:
        while not hass.is_stopping and (lifecycle is None or lifecycle.active):
            try:
                await drain_once(
                    store=store,
                    api_client=api_client,
                    api_key=api_key,
                    now_iso=_now_iso(),
                    cadence=cadence,
                    lifecycle=lifecycle,
                    discharge_positive_ids=discharge_positive_ids,
                    publisher=publisher,
                    control=control,
                )
            except Exception:  # never let the sender loop die
                _LOGGER.exception("sender drain failed")
            # Use the store's data-available event when present so a fresh reading
            # wakes the sender immediately instead of waiting up to tick_s.
            # Fall back to a plain sleep for test doubles or stores that lack the method.
            if wait_for_data is not None:
                await wait_for_data(tick_s)
            else:
                await asyncio.sleep(tick_s)
    finally:
        publisher.stop()


async def _maybe(awaitable_or_value: Any) -> Any:
    """Await coroutines; pass through plain values (lets tests pass a store
    whose async wrappers are real coroutines while keeping drain_once simple)."""
    if asyncio.iscoroutine(awaitable_or_value):
        return await awaitable_or_value
    return awaitable_or_value
