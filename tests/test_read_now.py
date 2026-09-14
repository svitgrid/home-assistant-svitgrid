"""#7: on-demand read for direct-harvest inverters.

poll_now (the app's refresh), the "Read now" button entity and the panel's
read-now endpoint each request exactly one immediate poll of the inverter's
harvest loop. Entity-relay inverters have no trigger, so poll_now stays an ACK.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.activity import ActivityTracker
from custom_components.svitgrid.button import SvitgridReadNowButton
from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.harvest.read_now import ReadNowTrigger, find_triggers
from custom_components.svitgrid.http_views import (
    SvitgridReadNowView,
    SvitgridSyncStatusView,
    register_views,
)
from custom_components.svitgrid.signing import generate_keypair


def _hass_with(triggers=None, activity=None):
    entry_state = {"read_now": dict(triggers or {})}
    if activity is not None:
        entry_state["activity"] = activity
    return SimpleNamespace(data={DOMAIN: {"entry-1": entry_state}})


class _Req:
    def __init__(self, hass, body=None, authenticated=True):
        self.app = {"hass": hass}
        self.query = {}
        self.headers = {}
        self._data = {"ha_authenticated": authenticated}
        self._body = body

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


# ── trigger lookup ─────────────────────────────────────────────────────


def test_find_triggers_by_inverter_and_all():
    a, b = ReadNowTrigger(), ReadNowTrigger()
    hass = _hass_with({"inv-a": a, "inv-b": b})
    assert find_triggers(hass, "inv-a") == {"inv-a": a}
    assert find_triggers(hass, None) == {"inv-a": a, "inv-b": b}
    assert find_triggers(hass, "inv-x") == {}
    assert find_triggers(None, "inv-a") == {}


# ── poll_now ────────────────────────────────────────────────────────────


async def _send_poll_now(hass, payload):
    priv, _ = generate_keypair()
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()
    await process_command(
        command={"commandId": "c-poll", "command": "poll_now", "payload": payload},
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=None,
        hass=hass,
        entry=None,
    )
    return api_client


@pytest.mark.asyncio
async def test_poll_now_requests_one_read_of_the_direct_harvest_inverter():
    target, other = ReadNowTrigger(), ReadNowTrigger()
    hass = _hass_with({"inv-a": target, "inv-b": other})

    api_client = await _send_poll_now(hass, {"inverterId": "inv-a"})

    assert target.requested is True
    assert len(target.begin_tick()) == 1  # exactly one pending request
    assert other.requested is False
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True


@pytest.mark.asyncio
async def test_poll_now_twice_before_the_loop_wakes_is_still_one_poll():
    trigger = ReadNowTrigger()
    hass = _hass_with({"inv-a": trigger})
    await _send_poll_now(hass, {"inverterId": "inv-a"})
    await _send_poll_now(hass, {"inverterId": "inv-a"})
    # One wake-up of the loop serves both: begin_tick clears the flag once.
    trigger.begin_tick()
    assert trigger.requested is False


@pytest.mark.asyncio
async def test_poll_now_for_an_entity_relay_inverter_is_still_acked():
    api_client = await _send_poll_now(_hass_with({}), {"inverterId": "ha-relay"})
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True


# ── button ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_now_button_requests_one_poll():
    trigger = ReadNowTrigger()
    button = SvitgridReadNowButton(trigger, "entry-1", "inv-a", "Deye SG04LP3")
    assert button.unique_id == "entry-1_inv-a_read_now"
    await button.async_press()
    assert trigger.requested is True
    assert len(trigger.begin_tick()) == 1


# ── read-now endpoint ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_now_view_returns_the_poll_outcome():
    trigger = ReadNowTrigger()
    view = SvitgridReadNowView(store=None)
    task = asyncio.ensure_future(view.post(_Req(_hass_with({"inv-a": trigger}), body={})))
    await asyncio.sleep(0)
    assert trigger.requested is True
    trigger.end_tick(
        trigger.begin_tick(),
        {"outcome": "incomplete", "missingFields": ["loadPower"], "detail": ""},
    )
    resp = await task
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["results"] == [
        {
            "inverterId": "inv-a",
            "outcome": "incomplete",
            "missingFields": ["loadPower"],
            "detail": "",
        }
    ]


@pytest.mark.asyncio
async def test_read_now_view_reports_busy_while_a_poll_is_in_flight():
    trigger = ReadNowTrigger()
    trigger.begin_tick()
    view = SvitgridReadNowView(store=None)
    resp = await view.post(_Req(_hass_with({"inv-a": trigger}), body={"inverter_id": "inv-a"}))
    body = json.loads(resp.body)
    assert body["results"][0]["outcome"] == "busy"


@pytest.mark.asyncio
async def test_read_now_view_without_direct_harvest_inverters():
    view = SvitgridReadNowView(store=None)
    resp = await view.post(_Req(_hass_with({})))
    body = json.loads(resp.body)
    assert body["results"] == []
    assert body["reason"] == "no_direct_harvest"


@pytest.mark.asyncio
async def test_read_now_view_requires_auth():
    trigger = ReadNowTrigger()
    view = SvitgridReadNowView(store=None)
    resp = await view.post(_Req(_hass_with({"inv-a": trigger}), authenticated=False))
    assert resp.status == 401
    assert trigger.requested is False


def test_read_now_view_is_registered():
    registered = []
    hass = SimpleNamespace(http=SimpleNamespace(register_view=registered.append))
    register_views(hass, store=None)
    assert any(isinstance(v, SvitgridReadNowView) for v in registered)


# ── sync-status: the last poll attempt ─────────────────────────────────


class _EmptyStore:
    async def sync_status(self):
        return {"counts": {}, "last_sent_ts": None, "cloud_ingest_enabled": True}


@pytest.mark.asyncio
async def test_sync_status_carries_the_last_gated_attempt():
    activity = ActivityTracker()
    activity.record_ingest_skipped(missing_fields=["loadPower"], entities={})
    view = SvitgridSyncStatusView(_EmptyStore())
    resp = await view.get(_Req(_hass_with(activity=activity)))
    body = json.loads(resp.body)
    assert body["last_sent_ts"] is None
    assert body["last_attempt"]["status"] == "skipped"
    assert body["last_attempt"]["missing_fields"] == ["loadPower"]
    assert body["last_attempt"]["at"]


@pytest.mark.asyncio
async def test_sync_status_carries_the_last_failed_attempt():
    activity = ActivityTracker()
    activity.record_ingest_failure(reason="logger unreachable")
    view = SvitgridSyncStatusView(_EmptyStore())
    body = json.loads((await view.get(_Req(_hass_with(activity=activity)))).body)
    assert body["last_attempt"]["status"] == "error"
    assert body["last_attempt"]["reason"] == "logger unreachable"


@pytest.mark.asyncio
async def test_sync_status_last_attempt_is_null_before_any_poll():
    view = SvitgridSyncStatusView(_EmptyStore())
    body = json.loads((await view.get(_Req(_hass_with(activity=ActivityTracker())))).body)
    assert body["last_attempt"] is None
