"""The edge-facing server: the subset of the cloud edge API an edge box calls.

An edge box on an edge island reports to this integration instead of the
cloud. It keeps its protocol, so these views answer at the cloud's paths and
authenticate the box by its `X-API-Key`, which the keystore binds to an
adopted box (`KeystoreState.edge_boxes`).

Two rules hold on every view here:

- Nothing is reachable until a box is adopted. While the keystore holds no
  edge box, every view answers 404 `{"error":"no_edge_boxes"}` before any auth
  check. HA cannot unregister a view, so the gate lives in the view, and an
  install that never adopts a box sees no change.
- Never 410. A 410 on an edge path evicts the box and reboots it. An unknown
  key is 401 `{"error":"unauthorized"}`.

The paths only the cloud can serve (bootstrap, MQTT, firmware offers,
coredumps, Zigbee) get a fixed answer, 2xx wherever the box only needs to hear
that the call landed: its cloud-contact watchdog restarts it after two hours
without a 2xx.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zlib
from dataclasses import replace
from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from . import edge_reading_pipeline
from .const import DOMAIN
from .keystore import EdgeBoxEntry, KeystoreState
from .settings_read_plan import settings_snapshot_kind_for
from .settings_sync import store_edge_settings_snapshot

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    """The clock the reading pipeline and grid events are judged against. A
    seam, so a test with fixed reading timestamps cannot age past the
    pipeline's 24-hour window."""
    return datetime.now(UTC)


def _server_time() -> str:
    """Now, in the cloud's wire format: `YYYY-MM-DDTHH:MM:SS.fffZ`."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The largest body an edge view inflates from gzip. A settings body is a few
# kilobytes; anything past this is refused rather than held in memory.
MAX_INFLATED_BODY_BYTES = 256 * 1024

# How often the box polls for commands: soon while one waits for it, else
# at the idle cadence (both inside the firmware's 500-600,000 ms clamp).
POLL_INTERVAL_PENDING_MS = 5000
POLL_INTERVAL_IDLE_MS = 30000

# The `telemetry` keys of an ingest batch kept as the box's state
# (`heartbeat-canonical.json` in the firmware's tests names them).
_TELEMETRY_STATE_KEYS = ("version", "uptime", "wifiRssi", "otaStatus")

# The per-inverter keys of the cloud's `config` block that the box reads
# (`config_sync_models_from_block`, the reconcile in `cloud_uploader.c`).
_CONFIG_INVERTER_KEYS = (
    "ipAddress",
    "loggerSerial",
    "modelId",
    "protocolId",
    "phases",
    "hasBattery",
    "slaveId",
    "pollingInterval",
    "reportingInterval",
)


def build_edge_config(box: EdgeBoxEntry) -> dict:
    """The `config` block for `box`, in the cloud's keys.

    The box's reconcile prunes every local inverter the block does not list,
    so it lists every inverter the box holds, even one whose adopt details
    are missing. `configHash` is the first 16 hex digits of the SHA-256 of
    the block's canonical JSON (sorted keys, no spaces), without the hash.
    """
    inverter_ids = list(box.inverter_ids)
    inverter_ids += [i for i in box.inverters if i not in inverter_ids]
    inverters = []
    for inverter_id in inverter_ids:
        adopted = box.inverters.get(inverter_id) or {}
        inverters.append(
            {
                "inverterId": inverter_id,
                **{k: adopted[k] for k in _CONFIG_INVERTER_KEYS if adopted.get(k) is not None},
            }
        )
    block = {
        "inverters": inverters,
        "pollingInterval": 60000,
        "reportingInterval": 60000,
        "nonMeteringModels": [],
        "chargers": [],
        "relayChargers": [],
    }
    canonical = json.dumps(block, sort_keys=True, separators=(",", ":"))
    block["configHash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return block


async def _json_body(request):
    """The request's JSON body, or None when it is not JSON.

    The box gzips every body of 256 bytes or more
    (`HTTP_WORKER_COMPRESS_MIN_BYTES`) and aiohttp does not inflate a request
    body by itself.
    """
    if "gzip" not in (request.headers.get("Content-Encoding") or "").lower():
        try:
            return await request.json()
        except ValueError:
            return None
    inflater = zlib.decompressobj(wbits=31)
    try:
        raw = inflater.decompress(await request.read(), MAX_INFLATED_BODY_BYTES)
    except zlib.error:
        return None
    if inflater.unconsumed_tail:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _snapshot_from_settings_body(box: EdgeBoxEntry, body) -> tuple[dict | None, str | None]:
    """The settings snapshot a `sync-settings` body stores, or why not.

    The body is one of the firmware's five: the Deye register batch
    (`startRegister`, `registers`), or a brand body for LuxPower, MUST,
    SMG II or PI30, each with `inverterId` and `modelId`. It is stored as the
    add-on's own settings read stores it, so the settings view serves the two
    alike. The brand body is kept as the box sent it.
    """
    if not isinstance(body, dict):
        return None, "bad_body"
    inverter_id = body.get("inverterId")
    model_id = body.get("modelId")
    if not isinstance(inverter_id, str) or not isinstance(model_id, str):
        return None, "bad_body"
    if inverter_id not in box.inverter_ids and inverter_id not in box.inverters:
        return None, "unknown_inverter"
    adopted_model = (box.inverters.get(inverter_id) or {}).get("modelId")
    if adopted_model is not None and adopted_model != model_id:
        return None, "model_mismatch"
    kind = settings_snapshot_kind_for(model_id)
    if kind is None:
        return None, "nothing_to_configure"
    read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "deye":
        start_register = body.get("startRegister")
        registers = body.get("registers")
        if type(start_register) is not int or not isinstance(registers, list):
            return None, "bad_body"
        return {
            "inverter_id": inverter_id,
            "model_id": model_id,
            "start_register": start_register,
            "registers": list(registers),
            "read_at": read_at,
        }, None
    brand_body = {k: v for k, v in body.items() if k not in ("inverterId", "modelId", "readAt")}
    return {
        "inverter_id": inverter_id,
        "model_id": model_id,
        "kind": kind,
        "body": brand_body,
        "read_at": read_at,
    }, None


class _EdgeView(HomeAssistantView):
    """Base for the edge views: the no-boxes gate and the box's key."""

    requires_auth = False

    @staticmethod
    def _keystore(hass):
        return (hass.data.get(DOMAIN) or {}).get("keystore")

    async def _state(self, request) -> KeystoreState | web.Response:
        """The keystore state, or the 404 every edge view answers while no
        box is adopted. Runs before any auth check."""
        keystore = self._keystore(request.app["hass"])
        state = await keystore.load() if keystore is not None else None
        if state is None or not state.edge_boxes:
            return self.json({"error": "no_edge_boxes"}, status_code=404)
        return state

    async def _box(self, request) -> EdgeBoxEntry | web.Response:
        """The adopted box the request's `X-API-Key` is bound to, or a refusal.

        The pairing-code binding (`X-Pair-Code`) is Task 11; until then an
        unknown key is 401 with or without a code.
        """
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        box = state.edge_box_for_api_key(request.headers.get("X-API-Key") or "")
        if box is None:
            return self.json({"error": "unauthorized"}, status_code=401)
        return box


