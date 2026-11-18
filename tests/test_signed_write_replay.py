"""A signed LAN write captured on the home network cannot be sent again (svitgrid D-2127).

`POST /api/svitgrid/commands` and the signed event writes (`POST /events`,
`PUT` and `DELETE /events/{id}`) sign `{command, payload}` or the event. From
the app build that fixed D-2127 that signed data also carries a `nonce` and a
`ts` (epoch ms). The add-on refuses a nonce it has seen (`403 replayed`) and a
`ts` outside the clock-skew window (`403 stale_request`), as
`signed_lan_request` does. A body with neither key, from an older app, is
accepted while `REQUIRE_SIGNED_WRITE_FRESHNESS` is False and refused once it
is True.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from custom_components.svitgrid.http_views import (
    SvitgridCommandsView,
    SvitgridEventDetailView,
    SvitgridEventsView,
)
from custom_components.svitgrid.signing import generate_keypair, sign_payload
from tests.test_commands_endpoint import (
    INVERTER_ID,
    ISLAND_KEY,
    _install_executor,
    _make_executor,
)
from tests.test_commands_endpoint import (
    _FakeRequest as _CommandsRequest,
)
from tests.test_commands_endpoint import (
    _install_keystore as _install_commands_keystore,
)
from tests.test_events_endpoint import (
    EVENT_ID,
    _install_event_store,
    _make_event,
)
from tests.test_events_endpoint import (
    ISLAND_KEY as EVENTS_ISLAND_KEY,
)
from tests.test_events_endpoint import (
    _FakeRequest as _EventsRequest,
)
from tests.test_events_endpoint import (
    _install_keystore as _install_events_keystore,
)

NOW_MS = 1_763_456_000_000
TEN_MINUTES_MS = 10 * 60 * 1000
KEY_ID = "admin-key-1"
_LAN = "custom_components.svitgrid.island_lan"


@pytest.fixture
def clock():
    with patch(f"{_LAN}._now_ms", return_value=NOW_MS):
        yield


def _command_body(private_key, *, nonce="n-1", ts=NOW_MS, fresh=True, command_id=None):
    payload = {"inverterId": INVERTER_ID, "workMode": "selling_first"}
    signed = {"command": "set_work_mode", "payload": payload}
    if fresh:
        signed = {**signed, "nonce": nonce, "ts": ts}
    body = {
        "command": "set_work_mode",
        "payload": payload,
        "signingKeyId": KEY_ID,
        "signedEventData": signed,
        "signature": sign_payload(signed, private_key),
    }
    if command_id is not None:
        body["commandId"] = command_id
    return body


def _commands_setup(hass):
    private_key, pub_hex = generate_keypair()
    _install_commands_keystore(hass, trusted_public_keys_hex={KEY_ID: pub_hex})
    executor = _make_executor()
    _install_executor(hass, INVERTER_ID, executor)
    return private_key, executor


async def _post_command(hass, view, body):
    return await view.post(_CommandsRequest(hass, island_key_header=ISLAND_KEY, body=body))


# ---------------------------------------------------------------------------
# POST /api/svitgrid/commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_sent_twice_is_refused_the_second_time(hass, clock):
    private_key, executor = _commands_setup(hass)
    view = SvitgridCommandsView()

    first = await _post_command(hass, view, _command_body(private_key, command_id="c-1"))
    # The capture, sent again under a new commandId (or none at all).
    again = await _post_command(hass, view, _command_body(private_key, command_id="c-2"))
    bare = await _post_command(hass, view, _command_body(private_key))

    assert first.status == 200
    assert again.status == 403
    assert json.loads(again.body)["error"] == "replayed"
    assert bare.status == 403
    assert executor.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_command_signed_ten_minutes_ago_is_refused(hass, clock):
    private_key, executor = _commands_setup(hass)

    resp = await _post_command(
        hass, SvitgridCommandsView(), _command_body(private_key, ts=NOW_MS - TEN_MINUTES_MS)
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "stale_request"
    executor.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_fresh_commands_both_run(hass, clock):
    private_key, executor = _commands_setup(hass)
    view = SvitgridCommandsView()

    a = await _post_command(hass, view, _command_body(private_key, nonce="n-a"))
    b = await _post_command(hass, view, _command_body(private_key, nonce="n-b"))

    assert (a.status, b.status) == (200, 200)
    assert executor.dispatch.await_count == 2


@pytest.mark.asyncio
async def test_same_command_id_retry_is_still_answered_deduped(hass, clock):
    private_key, executor = _commands_setup(hass)
    view = SvitgridCommandsView()
    body = _command_body(private_key, command_id="c-1")

    await _post_command(hass, view, body)
    retry = await _post_command(hass, view, body)

    assert retry.status == 200
    assert json.loads(retry.body) == {"ok": True, "deduped": True}
    assert executor.dispatch.await_count == 1


@pytest.mark.asyncio
async def test_a_bad_signature_does_not_spend_the_nonce(hass, clock):
    private_key, executor = _commands_setup(hass)
    view = SvitgridCommandsView()
    forged = _command_body(private_key)
    forged["signature"] = forged["signature"][:-1] + (
        "A" if forged["signature"][-1] != "A" else "B"
    )

    refused = await _post_command(hass, view, forged)
    genuine = await _post_command(hass, view, _command_body(private_key))

    assert refused.status == 403
    assert genuine.status == 200


@pytest.mark.asyncio
async def test_command_with_only_a_nonce_is_bad_request(hass, clock):
    private_key, executor = _commands_setup(hass)
    body = _command_body(private_key, fresh=False)
    signed = {**body["signedEventData"], "nonce": "n-1"}
    body["signedEventData"] = signed
    body["signature"] = sign_payload(signed, private_key)

    resp = await _post_command(hass, SvitgridCommandsView(), body)

    assert resp.status == 400
    executor.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_from_an_older_app_is_accepted_during_the_transition(hass, clock):
    private_key, executor = _commands_setup(hass)

    resp = await _post_command(
        hass, SvitgridCommandsView(), _command_body(private_key, fresh=False)
    )

    assert resp.status == 200
    executor.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_command_from_an_older_app_is_refused_once_required(hass, clock):
    private_key, executor = _commands_setup(hass)

    with patch(f"{_LAN}.REQUIRE_SIGNED_WRITE_FRESHNESS", True):
        resp = await _post_command(
            hass, SvitgridCommandsView(), _command_body(private_key, fresh=False)
        )

    assert resp.status == 400
    executor.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Event writes
# ---------------------------------------------------------------------------


def _events_setup(hass, initial_events=None):
    private_key, pub_hex = generate_keypair()
    _install_events_keystore(hass, trusted_public_keys_hex={KEY_ID: pub_hex})
    store = _install_event_store(hass, initial_events=initial_events)
    return private_key, store


def _write_body(private_key, *, nonce="n-1", ts=NOW_MS, fresh=True):
    event = _make_event()
    if fresh:
        event = {**event, "nonce": nonce, "ts": ts}
    return {
        "event": dict(event),
        "signingKeyId": KEY_ID,
        "signedEventData": dict(event),
        "signature": sign_payload(event, private_key),
    }


def _delete_body(private_key, *, nonce="n-1", ts=NOW_MS, fresh=True):
    signed = {"event_id": EVENT_ID}
    if fresh:
        signed = {**signed, "nonce": nonce, "ts": ts}
    return {
        "signingKeyId": KEY_ID,
        "signedEventData": signed,
        "signature": sign_payload(signed, private_key),
    }


def _events_request(hass, body):
    return _EventsRequest(hass, island_key_header=EVENTS_ISLAND_KEY, body=body)


@pytest.mark.asyncio
async def test_event_create_sent_twice_is_refused_the_second_time(hass, clock):
    private_key, store = _events_setup(hass)
    view = SvitgridEventsView()
    body = _write_body(private_key)

    first = await view.post(_events_request(hass, body))
    again = await view.post(_events_request(hass, body))

    assert first.status == 200
    assert again.status == 403
    assert json.loads(again.body)["error"] == "replayed"
    assert len(store.upsert_calls) == 1


@pytest.mark.asyncio
async def test_event_create_signed_ten_minutes_ago_is_refused(hass, clock):
    private_key, store = _events_setup(hass)

    resp = await SvitgridEventsView().post(
        _events_request(hass, _write_body(private_key, ts=NOW_MS - TEN_MINUTES_MS))
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "stale_request"
    assert store.upsert_calls == []


@pytest.mark.asyncio
async def test_event_update_sent_twice_is_refused_the_second_time(hass, clock):
    private_key, store = _events_setup(hass, initial_events=[_make_event()])
    view = SvitgridEventDetailView()
    body = _write_body(private_key)

    first = await view.put(_events_request(hass, body), event_id=EVENT_ID)
    again = await view.put(_events_request(hass, body), event_id=EVENT_ID)

    assert first.status == 200
    assert again.status == 403
    assert json.loads(again.body)["error"] == "replayed"
    assert len(store.upsert_calls) == 1


@pytest.mark.asyncio
async def test_event_update_signed_ten_minutes_ago_is_refused(hass, clock):
    private_key, store = _events_setup(hass, initial_events=[_make_event()])

    resp = await SvitgridEventDetailView().put(
        _events_request(hass, _write_body(private_key, ts=NOW_MS - TEN_MINUTES_MS)),
        event_id=EVENT_ID,
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "stale_request"
    assert store.upsert_calls == []


@pytest.mark.asyncio
async def test_event_delete_replayed_after_recreate_is_refused(hass, clock):
    private_key, store = _events_setup(hass, initial_events=[_make_event()])
    view = SvitgridEventDetailView()
    body = _delete_body(private_key)

    first = await view.delete(_events_request(hass, body), event_id=EVENT_ID)
    # The owner makes the event again; the captured delete must not remove it.
    store._events[EVENT_ID] = _make_event()
    again = await view.delete(_events_request(hass, body), event_id=EVENT_ID)

    assert first.status == 200
    assert again.status == 403
    assert json.loads(again.body)["error"] == "replayed"
    assert EVENT_ID in store._events


@pytest.mark.asyncio
async def test_event_delete_signed_ten_minutes_ago_is_refused(hass, clock):
    private_key, store = _events_setup(hass, initial_events=[_make_event()])

    resp = await SvitgridEventDetailView().delete(
        _events_request(hass, _delete_body(private_key, ts=NOW_MS - TEN_MINUTES_MS)),
        event_id=EVENT_ID,
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "stale_request"
    assert EVENT_ID in store._events


@pytest.mark.asyncio
async def test_event_writes_from_an_older_app_are_accepted_during_the_transition(hass, clock):
    private_key, store = _events_setup(hass)

    created = await SvitgridEventsView().post(
        _events_request(hass, _write_body(private_key, fresh=False))
    )
    deleted = await SvitgridEventDetailView().delete(
        _events_request(hass, _delete_body(private_key, fresh=False)), event_id=EVENT_ID
    )

    assert (created.status, deleted.status) == (200, 200)


@pytest.mark.asyncio
async def test_event_writes_from_an_older_app_are_refused_once_required(hass, clock):
    private_key, store = _events_setup(hass, initial_events=[_make_event()])

    with patch(f"{_LAN}.REQUIRE_SIGNED_WRITE_FRESHNESS", True):
        created = await SvitgridEventsView().post(
            _events_request(hass, _write_body(private_key, fresh=False))
        )
        updated = await SvitgridEventDetailView().put(
            _events_request(hass, _write_body(private_key, fresh=False)), event_id=EVENT_ID
        )
        deleted = await SvitgridEventDetailView().delete(
            _events_request(hass, _delete_body(private_key, fresh=False)), event_id=EVENT_ID
        )

    assert (created.status, updated.status, deleted.status) == (400, 400, 400)
    assert store.upsert_calls == []
    assert EVENT_ID in store._events
