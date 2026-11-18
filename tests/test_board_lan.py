"""Board management over the LAN on an edge island (svitgrid #1312).

`POST /api/svitgrid/edge/box-command` queues one board-level command for one
adopted box: the app's reboot, pause and resume (svitgrid D-2165). Each is a
signed request by a trusted key, as every other edge management write is,
and the box's answer comes back on `/edge/commands/{id}`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN
from tests.test_island_lan import (
    NOW_MS,
    _Admin,
    _adopt,
    _body,
    _entry,
    _inv,
    _keystore,
    _Req,
)

BOX_KEY = "box-1-key"


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


class _Store:
    """The parts of the reading store the box views and the status read."""

    def __init__(self) -> None:
        self.box_states: dict = {}

    async def get_edge_box_state(self, device_id):
        return self.box_states.get(device_id)

    async def set_edge_box_state(self, device_id, state):
        self.box_states[device_id] = dict(state)

    async def get_settings_snapshot(self, inverter_id):
        return None


class _BoxReq(_Req):
    """A request from the box itself: its API key, and a path parameter."""

    def __init__(self, hass, body=None, *, command_id: str = "") -> None:
        super().__init__(hass, body, island_key=None)
        self.headers = {"X-API-Key": BOX_KEY}
        self.match_info = {"command_id": command_id} if command_id else {}


async def _setup(hass, tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    store = _Store()
    hass.data[DOMAIN][entry.entry_id] = {"store": store, "edge_queue": queue}
    await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    await _adopt(hass, admin, "EC-2", _inv("inv-2"))
    await ks.async_bind_edge_box("EC-1", BOX_KEY)
    return admin, ks, queue, store


async def _send(hass, admin, device_id, command, **fields):
    from custom_components.svitgrid.board_lan import SvitgridEdgeBoxCommandView

    return await SvitgridEdgeBoxCommandView().post(
        _Req(
            hass,
            admin.request("edge_box_command", deviceId=device_id, command=command, **fields),
        )
    )


async def _ack(hass, command_id, **body):
    from custom_components.svitgrid.edge_api import EdgeCommandAckView

    return await EdgeCommandAckView().post(_BoxReq(hass, body, command_id=command_id), command_id)


async def _command_state(hass, command_id):
    from custom_components.svitgrid.island_lan import SvitgridEdgeCommandView

    return _body(await SvitgridEdgeCommandView().get(_Req(hass), command_id))


# ── reboot, pause, resume (D-2165) ──────────────────────────────────────────


@pytest.mark.parametrize("command", ["reboot", "pause", "resume"])
async def test_a_board_command_is_queued_for_its_box_alone(hass: HomeAssistant, tmp_path, command):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-1", command)

    assert resp.status == 200, _body(resp)
    command_id = _body(resp)["commandId"]
    assert _body(resp) == {"success": True, "commandId": command_id, "status": "pending"}
    assert await queue.async_pending_count("EC-2") == 0
    row = await queue.async_next_pending("EC-1")
    assert row["id"] == command_id
    assert row["command"] == command
    assert row["payload"] == {}
    assert row["inverter_id"] is None
    assert row["signing"] is None
    assert row["source"] == "board"
    # The box reads it as the cloud sends it: no signature, no inverter.
    sent = _command_for_box(row)
    assert sent == {"id": command_id, "command": command, "payload": {}, "inverterId": None}


async def test_the_box_answer_comes_back_on_the_command_route(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)
    command_id = _body(await _send(hass, admin, "EC-1", "reboot"))["commandId"]
    await queue.async_next_pending("EC-1")

    resp = await _ack(hass, command_id, success=True)

    assert resp.status == 200, _body(resp)
    assert (await _command_state(hass, command_id))["state"] == "success"


@pytest.mark.parametrize(("command", "paused"), [("pause", True), ("resume", False)])
async def test_a_pause_or_resume_the_box_confirms_sets_harvest_paused(
    hass: HomeAssistant, tmp_path, command, paused
):
    """A paused box uploads no telemetry, so the `harvestPaused` its batches
    carry would never say it paused: the box's answer does."""
    admin, _, queue, store = await _setup(hass, tmp_path)
    store.box_states["EC-1"] = {"harvestPaused": not paused, "version": "2.180.0"}
    command_id = _body(await _send(hass, admin, "EC-1", command))["commandId"]
    await queue.async_next_pending("EC-1")

    await _ack(hass, command_id, success=True)

    assert store.box_states["EC-1"]["harvestPaused"] is paused
    assert store.box_states["EC-1"]["version"] == "2.180.0"


