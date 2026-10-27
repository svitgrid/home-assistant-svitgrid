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
from .live_gate import LiveGate
from .mqtt_control import MqttControlState, apply_cadence, effective_interval_s
from .mqtt_readings_publisher import ReadingsMqttClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class Cadence:
    """Shared produce-cadence the sender updates and the publisher reads."""

    interval_s: int = CADENCE_DEFAULT_INTERVAL_S


# Cooldown after an auth failure (401): the API key is dead — retrying with
# different readings can never succeed, and before 0.17.1 a dead-key install
# hammered the cloud every 5s forever (~17k POSTs/day measured on one prod
# install). 15 min bounds the recovery lag after the user re-pairs.
AUTH_COOLDOWN_S = 15 * 60

# Batch-failure backoff: base doubles per consecutive failure, capped.
_FAILURE_BACKOFF_BASE_S = 10
_FAILURE_BACKOFF_CAP_S = 5 * 60


class SenderHealth:
    """Sender-wide failure backoff, consulted by `drain_once`.

    Row-level give-up lives in the store (MAX_SEND_ATTEMPTS); this class
    paces the SENDER between batches so failures don't retry on the raw 5s
    tick. Implements the "back off HARD" contract `ReadingRejected`'s
    docstring has always promised:

      - auth failure (401)      -> fixed long cooldown (AUTH_COOLDOWN_S)
      - batch failure (4xx/5xx/all-items-failed)
                                -> 10s, 20s, 40s ... capped at 5 min
      - any accepted reading    -> reset

    `now` is injectable (monotonic seconds) for tests.
    """

    def __init__(self, now=None) -> None:
        import time

        self._now = now or time.monotonic
        self._cooldown_until: float | None = None
        self._streak = 0

    def in_cooldown(self) -> bool:
        return self._cooldown_until is not None and self._now() < self._cooldown_until

    def cooldown_remaining(self) -> float:
        if self._cooldown_until is None:
            return 0.0
        return max(0.0, self._cooldown_until - self._now())

    def note_auth_failure(self) -> None:
        self._cooldown_until = self._now() + AUTH_COOLDOWN_S

    def note_batch_failure(self) -> None:
        self._streak += 1
        delay = min(
            _FAILURE_BACKOFF_CAP_S,
            _FAILURE_BACKOFF_BASE_S * (2 ** (self._streak - 1)),
        )
        self._cooldown_until = self._now() + delay

    def note_ok(self) -> None:
        self._streak = 0
        self._cooldown_until = None


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
    health: SenderHealth | None = None,
    control: MqttControlState | None = None,
) -> tuple[list[tuple[str, str]], bool]:
    """POST ``readings`` (aligned with ``keys``) via the HTTP batch ingest,
    map the per-item results back onto the store, handle DeviceEvicted /
    ReadingRejected / transient-5xx / ``stopped`` responses, best-effort
    mirror cloud-accepted readings over MQTT (fire-and-forget, additive), and
    update ``cadence`` from the response. Shared by both the bootstrap/full
    HTTP path and the MQTT-primary fallback path (Task 3) so this logic is
    never duplicated.

    Returns ``(sent_keys, response_ok)`` where ``response_ok`` is True iff
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
        return [], False
    except ReadingRejected as exc:
        if exc.status == 401:
            # AUTH failure, not a reading failure: the key is dead (rotated /
            # re-paired elsewhere). The rows are fine — retrying them cannot
            # help, so leave them pending, and back off HARD. Before 0.17.1
            # this path re-sent every 5s forever.
            _LOGGER.warning(
                "cloud rejected the API key (401); backing off %ss. If this "
                "persists, remove and re-pair the Svitgrid integration.",
                AUTH_COOLDOWN_S,
            )
            if health is not None:
                health.note_auth_failure()
            return [], False
        await _maybe(store.mark_failed(keys, now_iso))
        if health is not None:
            health.note_batch_failure()
        return [], False

    if body is None:  # transient 5xx
        # Leave the rows PENDING — a server outage must not burn the per-row
        # give-up budget (MAX_SEND_ATTEMPTS) of readings that will succeed on
        # recovery. The sender-wide backoff below paces the retries instead.
        if health is not None:
            health.note_batch_failure()
        return [], False

    if isinstance(body, dict) and body.get("stopped"):
        _LOGGER.warning(
            "cloud reports device stopped (%s); leaving %d row(s) pending",
            body.get("stoppedReason"),
            len(keys),
        )
        if lifecycle is not None:
            lifecycle.pause(str(body.get("stoppedReason") or "stopped"), now_iso)
            await _maybe(store.set_lifecycle(lifecycle.state, lifecycle.reason, lifecycle.since))
        return [], True

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
    # Any accepted reading proves the pipe works; a batch where EVERY item
    # failed escalates the sender-wide backoff (these rows burn attempts and
    # give up at MAX_SEND_ATTEMPTS, but pacing stops the 5s hammering the
    # three prod installs exhibited before 0.17.1).
    if health is not None:
        if sent_keys:
            health.note_ok()
        elif failed_keys:
            health.note_batch_failure()

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

    # The HTTP response is also the MQTT-primary switch, as it is for the edge
    # firmware and the mobile harvester. Before 2026-10-25 only a
    # `devices/{id}/config` push set it, and the server sends that push only
    # on a cadence change or refresh-now, so an install stayed on HTTP and
    # fell back to it on every restart. An absent field leaves it unchanged.
    if control is not None and isinstance(body, dict) and "mqttPublishReadings" in body:
        control.mqtt_primary = bool(body["mqttPublishReadings"])

    if control is not None:
        # The ack carries the server's mode as well as its interval; in Live
        # the read loops sample every checkSec, not at the 30 s interval.
        if isinstance(body, dict):
            apply_cadence(control, body)
        eff = effective_interval_s(control)
        if eff is not None and eff > 0:
            cadence.interval_s = eff
    else:
        interval_ms = body.get("ingestIntervalMs") if isinstance(body, dict) else None
        if isinstance(interval_ms, (int, float)) and interval_ms > 0:
            cadence.interval_s = int(interval_ms / 1000)

    return sent_keys, True


def _epoch_s(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _gate_rows(
    rows: list[dict], control: MqttControlState, live_gates: dict[str, LiveGate]
) -> tuple[list[dict], list[tuple[str, str]], list[dict]]:
    """Split sendable rows into (to_send, skipped_keys, retries) by the Live gate.

    Rows are judged oldest first by their own timestamps, so a backlog
    drained in one batch keeps the same spacing it would have had live. A
    passed row is recorded as sent at once, so the next row compares with
    it; the upload's outcome corrects that afterwards (_record_outcomes). A
    row at or before the gate's last attempt is a retry of a row the gate
    already passed (rejected rows never come back), and it goes out ungated.
    """
    params = control.live.params
    to_send: list[dict] = []
    skipped: list[tuple[str, str]] = []
    retries: list[dict] = []
    for r in rows:
        gate = live_gates.setdefault(r["inverter_id"], LiveGate())
        t = _epoch_s(r["ts"])
        if gate.has_attempted and t <= gate.last_attempt_s:
            to_send.append(r)
            retries.append(r)
        elif gate.should_send(r["payload"], t, params):
            gate.record_attempt(r["payload"], t, ok=True)
            to_send.append(r)
        else:
            skipped.append((r["inverter_id"], r["ts"]))
    return to_send, skipped, retries


def _record_outcomes(
    to_send: list[dict],
    retries: list[dict],
    sent_keys: set[tuple[str, str]],
    live_gates: dict[str, LiveGate],
) -> None:
    """Tell each gate how its rows fared. A row that did not go out holds the
    gate until one heartbeat after it; a retry that did go out counts as sent."""
    retry_ids = {id(r) for r in retries}
    failed: dict[str, dict] = {}
    for r in to_send:
        key = (r["inverter_id"], r["ts"])
        gate = live_gates[r["inverter_id"]]
        if key not in sent_keys:
            failed[r["inverter_id"]] = r  # the latest failed row per inverter wins
        elif id(r) in retry_ids and _epoch_s(r["ts"]) >= gate.last_attempt_s:
            gate.record_attempt(r["payload"], _epoch_s(r["ts"]), ok=True)
    for inv, r in failed.items():
        live_gates[inv].record_attempt(r["payload"], _epoch_s(r["ts"]), ok=False)


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
    health: SenderHealth | None = None,
    live_gates: dict[str, LiveGate] | None = None,
) -> int:
    """Drain at most one batch. Returns number of rows marked 'sent'.

    ``live_gates`` (one LiveGate per inverter, owned by the sender loop) turns
    on the Live gate while ``control`` says Live: a row the gate rejects is
    marked sent without being uploaded, so the cloud sees only the rows that
    moved or the heartbeat, and the local store keeps every row for the panel
    and the rollups. Leaving Live clears the gates.

    ``discharge_positive_ids`` names inverters whose battery power we normalized
    to Svitgrid's charge-positive convention at capture (HA Solarman). We
    re-invert those before upload so the cloud keeps receiving the raw
    discharge-positive value its `home_assistant_solarman` handler negates —
    the server contract is unchanged. See battery_sign.py.

    ``control`` (Task 3) is the shared ``MqttControlState`` updated by the
    MQTT wake client from `devices/{deviceId}/config` and, for
    ``mqtt_primary``, by every HTTP response's ``mqttPublishReadings``. When it says the
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
    if control is not None:
        eff = effective_interval_s(control)
        if eff is not None and eff > 0:
            cadence.interval_s = eff

    # Failure backoff (0.17.1): while cooling down, the drain is a complete
    # no-op — no store queries, no HTTP. The store's data-available event may
    # still wake the loop on every captured reading; this guard makes those
    # wake-ups free.
    if health is not None and health.in_cooldown():
        return 0

    # Age out anything beyond the backfill cap so it never clogs the queue.
    await _maybe(store.skip_aged(now_iso, cap_s))

    rows = await _maybe(store.get_sendable(now_iso, cap_s, batch_max))
    if not rows:
        return 0

    gating = live_gates is not None and control is not None and control.live.live
    if live_gates is not None and not gating:
        live_gates.clear()
    retries: list[dict] = []
    skipped_count = 0
    if gating:
        rows, skipped, retries = _gate_rows(rows, control, live_gates)
        if skipped:
            await _maybe(store.mark_sent(skipped))
            skipped_count = len(skipped)
        if not rows:
            return skipped_count

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

        http_sent: list[tuple[str, str]] = []
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
                health=health,
                control=control,
            )
        if gating:
            _record_outcomes(rows, retries, set(acked_keys) | set(http_sent), live_gates)
        return skipped_count + len(acked_keys) + len(http_sent)

    # Bootstrap (first drain this session) or MQTT-primary not ready: HTTP for
    # every row, as before Task 3. A real cloud response proves the HTTP path
    # is up this session, so later drains may switch to MQTT-primary.
    sent_keys, response_ok = await _http_send(
        store=store,
        api_client=api_client,
        api_key=api_key,
        now_iso=now_iso,
        cadence=cadence,
        lifecycle=lifecycle,
        keys=keys,
        readings=readings,
        publisher=publisher,
        health=health,
        control=control,
    )
    if control is not None and response_ok:
        control.bootstrapped = True
    if gating:
        _record_outcomes(rows, retries, set(sent_keys), live_gates)
    return skipped_count + len(sent_keys)


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
    # One health tracker per loop: failure backoff spans drains (0.17.1).
    health = SenderHealth()
    # One Live gate per inverter, for the life of the loop (cleared on leaving Live).
    live_gates: dict[str, LiveGate] = {}
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
                    health=health,
                    live_gates=live_gates,
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
