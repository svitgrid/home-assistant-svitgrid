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

What the box's answer changes here is applied when it acks
(`edge_api.apply_board_ack`).

While no box is adopted the view answers 404 `{"error":"no_edge_boxes"}`
before any auth check, so an install without one sees no change.
"""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .edge_api import _entry_state as _edge_entry_state
from .island_lan import _device_id, _json_error, signed_lan_request

_LOGGER = logging.getLogger(__name__)

ACTION_EDGE_BOX_COMMAND = "edge_box_command"

# The capability `/island/status` lists for this route.
CAPABILITY = "edge_box_commands"

# A tap on the board page is a request about now: a board that comes back
# later than this does not run it.
BOARD_COMMAND_TTL_S = 300

# The board-level commands this route queues, each with an empty payload.
BOARD_COMMANDS = ("reboot", "pause", "resume")


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
        if command not in BOARD_COMMANDS:
            return _json_error(400, "unknown_command")
        if box_id not in boxes:
            return _json_error(404, "unknown_box")
        queue = (_edge_entry_state(hass) or {}).get("edge_queue")
        if queue is None:
            return _json_error(503, "unavailable")
        command_id = await queue.async_enqueue(
            box_id,
            command,
            {},
            signing=None,
            inverter_id=None,
            source="board",
            ttl_s=BOARD_COMMAND_TTL_S,
        )
        _LOGGER.info("Board command %s queued for edge box %s (%s)", command, box_id, command_id)
        return web.json_response({"success": True, "commandId": command_id, "status": "pending"})


BOARD_LAN_VIEWS = (SvitgridEdgeBoxCommandView,)