async def test_a_refused_pause_leaves_harvest_paused_alone(hass: HomeAssistant, tmp_path):
    admin, _, queue, store = await _setup(hass, tmp_path)
    store.box_states["EC-1"] = {"harvestPaused": False}
    command_id = _body(await _send(hass, admin, "EC-1", "pause"))["commandId"]
    await queue.async_next_pending("EC-1")

    await _ack(hass, command_id, success=False, reason="busy")

    assert store.box_states["EC-1"]["harvestPaused"] is False
    state = await _command_state(hass, command_id)
    assert state["state"] == "failed"
    assert state["reason"] == "busy"


async def test_an_unknown_command_is_refused(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-1", "format_flash")

    assert resp.status == 400
    assert _body(resp) == {"error": "unknown_command"}
    assert await queue.async_pending_count("EC-1") == 0


async def test_an_unknown_box_is_404(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-9", "reboot")

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_box"}


async def test_a_board_command_needs_a_trusted_signature(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.board_lan import SvitgridEdgeBoxCommandView

    admin, _, queue, _ = await _setup(hass, tmp_path)
    fields = {"deviceId": "EC-1", "command": "reboot"}

    no_key = await SvitgridEdgeBoxCommandView().post(
        _Req(hass, admin.request("edge_box_command", **fields), island_key=None)
    )
    assert no_key.status == 401
    untrusted = await SvitgridEdgeBoxCommandView().post(
        _Req(hass, _Admin().request("edge_box_command", **fields))
    )
    assert untrusted.status == 403
    other_action = await SvitgridEdgeBoxCommandView().post(
        _Req(hass, admin.request("edge_leave", **fields))
    )
    assert other_action.status == 400
    no_device = await SvitgridEdgeBoxCommandView().post(
        _Req(hass, admin.request("edge_box_command", command="reboot"))
    )
    assert no_device.status == 400
    assert await queue.async_pending_count("EC-1") == 0


async def test_no_adopted_box_is_404(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)

    resp = await _send(hass, admin, "EC-1", "reboot")

    assert resp.status == 404
    assert _body(resp) == {"error": "no_edge_boxes"}


async def test_island_status_advertises_board_commands(hass: HomeAssistant):
    from custom_components.svitgrid.island_lan import SvitgridIslandStatusView

    await _keystore(hass, _Admin())
    _entry(hass)

    status = await SvitgridIslandStatusView().get(_Req(hass))

    assert "edge_box_commands" in json.loads(status.body)["capabilities"]


# ── rename, factory reset, remove (D-2166) ──────────────────────────────────


def _reset_command(admin: _Admin, **signed_fields) -> dict:
    """The phone-signed `factory_reset` the box itself verifies
    (`reset_event_builder.dart`)."""
    from custom_components.svitgrid.signing import sign_payload

    signed = {
        "command": "factory_reset",
        "confirm": True,
        "deviceId": "EC-1",
        "validUntil": "2026-10-29T10:20:00.000Z",
        **signed_fields,
    }
    return {
        "signedEventData": signed,
        "signature": sign_payload(signed, admin.priv),
        "signingKeyId": admin.key_id,
    }


async def _rename(hass, admin, device_id, name):
    from custom_components.svitgrid.board_lan import SvitgridEdgeRenameView

    return await SvitgridEdgeRenameView().post(
        _Req(hass, admin.request("edge_rename", deviceId=device_id, name=name))
    )


async def _edge_status(hass) -> dict:
    from custom_components.svitgrid.island_lan import SvitgridEdgeStatusView

    return {
        b["deviceId"]: b for b in _body(await SvitgridEdgeStatusView().get(_Req(hass)))["boxes"]
    }


async def test_rename_sets_the_box_name_home_assistant_serves(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.island_lan import SvitgridIslandStatusView

    admin, ks, _, _ = await _setup(hass, tmp_path)

    resp = await _rename(hass, admin, "EC-1", "  Гараж  ")

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "name": "Гараж"}
    assert (await ks.load()).edge_boxes["EC-1"].name == "Гараж"
    boxes = await _edge_status(hass)
    assert boxes["EC-1"]["name"] == "Гараж"
    # A box never renamed here has no name to serve.
    assert "name" not in boxes["EC-2"]
    island = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert {b["deviceId"]: b.get("name") for b in island["edgeBoxes"]} == {
        "EC-1": "Гараж",
        "EC-2": None,
    }


async def test_a_name_survives_adopting_the_box_again(hass: HomeAssistant, tmp_path):
    admin, ks, _, _ = await _setup(hass, tmp_path)
    await _rename(hass, admin, "EC-1", "Гараж")

    await _adopt(hass, admin, "EC-1", _inv("inv-1"))

    assert (await ks.load()).edge_boxes["EC-1"].name == "Гараж"


@pytest.mark.parametrize("name", ["", "   ", 7, None, "x" * 65])
async def test_a_blank_or_long_name_is_refused(hass: HomeAssistant, tmp_path, name):
    admin, ks, _, _ = await _setup(hass, tmp_path)

    resp = await _rename(hass, admin, "EC-1", name)

    assert resp.status == 400
    assert (await ks.load()).edge_boxes["EC-1"].name is None


async def test_rename_of_an_unknown_box_is_404(hass: HomeAssistant, tmp_path):
    admin, _, _, _ = await _setup(hass, tmp_path)

    resp = await _rename(hass, admin, "EC-9", "Гараж")

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_box"}


async def test_a_factory_reset_carries_the_phone_signature_to_the_box(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, _, queue, _ = await _setup(hass, tmp_path)
    box_command = _reset_command(admin)

    resp = await _send(
        hass, admin, "EC-1", "factory_reset", payload={"confirm": True}, boxCommand=box_command
    )

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert row["command"] == "factory_reset"
    assert row["payload"] == {"confirm": True}
    assert row["signing"] == box_command
    sent = _command_for_box(row)
    assert sent["signedEventData"] == box_command["signedEventData"]
    assert sent["signature"] == box_command["signature"]
    assert sent["signingKeyId"] == box_command["signingKeyId"]


async def test_a_factory_reset_without_the_box_signature_is_refused(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-1", "factory_reset", payload={"confirm": True})

    assert resp.status == 400
    assert _body(resp) == {"error": "box_signature_required"}
    assert await queue.async_pending_count("EC-1") == 0


async def test_a_signature_over_another_command_is_never_a_reset(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(
        hass,
        admin,
        "EC-1",
        "factory_reset",
        payload={"confirm": True},
        boxCommand=_reset_command(admin, command="zigbee_factory_reset"),
    )

    assert resp.status == 400
    assert _body(resp) == {"error": "box_signature_invalid"}
    assert await queue.async_pending_count("EC-1") == 0


async def test_a_reset_the_box_confirms_forgets_the_box(hass: HomeAssistant, tmp_path):
    """As the cloud deletes the board once it answers its reset (#1306)."""
    admin, ks, queue, _ = await _setup(hass, tmp_path)
    resp = await _send(
        hass,
        admin,
        "EC-1",
        "factory_reset",
        payload={"confirm": True},
        boxCommand=_reset_command(admin),
    )
    command_id = _body(resp)["commandId"]
    await queue.async_next_pending("EC-1")

    await _ack(hass, command_id, success=True)

    boxes = (await ks.load()).edge_boxes
    assert "EC-1" not in boxes
    assert "EC-2" in boxes
    # The app still reads the board's answer.
    assert (await _command_state(hass, command_id))["state"] == "success"


async def test_the_answer_to_the_last_boxs_reset_stays_readable(hass: HomeAssistant, tmp_path):
    """The reload after the last box is forgotten builds no queue (svitgrid
    D-2225): the app still reads the reset's answer, not 404."""
    from custom_components.svitgrid.island_lan import SvitgridEdgeCommandView

    admin, ks, queue, _ = await _setup(hass, tmp_path)
    resp = await _send(
        hass,
        admin,
        "EC-1",
        "factory_reset",
        payload={"confirm": True},
        boxCommand=_reset_command(admin),
    )
    command_id = _body(resp)["commandId"]
    await queue.async_next_pending("EC-1")
    await _ack(hass, command_id, success=True)
    # The reload with no box left: the entry holds no queue.
    for state in hass.data[DOMAIN].values():
        if isinstance(state, dict) and "edge_queue" in state:
            state["edge_queue"] = None

    resp = await SvitgridEdgeCommandView().get(_Req(hass), command_id)

    assert resp.status == 200, _body(resp)
    assert _body(resp)["state"] == "success"
    assert _body(resp)["id"] == command_id
    # An id nobody queued is still 404.
    resp = await SvitgridEdgeCommandView().get(_Req(hass), "nope")
    assert resp.status == 404


async def test_a_refused_reset_keeps_the_box(hass: HomeAssistant, tmp_path):
    admin, ks, queue, _ = await _setup(hass, tmp_path)
    resp = await _send(
        hass,
        admin,
        "EC-1",
        "factory_reset",
        payload={"confirm": True},
        boxCommand=_reset_command(admin),
    )
    command_id = _body(resp)["commandId"]
    await queue.async_next_pending("EC-1")

    await _ack(hass, command_id, success=False, rejected=True, reason="device_id_mismatch")

    assert "EC-1" in (await ks.load()).edge_boxes


# ── add, change, remove an inverter on a board (D-2168) ─────────────────────


async def test_update_inverter_goes_to_the_box_and_home_assistant_follows(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_api import _command_for_box, build_edge_config

    admin, ks, queue, _ = await _setup(hass, tmp_path)
    payload = {
        "inverterId": "inv-1",
        "ipAddress": "192.168.1.77",
        "loggerSerial": 2712345678,
        "modelId": _inv("inv-1")["modelId"],
        "phases": 3,
        "hasBattery": True,
    }

    resp = await _send(hass, admin, "EC-1", "update_inverter", payload=payload)

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert row["command"] == "update_inverter"
    assert row["payload"] == payload
    assert _command_for_box(row)["inverterId"] == "inv-1"
    # Nothing changes here until the box confirms.
    assert (await ks.load()).edge_boxes["EC-1"].inverters["inv-1"].get("ipAddress") != (
        "192.168.1.77"
    )

    await _ack(hass, row["id"], success=True)

    box = (await ks.load()).edge_boxes["EC-1"]
    assert box.inverters["inv-1"]["ipAddress"] == "192.168.1.77"
    assert box.inverters["inv-1"]["loggerSerial"] == 2712345678
    assert box.inverter_ids == ["inv-1"]
    config = build_edge_config(box)
    assert config["inverters"][0]["ipAddress"] == "192.168.1.77"


async def test_add_inverter_adds_it_to_the_box_once_confirmed(hass: HomeAssistant, tmp_path):
    admin, ks, queue, _ = await _setup(hass, tmp_path)
    payload = {
        "inverterId": "inv-3",
        "modelId": "deye_sg04lp3",
        "ipAddress": "192.168.1.78",
        "loggerSerial": 2799999999,
        "phases": 3,
        "hasBattery": True,
        "pollingInterval": 5,
        "reportingInterval": 60,
    }

    resp = await _send(hass, admin, "EC-1", "add_inverter", payload=payload)
    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert row["payload"] == payload
    assert "inv-3" not in (await ks.load()).edge_boxes["EC-1"].inverters

    await _ack(hass, row["id"], success=True)

    box = (await ks.load()).edge_boxes["EC-1"]
    assert box.inverter_ids == ["inv-1", "inv-3"]
    assert box.inverters["inv-3"]["modelId"] == "deye_sg04lp3"
    assert box.inverters["inv-3"]["ipAddress"] == "192.168.1.78"
    # The binding stays: nothing is re-adopted.
    assert box.api_key == BOX_KEY


async def test_remove_inverter_drops_it_once_confirmed(hass: HomeAssistant, tmp_path):
    admin, ks, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-1", "remove_inverter", payload={"inverterId": "inv-1"})
    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")

    await _ack(hass, row["id"], success=True)

    box = (await ks.load()).edge_boxes["EC-1"]
    assert box.inverter_ids == []
    assert box.inverters == {}
    assert box.api_key == BOX_KEY


async def test_a_refused_inverter_change_leaves_home_assistant_alone(hass: HomeAssistant, tmp_path):
    admin, ks, queue, _ = await _setup(hass, tmp_path)
    before = (await ks.load()).edge_boxes["EC-1"]
    await _send(hass, admin, "EC-1", "remove_inverter", payload={"inverterId": "inv-1"})
    row = await queue.async_next_pending("EC-1")

    await _ack(hass, row["id"], success=False)

    after = (await ks.load()).edge_boxes["EC-1"]
    assert after.inverter_ids == before.inverter_ids
    assert after.inverters == before.inverters


async def test_an_inverter_another_box_holds_cannot_be_added(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(
        hass,
        admin,
        "EC-1",
        "add_inverter",
        payload={"inverterId": "inv-2", "modelId": "deye_sg04lp3"},
    )

    assert resp.status == 409
    assert _body(resp) == {"error": "inverter_held_by_other_box"}
    assert await queue.async_pending_count("EC-1") == 0


@pytest.mark.parametrize(
    ("command", "payload", "error"),
    [
        ("update_inverter", {"inverterId": "inv-2", "ipAddress": "1.2.3.4"}, "unknown_inverter"),
        ("remove_inverter", {"inverterId": "inv-9"}, "unknown_inverter"),
        ("update_inverter", {"ipAddress": "1.2.3.4"}, "bad_request"),
        ("add_inverter", {"inverterId": "inv-3"}, "bad_request"),
        ("remove_inverter", None, "bad_request"),
    ],
)
async def test_an_inverter_command_must_name_an_inverter_of_this_box(
    hass: HomeAssistant, tmp_path, command, payload, error
):
    admin, _, queue, _ = await _setup(hass, tmp_path)
    fields = {} if payload is None else {"payload": payload}

    resp = await _send(hass, admin, "EC-1", command, **fields)

    assert resp.status in (400, 404)
    assert _body(resp)["error"] == error
    assert await queue.async_pending_count("EC-1") == 0


# ── register dump, RS-485 discover, logger rescan, probe (D-2169) ───────────


@pytest.mark.parametrize(
    ("command", "payload", "inverter_id"),
    [
        ("register_dump", {"inverterId": "inv-1"}, "inv-1"),
        ("rs485_discover", None, None),
        ("rescan_logger", {"inverterId": "inv-1", "subnet": "192.168.1.0/24"}, "inv-1"),
        (
            "probe_inverter",
            {"ipAddress": "192.168.1.20", "loggerSerial": 2712345678, "modelId": "deye_sg04lp3"},
            None,
        ),
    ],
)
async def test_a_diagnostic_command_is_queued_as_the_cloud_sends_it(
    hass: HomeAssistant, tmp_path, command, payload, inverter_id
):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, _, queue, _ = await _setup(hass, tmp_path)
    fields = {} if payload is None else {"payload": payload}

    resp = await _send(hass, admin, "EC-1", command, **fields)

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert row["command"] == command
    assert row["payload"] == (payload or {})
    assert row["signing"] is None
    assert _command_for_box(row)["inverterId"] == inverter_id


async def test_a_register_dump_result_comes_back_whole(hass: HomeAssistant, tmp_path):
    """The board gzips its ack; the largest dump it sends (256 pairs) comes
    back on the command route as it sent it."""
    import gzip

    from custom_components.svitgrid.edge_api import EdgeCommandAckView

    admin, _, queue, _ = await _setup(hass, tmp_path)
    command_id = _body(
        await _send(hass, admin, "EC-1", "register_dump", payload={"inverterId": "inv-1"})
    )["commandId"]
    await queue.async_next_pending("EC-1")
    result = {
        "inverterId": "inv-1",
        "count": 256,
        "registers": {str(addr): 65535 - addr for addr in range(256)},
    }
    raw = gzip.compress(json.dumps({"success": True, "result": result}).encode())

    request = _BoxReq(hass, command_id=command_id)
    request.headers["Content-Encoding"] = "gzip"

    async def read():
        return raw

    request.read = read
    resp = await EdgeCommandAckView().post(request, command_id)

    assert resp.status == 200, _body(resp)
    state = await _command_state(hass, command_id)
    assert state["state"] == "success"
    assert state["result"] == result


async def test_a_diagnostic_payload_must_be_an_object(hass: HomeAssistant, tmp_path):
    admin, _, queue, _ = await _setup(hass, tmp_path)

    resp = await _send(hass, admin, "EC-1", "register_dump", payload=["inv-1"])

    assert resp.status == 400
    assert await queue.async_pending_count("EC-1") == 0
