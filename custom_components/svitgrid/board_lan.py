"""Board management over the LAN for the app on an edge island (svitgrid #1312).

On an edge island the board polls Home Assistant for its commands, so a
command the app sends through the cloud never reaches it. This view queues
the app's board-level commands for one adopted box instead:

- `POST /api/svitgrid/edge/box-command` — `signedData`: `{action:
  "edge_box_command", deviceId, command, payload?, boxCommand?, nonce, ts}`,
  signed by a trusted key, as every other edge management write is. Answers
  `{success, commandId, status}`; `GET /api/svitgrid/edge/commands/{id}`
  (`island_lan`) reports how the command stands, with the box's `result` and
  `reason` once it answers.

`BOARD_COMMANDS` lists what it queues. The names and payloads are the ones
the cloud sends (`edge_command_payloads.json` in the monorepo), so the box
runs each as it does from the cloud. `reboot`, `pause` and `resume` are not
on the box's signed-command list (`EDGE_SIGNED_COMMANDS`), so the trusted
key's signature on the request is the whole check, as for the Zigbee
box-wide commands.

`factory_reset` needs `boxCommand`, the phone's signature over
`{command: "factory_reset", confirm, deviceId, validUntil}` for the box,
forwarded untouched; one signed for any other command is 400
`box_signature_invalid`.

`add_inverter`, `update_inverter` and `remove_inverter` carry the cloud's
payload, which names `inverterId`: an inverter another box holds cannot be
added (409), and one this box does not hold cannot be changed or removed
(404 `unknown_inverter`).

- `POST /api/svitgrid/edge/rename` — `signedData`: `{action: "edge_rename",
  deviceId, name, nonce, ts}`: the owner's name for the box, which
  `/edge/status` serves.

What the box's answer changes here is applied when it acks
(`edge_api.apply_board_ack`): a confirmed `pause` or `resume` sets
`harvestPaused`, a confirmed `factory_reset` forgets the box, and a
confirmed inverter command changes Home Assistant's list for the box, so
nothing has to be adopted again.

While no box is adopted the view answers 404 `{"error":"no_edge_boxes"}`
before any auth check, so an install without one sees no change.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .edge_api import EDGE_BOX_LOCK
from .edge_api import _entry_state as _edge_entry_state
from .island_lan import _box_command, _device_id, _json_error, signed_lan_request

_LOGGER = logging.getLogger(__name__)

ACTION_EDGE_BOX_COMMAND = "edge_box_command"
ACTION_EDGE_RENAME = "edge_rename"

# The longest name a box takes, as the cloud's board rename allows.
BOX_NAME_MAX = 64

# The capability `/island/status` lists for this route.
CAPABILITY = "edge_box_commands"

# A tap on the board page is a request about now: a board that comes back
# later than this does not run it.
BOARD_COMMAND_TTL_S = 300

# The board-level commands this route queues, each with an empty payload.
BOARD_COMMANDS = ("reboot", "pause", "resume")
# Commands the box runs only for a phone signature that names them, which
# rides in `boxCommand` untouched: the integration holds no key the box
# trusts. A signature over any other command is never forwarded as one of
# these (svitgrid D-2166).
SIGNED_BOARD_COMMANDS = {"factory_reset": {"confirm": True}}
# The box's own inverter list (svitgrid D-2168), each payload as the cloud
# sends it and naming its `inverterId`. Home Assistant's list for the box
# follows once the box confirms (`edge_api.apply_board_ack`).
INVERTER_COMMANDS = ("add_inverter", "update_inverter", "remove_inverter")


def _held(box) -> set[str]:
    return set(box.inverter_ids) | set(box.inverters)


def _inverter_payload(
    command: str, raw, box_id: str, boxes: dict
) -> tuple[dict | None, web.Response | None]:
    """The payload of an inverter command, or the refusal: it must name an
    inverter; `add_inverter` also its model, and one no other box holds; the
    others one this box holds."""
    if not isinstance(raw, dict):
        return None, _json_error(400, "bad_request", detail="payload must be an object")
    inverter_id = raw.get("inverterId")
    if not isinstance(inverter_id, str) or not inverter_id:
        return None, _json_error(400, "bad_request", detail="payload must name inverterId")
    if command == "add_inverter":
        if not isinstance(raw.get("modelId"), str) or not raw["modelId"]:
            return None, _json_error(400, "bad_request", detail="payload must name modelId")
        for other_id, other in boxes.items():
            if other_id != box_id and inverter_id in _held(other):
                return None, _json_error(409, "inverter_held_by_other_box")
    elif inverter_id not in _held(boxes[box_id]):
        return None, _json_error(404, "unknown_inverter")
    return dict(raw), None


def _payload_for(command: str, signed: dict) -> tuple[dict | None, web.Response | None]:
    """The payload to queue for `command`, or the refusal."""
    if command in BOARD_COMMANDS:
        return {}, None
    if command in SIGNED_BOARD_COMMANDS:
        return dict(SIGNED_BOARD_COMMANDS[command]), None
    if command in INVERTER_COMMANDS:
        # Checked against the box once it is known.
        return {}, None
    return None, _json_error(400, "unknown_command")


def _signing_for(command: str, signed: dict) -> tuple[dict | None, web.Response | None]:
    """The phone's signature for the box, for a command the box checks."""
    if command not in SIGNED_BOARD_COMMANDS:
        return None, None
    if "boxCommand" not in signed:
        return None, _json_error(400, "box_signature_required")
    box_command = _box_command(signed.get("boxCommand"))
    if box_command is None:
        return None, _json_error(400, "box_signature_required")
    if box_command["signedEventData"].get("command") != command:
        return None, _json_error(400, "box_signature_invalid")
    return box_command, None