class EdgeHealthView(_EdgeView):
    url = "/health"
    name = "svitgrid:edge:health"

    async def get(self, request):
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        return self.json({"status": "ok", "timestamp": _server_time()})


class EdgeMeView(_EdgeView):
    url = "/api/v3/me"
    name = "svitgrid:edge:me"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        hass = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        return self.json(
            {
                "executorType": "edge",
                "deviceId": box.device_id,
                "householdId": entries[0].entry_id if entries else None,
            }
        )


class EdgeBootstrapView(_EdgeView):
    """A box never bootstraps here: the bootstrap reads `cloud_ep` only. One
    that reaches this path anyway gets a clear refusal, not a stray 200."""

    url = "/api/v1/edge-devices/bootstrap"
    name = "svitgrid:edge:bootstrap"

    async def post(self, request):
        state = await self._state(request)
        if isinstance(state, web.Response):
            return state
        return self.json({"error": "island_no_bootstrap"}, status_code=404)


class EdgeConfigView(_EdgeView):
    url = "/api/v1/edge-devices/config"
    name = "svitgrid:edge:config"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json(build_edge_config(box))


class EdgeSyncSettingsView(_EdgeView):
    """Feeds the LAN settings mirror (`/api/svitgrid/settings`).

    A body it cannot store answers 200 `{"ok": false, "error"}`: the
    firmware only checks for 200, and a 4xx would make it retry every cycle.
    """

    url = "/api/v1/edge-devices/sync-settings"
    name = "svitgrid:edge:sync-settings"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        snapshot, error = _snapshot_from_settings_body(box, await _json_body(request))
        if error is not None:
            _LOGGER.info("Edge box %s: settings not stored (%s)", box.device_id, error)
            return self.json({"ok": False, "error": error})
        await store_edge_settings_snapshot(request.app["hass"], snapshot)
        return self.json({"ok": True})


