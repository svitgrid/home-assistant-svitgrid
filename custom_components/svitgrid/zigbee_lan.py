"""Zigbee devices over the LAN for the app on an edge island (plan Task 43).

The box reports its Zigbee roster and readings to Home Assistant
(`edge_api.py`, `zigbee_devices.py`); these views serve them to the app and
relay its commands to the box:

- `GET /api/svitgrid/zigbee/devices`, `/zigbee/devices/{id}`,
  `/zigbee/devices/{id}/readings` and `/history` read, island key only,
  shaped like the cloud's `/households/:id/smart-devices`,
  `/smart-devices/:id`, `/readings` and `/history` so the app's models parse
  them. `PATCH /zigbee/devices/{id}` renames a device or moves it to a room.
- `POST /api/svitgrid/edge/zigbee/{id}/command` sends `set_state` signed by a
  trusted (owner or admin) key, and the phone's signature for the box rides
  untouched in `boxCommand`: the integration holds no key the box trusts. The
  unsigned `identify`, `get_device_status` and `remove_device` need the
  island key only (spec 27).
- `POST /api/svitgrid/edge/zigbee/command` sends a box-wide command:
  `list_devices`, `neighbors`, `routes`, `mesh_snapshot` with the island key,
  `zigbee_restart` and `zigbee_factory_reset` signed by a trusted key only.
- `POST /api/svitgrid/edge/permit-join` queues the phone-signed `permit_join`
  and opens the box's pairing window, which `device-joined` reads.

A phone paired view-only holds an island key and no trusted signing key, so
it may read, rename and identify, but never switch a plug or open pairing.
While no box is adopted every view answers 404 `{"error":"no_edge_boxes"}`
before any auth check, so an install without one sees no change.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .edge_api import _entry_state as _edge_entry_state
from .island_lan import _box_command, _device_id, _island_key_ok, _json_error, signed_lan_request

_LOGGER = logging.getLogger(__name__)

ACTION_ZIGBEE_COMMAND = "edge_zigbee_command"
ACTION_ZIGBEE_BOX_COMMAND = "edge_zigbee_box_command"
ACTION_PERMIT_JOIN = "edge_permit_join"

# A tap is a request about now: the box refuses one older than this
# (`USER_TOGGLE_TTL_MS`, `services/api/src/services/command-ttl.ts`).
SET_STATE_TTL_S = 60
# The firmware clamps `permit_join`'s duration to 0-254 seconds.
PERMIT_JOIN_MAX_S = 254
# `SENSOR_HISTORY_BUCKET_MS` and `SENSOR_HISTORY_MAX_HOURS` (`smart-devices.ts`).
SENSOR_BUCKET = timedelta(minutes=10)
SENSOR_HISTORY_MAX_HOURS = 168

# Per-device commands the island key may send (spec 27).
UNSIGNED_DEVICE_COMMANDS = ("identify", "get_device_status", "remove_device")
# Box-wide commands the island key may send, and those a trusted key must sign.
UNSIGNED_BOX_COMMANDS = ("list_devices", "neighbors", "routes", "mesh_snapshot")
OWNER_BOX_COMMANDS = ("zigbee_restart", "zigbee_factory_reset")


async def _edge_boxes(hass) -> dict:
    keystore = (hass.data.get(DOMAIN) or {}).get("keystore")
    state = await keystore.load() if keystore is not None else None
    return dict(state.edge_boxes) if state is not None else {}


def _no_edge_boxes() -> web.Response:
    return _json_error(404, "no_edge_boxes")


def _parts(hass):
    """The Zigbee store and the edge queue, either None when not running."""
    state = _edge_entry_state(hass) or {}
    return state.get("zigbee_devices"), state.get("edge_queue")


async def _body(request) -> dict | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _device_body(device: dict, socket: dict | None, sensor: dict | None) -> dict:
    """One device in the cloud's smart-device shape. A device that has sent a
    sensor reading is a `sensor`; any other is a `switch`."""
    state: dict = {"on": device["on"]}
    if socket is not None:
        state.update(
            power=socket["power"],
            voltage=socket["voltage"],
            current=socket["current"],
            energy=socket["energy"],
            powerAt=socket["timestamp"],
        )
    last_reading = (
        {k: sensor[k] for k in ("temperature", "humidity", "battery") if sensor[k] is not None}
        if sensor is not None
        else None
    )
    return {
        "id": device["id"],
        "edgeDeviceId": device["deviceId"],
        "name": device["name"],
        "type": "sensor" if sensor is not None else "switch",
        "protocol": "zigbee",
        "zigbeeAddr": device["addr"],
        "zigbeeIeee": device["ieee"],
        "zigbeeEndpoint": device["endpoint"],
        "model": device["model"],
        "manufacturer": device["manufacturer"],
        "roomId": device["room"],
        "capabilities": device["caps"],
        "state": state,
        "lastReading": last_reading,
        "lastReadingAt": sensor["captured_at"] if sensor is not None else None,
        "status": "offline" if device["reachable"] is False else "online",
        "unreachableSince": device["unreachableSince"],
        "lastSeen": device["lastSeen"],
        "createdAt": device["addedAt"],
    }


async def _describe(devices, device: dict) -> dict:
    socket, sensor = await devices.async_latest(device["id"])
    return _device_body(device, socket, sensor)


class _ZigbeeView(HomeAssistantView):
    requires_auth = False

    async def _gate(self, request, *, island_key: bool = True):
        """The Zigbee store and the queue, or a refusal: 404 `no_edge_boxes`
        before any auth, then 401 without the island key."""
        hass = request.app["hass"]
        if not await _edge_boxes(hass):
            return _no_edge_boxes()
        if island_key and not await _island_key_ok(request):
            return _json_error(401, "unauthorized")
        devices, queue = _parts(hass)
        if devices is None or queue is None:
            return _json_error(503, "unavailable")
        return devices, queue


class ZigbeeDevicesView(_ZigbeeView):
    """GET /api/svitgrid/zigbee/devices — every device, with its live state."""

    url = "/api/svitgrid/zigbee/devices"
    name = "api:svitgrid:zigbee:devices"

    async def get(self, request) -> web.Response:  # noqa: D102
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, _ = gate
        return web.json_response(
            {"smartDevices": [await _describe(devices, d) for d in await devices.async_list()]}
        )


class ZigbeeDeviceView(_ZigbeeView):
    """GET or PATCH /api/svitgrid/zigbee/devices/{id}. PATCH takes `name`
    and `roomId` (a string, or null to clear the room), island key only."""

    url = "/api/svitgrid/zigbee/devices/{device_id}"
    name = "api:svitgrid:zigbee:device"

    async def get(self, request, device_id: str) -> web.Response:  # noqa: D102
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, _ = gate
        device = await devices.async_get(device_id)
        if device is None:
            return _json_error(404, "unknown_device")
        return web.json_response(await _describe(devices, device))

    async def patch(self, request, device_id: str) -> web.Response:  # noqa: D102
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, _ = gate
        body = await _body(request)
        if body is None or not body.keys() & {"name", "roomId"}:
            return _json_error(400, "bad_request", detail="name or roomId")
        fields = {}
        if "name" in body:
            name = body["name"]
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                return _json_error(400, "bad_request", detail="name must be 1-100 characters")
            fields["name"] = name.strip()
        if "roomId" in body:
            room = body["roomId"]
            if room is not None and (not isinstance(room, str) or not room or len(room) > 100):
                return _json_error(400, "bad_request", detail="roomId must be a string or null")
            fields["room"] = room
        if await devices.async_get(device_id) is None:
            return _json_error(404, "unknown_device")
        await devices.async_update(device_id, fields)
        return web.json_response({"success": True})


def _sensor_points(rows: list[dict], from_dt: datetime, to_dt: datetime) -> list[dict]:
    """`bucketSensorReadings`: temperature and humidity averaged per slot,
    the slot's latest battery kept, and a slot with no reading left out."""
    slots: dict[datetime, dict] = {}
    for row in rows:
        captured = datetime.fromisoformat(row["capturedAt"].replace("Z", "+00:00"))
        if not from_dt <= captured < to_dt:
            continue
        at = from_dt + ((captured - from_dt) // SENSOR_BUCKET) * SENSOR_BUCKET
        acc = slots.setdefault(at, {"t": [], "h": [], "battery": None, "batteryAt": None})
        if row["temperature"] is not None:
            acc["t"].append(row["temperature"])
        if row["humidity"] is not None:
            acc["h"].append(row["humidity"])
        if row["battery"] is not None and (
            acc["batteryAt"] is None or captured >= acc["batteryAt"]
        ):
            acc["battery"], acc["batteryAt"] = row["battery"], captured
    points = []
    for at, acc in sorted(slots.items()):
        point = {"at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
        if acc["t"]:
            point["temperature"] = int(sum(acc["t"]) / len(acc["t"]) + 0.5)
        if acc["h"]:
            point["humidity"] = int(sum(acc["h"]) / len(acc["h"]) + 0.5)
        if acc["battery"] is not None:
            point["battery"] = acc["battery"]
        if len(point) > 1:
            points.append(point)
    return points


class ZigbeeDeviceReadingsView(_ZigbeeView):
    """GET /api/svitgrid/zigbee/devices/{id}/readings?hours= — a sensor's
    history in 10-minute slots: `{hours, bucketMinutes, points: [{at,
    temperature?, humidity?, battery?}]}`. A plug is 400, as in the cloud.
    No subscription gate on an island."""

    url = "/api/svitgrid/zigbee/devices/{device_id}/readings"
    name = "api:svitgrid:zigbee:device:readings"

    async def get(self, request, device_id: str) -> web.Response:  # noqa: D102
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, _ = gate
        if await devices.async_get(device_id) is None:
            return _json_error(404, "unknown_device")
        rows = await devices.async_sensor_readings(device_id)
        if not rows:
            return _json_error(400, "not_a_sensor")
        try:
            hours = int(request.query.get("hours", ""))
        except ValueError:
            hours = 24
        hours = min(max(hours, 1), SENSOR_HISTORY_MAX_HOURS)
        now = datetime.now(UTC)
        bucket_s = int(SENSOR_BUCKET.total_seconds())
        to_dt = datetime.fromtimestamp((int(now.timestamp()) // bucket_s + 1) * bucket_s, UTC)
        from_dt = to_dt - timedelta(hours=hours)
        return web.json_response(
            {
                "hours": hours,
                "bucketMinutes": bucket_s // 60,
                "points": _sensor_points(rows, from_dt, to_dt),
            }
        )


class ZigbeeDeviceHistoryView(_ZigbeeView):
    """GET /api/svitgrid/zigbee/devices/{id}/history?limit= — the commands
    sent to the device, newest first: `{history: [{id, timestamp, event,
    state, commandId, triggeredBy}]}`."""

    url = "/api/svitgrid/zigbee/devices/{device_id}/history"
    name = "api:svitgrid:zigbee:device:history"

    async def get(self, request, device_id: str) -> web.Response:  # noqa: D102
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, _ = gate
        if await devices.async_get(device_id) is None:
            return _json_error(404, "unknown_device")
        try:
            limit = min(max(int(request.query.get("limit", "")), 1), 100)
        except ValueError:
            limit = 50
        return web.json_response({"history": await devices.async_history(device_id, limit)})


def _set_state_command(raw) -> dict | None:
    """The `boxCommand` of a `set_state`: the phone's signature over
    `{command: "set_state", payload: {on}}`, or None."""
    box_command = _box_command(raw)
    if box_command is None:
        return None
    event = box_command["signedEventData"]
    payload = event.get("payload")
    if event.get("command") != "set_state" or not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("on"), bool):
        return None
    return box_command


class ZigbeeDeviceCommandView(_ZigbeeView):
    """POST /api/svitgrid/edge/zigbee/{id}/command — one device's command.

    `set_state` is a signed request, `signedData`: `{action:
    "edge_zigbee_command", command: "set_state", boxCommand, nonce, ts}`,
    where `boxCommand` is the phone's `{signedEventData: {command:
    "set_state", payload: {on}, ...}, signature, signingKeyId}` for the box,
    queued untouched with the device's address and endpoint beside it. Without
    `boxCommand` it is 400 `box_signature_required`, and a request that is not
    signed by a trusted key is refused (`signed_lan_request`).

    `{command: "identify" | "get_device_status" | "remove_device", payload?}`
    needs the island key only. `remove_device` also forgets the device here.
    Answers `{success, commandId, status}`.
    """

    url = "/api/svitgrid/edge/zigbee/{device_id}/command"
    name = "api:svitgrid:edge:zigbee:command"

    async def post(self, request, device_id: str) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _no_edge_boxes()
        body = await _body(request)
        if body is not None and "signedData" in body:
            return await self._set_state(request, device_id, boxes)
        gate = await self._gate(request)
        if isinstance(gate, web.Response):
            return gate
        devices, queue = gate
        if body is None:
            return _json_error(400, "bad_request")
        command = body.get("command")
        if command == "set_state":
            return _json_error(400, "box_signature_required")
        if command not in UNSIGNED_DEVICE_COMMANDS:
            return _json_error(400, "bad_request", detail="unknown command")
        device = await devices.async_get(device_id)
        if device is None:
            return _json_error(404, "unknown_device")
        if device["deviceId"] not in boxes:
            return _json_error(409, "edge_device_missing")
        if command == "remove_device":
            payload = {"addr": device["addr"], "ieee": device["ieee"]}
        else:
            extra = body.get("payload") if isinstance(body.get("payload"), dict) else {}
            payload = {**extra, "addr": device["addr"], "ep": device["endpoint"]}
        command_id = await queue.async_enqueue(
            device["deviceId"], command, payload, signing=None, inverter_id=None, source="zigbee"
        )
        if command == "remove_device":
            await devices.async_remove(device_id)
        return web.json_response({"success": True, "commandId": command_id, "status": "pending"})

    async def _set_state(self, request, device_id: str, boxes: dict) -> web.Response:
        signed, error = await signed_lan_request(request, ACTION_ZIGBEE_COMMAND)
        if error is not None:
            return error
        if signed.get("command") != "set_state":
            return _json_error(400, "bad_request", detail="command must be set_state")
        box_command = _set_state_command(signed.get("boxCommand"))
        if box_command is None:
            return _json_error(400, "box_signature_required")
        devices, queue = _parts(request.app["hass"])
        if devices is None or queue is None:
            return _json_error(503, "unavailable")
        device = await devices.async_get(device_id)
        if device is None:
            return _json_error(404, "unknown_device")
        if device["deviceId"] not in boxes:
            return _json_error(409, "edge_device_missing")
        payload = box_command["signedEventData"]["payload"]
        command_id = await queue.async_enqueue(
            device["deviceId"],
            "set_state",
            payload,
            signing=box_command,
            inverter_id=None,
            source="zigbee",
            ttl_s=SET_STATE_TTL_S,
            target={
                "smartDeviceId": device["id"],
                "zigbeeAddr": device["addr"],
                "zigbeeEndpoint": device["endpoint"],
            },
        )
        await devices.async_add_history(
            device_id, "command_sent", {"on": payload["on"]}, command_id
        )
        return web.json_response({"success": True, "commandId": command_id, "status": "pending"})


class ZigbeeBoxCommandView(_ZigbeeView):
    """POST /api/svitgrid/edge/zigbee/command — a box-wide Zigbee command.

    `{deviceId, command: "list_devices" | "neighbors" | "routes" |
    "mesh_snapshot", payload?}` needs the island key. `zigbee_restart` and
    `zigbee_factory_reset` are owner-only: a signed request, `signedData`:
    `{action: "edge_zigbee_box_command", deviceId, command, nonce, ts}`;
    sent with the island key alone they are 403 `owner_only`. A box this
    integration does not hold is 404 `unknown_box`. Answers `{success,
    commandId, status}`.
    """

    url = "/api/svitgrid/edge/zigbee/command"
    name = "api:svitgrid:edge:zigbee:box-command"

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _no_edge_boxes()
        body = await _body(request)
        if body is not None and "signedData" in body:
            signed, error = await signed_lan_request(request, ACTION_ZIGBEE_BOX_COMMAND)
            if error is not None:
                return error
            command, payload = signed.get("command"), {}
            if command not in OWNER_BOX_COMMANDS:
                return _json_error(400, "bad_request", detail="unknown command")
            box_id = _device_id(signed)
        else:
            gate = await self._gate(request)
            if isinstance(gate, web.Response):
                return gate
            if body is None:
                return _json_error(400, "bad_request")
            command = body.get("command")
            if command in OWNER_BOX_COMMANDS:
                return _json_error(403, "owner_only")
            if command not in UNSIGNED_BOX_COMMANDS:
                return _json_error(400, "bad_request", detail="unknown command")
            payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
            box_id = _device_id(body)
        if box_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        _, queue = _parts(hass)
        if queue is None:
            return _json_error(503, "unavailable")
        command_id = await queue.async_enqueue(
            box_id, command, payload, signing=None, inverter_id=None, source="zigbee"
        )
        return web.json_response({"success": True, "commandId": command_id, "status": "pending"})


def _permit_join_command(raw) -> dict | None:
    """The `boxCommand` of a `permit_join`: the phone's signature over
    `{command: "permit_join", payload: {duration}}`, or None."""
    box_command = _box_command(raw)
    if box_command is None:
        return None
    event = box_command["signedEventData"]
    payload = event.get("payload")
    if event.get("command") != "permit_join" or not isinstance(payload, dict):
        return None
    duration = payload.get("duration")
    if type(duration) is not int or not 1 <= duration <= PERMIT_JOIN_MAX_S:
        return None
    return box_command


class ZigbeePermitJoinView(_ZigbeeView):
    """POST /api/svitgrid/edge/permit-join — open a box for pairing.

    `signedData`: `{action: "edge_permit_join", deviceId, boxCommand, nonce,
    ts}`, `boxCommand` the phone's `{signedEventData: {command:
    "permit_join", payload: {duration}, ...}, signature, signingKeyId}` over
    the box's hardware id (`permit_join_event_builder.dart`). Queued
    untouched; Home Assistant's own window for the box opens for `duration`
    seconds, so a device that joins in it is stored (`device-joined`).
    Answers `{success, commandId, until}`.
    """

    url = "/api/svitgrid/edge/permit-join"
    name = "api:svitgrid:edge:permit-join"

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _no_edge_boxes()
        signed, error = await signed_lan_request(request, ACTION_PERMIT_JOIN)
        if error is not None:
            return error
        box_id = _device_id(signed)
        if box_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        box_command = _permit_join_command(signed.get("boxCommand"))
        if box_command is None:
            return _json_error(400, "box_signature_required")
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        devices, queue = _parts(hass)
        if devices is None or queue is None:
            return _json_error(503, "unavailable")
        payload = box_command["signedEventData"]["payload"]
        command_id = await queue.async_enqueue(
            box_id, "permit_join", payload, signing=box_command, inverter_id=None, source="zigbee"
        )
        until = await devices.async_open_permit_join(box_id, payload["duration"])
        _LOGGER.info("Edge box %s open for Zigbee pairing until %s", box_id, until)
        return web.json_response({"success": True, "commandId": command_id, "until": until})


ZIGBEE_LAN_VIEWS = (
    ZigbeeDevicesView,
    ZigbeeDeviceView,
    ZigbeeDeviceReadingsView,
    ZigbeeDeviceHistoryView,
    ZigbeeDeviceCommandView,
    ZigbeeBoxCommandView,
    ZigbeePermitJoinView,
)