async def _edge_boxes(hass) -> dict:
    keystore = (hass.data.get(DOMAIN) or {}).get("keystore")
    state = await keystore.load() if keystore is not None else None
    return dict(state.edge_boxes) if state is not None else {}


class SvitgridEdgeBoxCommandView(HomeAssistantView):
    """POST /api/svitgrid/edge/box-command — one board command for one box.

    A command not in `BOARD_COMMANDS` is 400 `unknown_command`, and a box
    this integration does not hold is 404 `unknown_box`.
    """

    url = "/api/svitgrid/edge/box-command"
    name = "api:svitgrid:edge:box-command"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        boxes = await _edge_boxes(hass)
        if not boxes:
            return _json_error(404, "no_edge_boxes")
        signed, error = await signed_lan_request(request, ACTION_EDGE_BOX_COMMAND)
        if error is not None:
            return error
        box_id = _device_id(signed)
        if box_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        command = signed.get("command")
        payload, error = _payload_for(command, signed)
        if error is not None:
            return error
        signing, error = _signing_for(command, signed)
        if error is not None:
            return error
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        inverter_id = None
        if command in INVERTER_COMMANDS:
            payload, error = _inverter_payload(command, signed.get("payload"), box_id, boxes)
            if error is not None:
                return error
            inverter_id = payload["inverterId"]
        queue = (_edge_entry_state(hass) or {}).get("edge_queue")
        if queue is None:
            return _json_error(503, "unavailable")
        command_id = await queue.async_enqueue(
            box_id,
            command,
            payload,
            signing=signing,
            inverter_id=inverter_id,
            source="board",
            ttl_s=BOARD_COMMAND_TTL_S,
        )
        _LOGGER.info("Board command %s queued for edge box %s (%s)", command, box_id, command_id)
        return web.json_response({"success": True, "commandId": command_id, "status": "pending"})


class SvitgridEdgeRenameView(HomeAssistantView):
    """POST /api/svitgrid/edge/rename — the owner's name for one box.

    `signedData`: `{action: "edge_rename", deviceId, name, nonce, ts}`. The
    name is trimmed, and a blank one or one over `BOX_NAME_MAX` characters is
    400 `bad_request`. `/edge/status` and `/island/status` serve it. The box
    itself is not told: the cloud keeps a board's name on its document too.
    Answers `{ok, name}`.
    """

    url = "/api/svitgrid/edge/rename"
    name = "api:svitgrid:edge:rename"
    requires_auth = False

    async def post(self, request) -> web.Response:  # noqa: D102
        hass = request.app["hass"]
        if not await _edge_boxes(hass):
            return _json_error(404, "no_edge_boxes")
        signed, error = await signed_lan_request(request, ACTION_EDGE_RENAME)
        if error is not None:
            return error
        box_id = _device_id(signed)
        if box_id is None:
            return _json_error(400, "bad_request", detail="deviceId must be a string")
        raw = signed.get("name")
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or len(name) > BOX_NAME_MAX:
            return _json_error(
                400, "bad_request", detail=f"name must be 1 to {BOX_NAME_MAX} characters"
            )
        keystore = (hass.data.get(DOMAIN) or {}).get("keystore")
        async with EDGE_BOX_LOCK:
            state = await keystore.load()
            box = state.edge_boxes.get(box_id) if state is not None else None
            if box is None:
                return _json_error(404, "unknown_box")
            await keystore.async_add_edge_box(replace(box, name=name))
        _LOGGER.info("Edge box %s renamed over the LAN", box_id)
        return web.json_response({"ok": True, "name": name})


BOARD_LAN_VIEWS = (SvitgridEdgeBoxCommandView, SvitgridEdgeRenameView)