def _entry_state(hass) -> dict | None:
    """The first config entry's state holding a reading store: the entry an
    edge box's readings, state and commands belong to."""
    for state in (hass.data.get(DOMAIN) or {}).values():
        if isinstance(state, dict) and state.get("store") is not None:
            return state
    return None


async def _poll_interval_ms(entry_state: dict, box: EdgeBoxEntry) -> int:
    queue = entry_state.get("edge_queue")
    if queue is not None and await queue.async_pending_count(box.device_id) > 0:
        return POLL_INTERVAL_PENDING_MS
    return POLL_INTERVAL_IDLE_MS


async def _stamp_contact(store, box: EdgeBoxEntry, telemetry=None) -> None:
    """Record the box's 2xx as `lastContactAt`, with what its `telemetry`
    says about it. Keys the box did not send keep their last value."""
    state = dict(await store.get_edge_box_state(box.device_id) or {})
    if isinstance(telemetry, dict):
        state.update({k: telemetry[k] for k in _TELEMETRY_STATE_KEYS if k in telemetry})
    state["lastContactAt"] = _server_time()
    await store.set_edge_box_state(box.device_id, state)


async def _previous_reading(store, inverter_id: str) -> dict | None:
    """The inverter's latest stored reading, the cloud's `current/latest`
    the pipeline derives the next one from; None before the first."""
    for row in await store.live_snapshot():
        if row.get("inverterId") == inverter_id and isinstance(row.get("payload"), dict):
            return row["payload"]
    return None


async def _latest_grid_event(store, inverter_id: str) -> dict | None:
    """The box's latest grid event for the inverter, or None. A store that
    keeps no grid events (`add_grid_event`) has none to give."""
    latest = getattr(store, "latest_grid_event", None)
    return await latest(inverter_id) if latest is not None else None


async def _ingest_one(store, box: EdgeBoxEntry, reading) -> dict:
    """Store one reading of the box; the per-item result in the cloud's shape."""
    inverter_id = reading.get("inverterId") if isinstance(reading, dict) else None
    if (
        not isinstance(inverter_id, str)
        or not inverter_id
        or not isinstance(reading.get("timestamp"), str)
    ):
        return {
            "ok": False,
            "inverterId": inverter_id if isinstance(inverter_id, str) else None,
            "error": "Validation error",
            "status": 400,
        }
    if inverter_id not in box.inverters:
        return {
            "ok": False,
            "inverterId": inverter_id,
            "error": "Inverter not found",
            "status": 404,
        }
    prev = edge_reading_pipeline.with_grid_event(
        await _previous_reading(store, inverter_id),
        await _latest_grid_event(store, inverter_id),
    )
    result = edge_reading_pipeline.process(reading, box.inverters[inverter_id], prev, now=_now())
    if result.rejected == "schema":
        # The cloud's schema answers 400, not 422.
        return {
            "ok": False,
            "inverterId": inverter_id,
            "error": "Validation error",
            "status": 400,
        }
    if result.store_data is None:
        return {
            "ok": False,
            "inverterId": inverter_id,
            "error": "Reading rejected",
            "reason": result.rejected,
            "status": 422,
        }
    await store.append(result.store_data)
    return {"ok": True, "inverterId": inverter_id}


class EdgeIngestBatchView(_EdgeView):
    """The box's batch upload. Each reading gets its own result, so a reading
    of an inverter the box does not hold fails alone while the rest are
    stored. Never `dropped` or `stopped`: the integration has no cadence floor
    and never evicts a box."""

    url = "/api/v1/ingest/readings"
    name = "svitgrid:edge:ingest-readings"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        entry_state = _entry_state(request.app["hass"])
        if entry_state is None:
            return self.json({"error": "store_unavailable"}, status_code=503)
        body = await _json_body(request)
        readings = body.get("readings", []) if isinstance(body, dict) else None
        if not isinstance(readings, list):
            return self.json({"error": "Validation error"}, status_code=400)
        store = entry_state["store"]
        results = [await _ingest_one(store, box, reading) for reading in readings]
        # `bms` and `socketReadings` have no reader on an island.
        await _stamp_contact(store, box, body.get("telemetry"))
        return self.json(
            {
                "results": results,
                "pollIntervalMs": await _poll_interval_ms(entry_state, box),
                "mqttPublishReadings": False,
                "ingestMode": "batch",
                "expectedInverterCount": len(box.inverter_ids),
                "serverTime": _server_time(),
            }
        )


class EdgeIngestReadingView(_EdgeView):
    """One reading, answered as the cloud answers the single path."""

    url = "/api/v1/ingest/reading"
    name = "svitgrid:edge:ingest-reading"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        entry_state = _entry_state(request.app["hass"])
        if entry_state is None:
            return self.json({"error": "store_unavailable"}, status_code=503)
        store = entry_state["store"]
        result = await _ingest_one(store, box, await _json_body(request))
        if not result["ok"]:
            refusal = {"error": result["error"]}
            if result.get("reason") is not None:
                refusal["reason"] = result["reason"]
            return self.json(refusal, status_code=result["status"])
        await _stamp_contact(store, box)
        return self.json(
            {
                "success": True,
                "stored": True,
                "deduplicated": False,
                "mqttPublishReadings": False,
                "serverTime": _server_time(),
            }
        )


def _command_for_box(row: dict) -> dict:
    """A queued command in the names the box's `poll_task` reads. The
    phone's signature travels untouched, and only when there is one."""
    command = {
        "id": row["id"],
        "command": row["command"],
        "payload": row["payload"],
        "inverterId": row["inverter_id"],
    }
    signing = row.get("signing") or {}
    for key in ("signedEventData", "signature", "signingKeyId"):
        if key in signing:
            command[key] = signing[key]
    return command


class EdgeCommandsPollView(_EdgeView):
    """The box's command poll: one command at a time, as the box runs one per
    poll. `wait` is ignored (the box sends `wait=0`). Nothing pending is a 200
    with an empty list, never a 204, so the parser path stays the same.
    `config` is left out while the box's `configHash` matches."""

    url = "/api/v3/executors/commands"
    name = "svitgrid:edge:commands"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        entry_state = _entry_state(request.app["hass"]) or {}
        queue = entry_state.get("edge_queue")
        row = await queue.async_next_pending(box.device_id) if queue is not None else None
        body = {
            "commands": [_command_for_box(row)] if row is not None else [],
            "serverTime": _server_time(),
            "executorType": "edge",
            "pollIntervalMs": await _poll_interval_ms(entry_state, box),
            "ingestMode": "batch",
            "periodicPollDisabled": False,
        }
        config = build_edge_config(box)
        if request.query.get("configHash") != config["configHash"]:
            body["config"] = config
        return self.json(body)


class EdgeCommandAckView(_EdgeView):
    """The box's answer to one command. An id this box was not given answers
    200 `{"success": false, "status": "unknown"}`: a 4xx would only make the
    box retry it."""

    url = "/api/v3/executors/commands/{command_id}/ack"
    name = "svitgrid:edge:commands-ack"

    async def post(self, request, command_id: str):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        body = await _json_body(request)
        if not isinstance(body, dict):
            body = {}
        queue = (_entry_state(request.app["hass"]) or {}).get("edge_queue")
        row = None
        if queue is not None:
            row = await queue.async_ack(
                command_id,
                success=body.get("success") is True,
                result=body.get("result"),
                rejected=body.get("rejected"),
                reason=body.get("reason"),
                device_id=box.device_id,
            )
        if row is None:
            return self.json({"success": False, "status": "unknown"})
        return self.json({"success": True, "status": row["state"]})


class EdgeGridEventView(_EdgeView):
    """The box's confirmed grid loss or return, ahead of its next reading.

    Answered as the cloud's `executor-grid-event.ts` answers it: a body it
    cannot parse is 400 and an inverter the box does not hold is 403 (the
    box drops the event on a 403 and retries a 400 three times); a `lost`
    the cloud would not trust is 200 `vetoed` and not stored. A stored event
    holds the inverter's `gridPresent` through the readings after it that
    cannot see the grid, for `EDGE_CONFIRM_STALENESS_MS`.
    """

    url = "/api/v3/executors/grid-event"
    name = "svitgrid:edge:grid-event"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        event, error = edge_reading_pipeline.parse_grid_event(await _json_body(request), now=_now())
        if error is not None:
            return self.json({"error": error}, status_code=400)
        inverter_id = event["inverterId"]
        if inverter_id not in box.inverter_ids and inverter_id not in box.inverters:
            return self.json({"error": "inverter_not_owned"}, status_code=403)
        entry_state = _entry_state(request.app["hass"])
        if entry_state is None:
            return self.json({"error": "store_unavailable"}, status_code=503)
        store = entry_state["store"]
        veto = edge_reading_pipeline.grid_event_veto(
            event, box.inverters.get(inverter_id), await _previous_reading(store, inverter_id)
        )
        if veto == "signals":
            answer = {"ok": True, "vetoed": True}
        elif veto is not None:
            answer = {"ok": True, "vetoed": True, "reason": veto}
        else:
            answer = {"ok": True, "deduped": not await store.add_grid_event(event)}
        if veto is not None:
            _LOGGER.info(
                "Edge box %s: grid event for %s not trusted (%s)", box.device_id, inverter_id, veto
            )
        await _stamp_contact(store, box)
        return self.json(answer)


class EdgeFirmwareCheckView(_EdgeView):
    url = "/api/v3/executors/firmware-check"
    name = "svitgrid:edge:firmware-check"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({"offer": None, "reason": "island"})


class EdgeMqttTokenView(_EdgeView):
    url = "/api/v3/edge-devices/{device_id}/mqtt-token"
    name = "svitgrid:edge:mqtt-token"

    async def post(self, request, device_id: str):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({"error": "island_no_mqtt"}, status_code=404)


class EdgeCoredumpView(_EdgeView):
    """Not stored; the WARNING is the only crash trace an island owner sees."""

    url = "/api/v1/edge-devices/coredump"
    name = "svitgrid:edge:coredump"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        _LOGGER.warning(
            "Edge box %s crashed: resetReason=%s crashSummary=%s",
            box.device_id,
            body.get("resetReason"),
            body.get("crashSummary"),
        )
        return self.json({"stored": False, "reason": "island"})


class EdgePairAttemptsView(_EdgeView):
    url = "/api/v1/edge-devices/{device_id}/pair-attempts"
    name = "svitgrid:edge:pair-attempts"

    async def post(self, request, device_id: str):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({})


class EdgeUpdateInverterIpView(_EdgeView):
    """Records the inverter's new address on the box's entry. An inverter the
    box does not hold is ignored; the answer is 200 either way."""

    url = "/api/v1/edge-devices/update-inverter-ip"
    name = "svitgrid:edge:update-inverter-ip"

    async def post(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            body = {}
        inverter_id = body.get("inverterId")
        ip_address = body.get("ipAddress")
        if (
            isinstance(inverter_id, str)
            and isinstance(ip_address, str)
            and ip_address
            and inverter_id in box.inverters
        ):
            inverters = {
                **box.inverters,
                inverter_id: {**box.inverters[inverter_id], "ipAddress": ip_address},
            }
            keystore = self._keystore(request.app["hass"])
            await keystore.async_add_edge_box(replace(box, inverters=inverters))
        return self.json({})


class EdgeZigbeeSinkView(_EdgeView):
    """Zigbee is a non-goal on an island; the box must not see a 4xx storm."""

    url = "/api/v1/edge-devices/zigbee/device-joined"
    extra_urls = [
        "/api/v1/edge-devices/zigbee/state-update",
        "/api/v1/edge-devices/{device_id}/sensor-readings",
    ]
    name = "svitgrid:edge:zigbee"

    async def post(self, request, **_match):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        return self.json({})


class EdgeTrustedKeysView(_EdgeView):
    url = "/api/v3/executors/trusted-keys"
    name = "svitgrid:edge:trusted-keys"

    async def get(self, request):
        box = await self._box(request)
        if isinstance(box, web.Response):
            return box
        state = await self._keystore(request.app["hass"]).load()
        return self.json(
            {
                "trustedKeys": [
                    {"keyId": key_id, "publicKeyHex": public_key_hex}
                    for key_id, public_key_hex in state.trusted_public_keys_hex.items()
                ]
            }
        )


EDGE_VIEWS = (
    EdgeHealthView,
    EdgeMeView,
    EdgeBootstrapView,
    EdgeConfigView,
    EdgeSyncSettingsView,
    EdgeIngestBatchView,
    EdgeIngestReadingView,
    EdgeCommandsPollView,
    EdgeCommandAckView,
    EdgeGridEventView,
    EdgeFirmwareCheckView,
    EdgeMqttTokenView,
    EdgeCoredumpView,
    EdgePairAttemptsView,
    EdgeUpdateInverterIpView,
    EdgeZigbeeSinkView,
    EdgeTrustedKeysView,
)
