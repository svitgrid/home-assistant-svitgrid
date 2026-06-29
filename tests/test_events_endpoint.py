"""TDD tests for GET/POST/PUT/DELETE /api/svitgrid/events (Task 2 — island mode SP3).

Written BEFORE implementation (RED phase). Tests cover:
- GET with valid island key → 200 + events list
- GET no key → 401
- POST with key + valid admin signature over the event → 200 + store mutated
- POST with key but NO signature field → 400
- POST with tampered/wrong signature → 403
- POST with bind mismatch (top-level event ≠ signed copy) → 403
- POST no key → 401
- PUT (update) same auth flow as POST
- DELETE signed with correct event_id matching URL → 200 + removed
- DELETE with event_id in signed payload mismatching URL → 403
"""
from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridEventDetailView, SvitgridEventsView
from custom_components.svitgrid.signing import generate_keypair, sign_payload

ISLAND_KEY = "test-island-key-for-events-endpoint"
EVENT_ID = "ev-test-001"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeKeystoreState:
    def __init__(self, trusted_public_keys_hex: dict) -> None:
        self.trusted_public_keys_hex = trusted_public_keys_hex


class _FakeKeystore:
    def __init__(
        self,
        island_key: str | None,
        trusted_public_keys_hex: dict | None = None,
    ) -> None:
        self._island_key = island_key
        self._trusted = trusted_public_keys_hex or {}

    async def async_get_island_key(self) -> str | None:
        return self._island_key

    async def load(self) -> _FakeKeystoreState:
        return _FakeKeystoreState(self._trusted)


class _FakeHeaders(dict):
    """Case-insensitive header dict matching aiohttp CIMultiDictProxy semantics."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    """Minimal aiohttp-style request mock for event endpoint tests."""

    def __init__(
        self,
        hass_obj,
        *,
        island_key_header: str | None = None,
        body: dict | None = None,
        raise_json: bool = False,
    ) -> None:
        self.app = {"hass": hass_obj}
        self._data: dict = {}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header
        self._body = body or {}
        self._raise_json = raise_json

    def get(self, key, default=None):  # noqa: D102
        return self._data.get(key, default)

    def __getitem__(self, key):  # noqa: D105
        return self._data[key]

    async def json(self):  # noqa: D102
        if self._raise_json:
            raise ValueError("invalid JSON")
        return self._body


class _FakeEventStore:
    """In-memory fake IslandEventStore for tests."""

    def __init__(self, initial_events: list | None = None) -> None:
        self._events: dict = {}
        for ev in initial_events or []:
            self._events[ev["id"]] = ev

        # Track calls for assertions
        self.upsert_calls: list[dict] = []
        self.delete_calls: list[str] = []

    async def async_list_events(self) -> list:
        return list(self._events.values())

    async def async_upsert_event(self, event: dict) -> None:
        self._events[event["id"]] = event
        self.upsert_calls.append(event)

    async def async_delete_event(self, event_id: str) -> bool:
        self.delete_calls.append(event_id)
        if event_id in self._events:
            del self._events[event_id]
            return True
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_keystore(
    hass,
    island_key: str | None = ISLAND_KEY,
    trusted_public_keys_hex: dict | None = None,
) -> None:
    """Wire a fake keystore into hass.data[DOMAIN]."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["keystore"] = _FakeKeystore(island_key, trusted_public_keys_hex)


def _install_event_store(hass, initial_events: list | None = None) -> _FakeEventStore:
    """Wire a fake event store into hass.data[DOMAIN] and return it."""
    event_store = _FakeEventStore(initial_events)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["event_store"] = event_store
    return event_store


def _make_event(event_id: str = EVENT_ID) -> dict:
    return {
        "id": event_id,
        "type": "charge",
        "inverterId": "inv-abc",
        "chargeW": 3000,
        "enabled": True,
    }


def _make_signed_write_body(
    private_key,
    key_id: str,
    event: dict,
    *,
    corrupt_sig: bool = False,
    tamper_top_level: dict | None = None,
) -> dict:
    """Build a well-formed POST/PUT body with a real ECDSA signature.

    The ``signedEventData`` is the authoritative copy; ``event`` is the
    top-level binding copy that must equal it on the happy path.
    Pass ``tamper_top_level`` to inject a mismatched top-level event.
    """
    signed_event_data = event.copy()
    signature = sign_payload(signed_event_data, private_key)
    if corrupt_sig:
        signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    top_level_event = tamper_top_level if tamper_top_level is not None else event.copy()
    return {
        "event": top_level_event,
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }


def _make_signed_delete_body(
    private_key,
    key_id: str,
    event_id: str,
    *,
    corrupt_sig: bool = False,
    signed_event_id_override: str | None = None,
) -> dict:
    """Build a DELETE body.  signed_event_id_override fakes an id mismatch."""
    actual_id = signed_event_id_override if signed_event_id_override is not None else event_id
    signed_event_data = {"event_id": actual_id}
    signature = sign_payload(signed_event_data, private_key)
    if corrupt_sig:
        signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    return {
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }


# ---------------------------------------------------------------------------
# GET tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_with_valid_island_key_returns_200_events_list(hass):
    """Valid island key → 200 with list of stored events."""
    event = _make_event()
    _install_keystore(hass)
    _install_event_store(hass, initial_events=[event])

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert "events" in data
    assert len(data["events"]) == 1
    assert data["events"][0]["id"] == EVENT_ID


@pytest.mark.asyncio
async def test_get_with_no_island_key_returns_401(hass):
    """Missing X-Island-Key → 401."""
    _install_keystore(hass)
    _install_event_store(hass)

    view = SvitgridEventsView()
    request = _FakeRequest(hass)  # No island_key_header
    resp = await view.get(request)

    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_with_wrong_island_key_returns_401(hass):
    """Wrong X-Island-Key → 401."""
    _install_keystore(hass)
    _install_event_store(hass)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header="totally-wrong-key")
    resp = await view.get(request)

    assert resp.status == 401


@pytest.mark.asyncio
async def test_get_empty_store_returns_empty_list(hass):
    """No events in store → 200 with empty list."""
    _install_keystore(hass)
    _install_event_store(hass)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    resp = await view.get(request)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["events"] == []


# ---------------------------------------------------------------------------
# POST tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_valid_key_valid_sig_stores_event_returns_200(hass):
    """Valid island key + valid admin signature → 200; event stored."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["event"]["id"] == EVENT_ID

    # Store was mutated
    assert len(event_store.upsert_calls) == 1
    assert event_store.upsert_calls[0]["id"] == EVENT_ID


@pytest.mark.asyncio
async def test_post_no_island_key_returns_401_store_not_touched(hass):
    """Missing island key → 401; store untouched."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, body=body)  # No island key
    resp = await view.post(request)

    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "unauthorized"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_post_valid_key_no_signature_field_returns_400(hass):
    """Island key present but signature field missing → 400 bad_request."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    event_store = _install_event_store(hass)

    event = _make_event()
    body = {
        "event": event,
        "signingKeyId": key_id,
        "signedEventData": event.copy(),
        # "signature" intentionally omitted
    }
    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_post_tampered_signature_returns_403_store_not_touched(hass):
    """Valid key + corrupted signature → 403 signature_invalid; store untouched."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event, corrupt_sig=True)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "signature_invalid"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_post_untrusted_signing_key_returns_403(hass):
    """Valid key + valid sig but key_id not in trusted keys → 403 signature_invalid."""
    private_key, _pub_hex = generate_keypair()
    key_id = "untrusted-key"
    _install_keystore(hass, trusted_public_keys_hex={})  # Empty trusted keys
    event_store = _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "signature_invalid"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_post_bind_mismatch_top_level_event_differs_returns_403(hass):
    """Valid key + valid sig but top-level event differs from signed copy → 403 event_mismatch."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass)

    event = _make_event()
    # Top-level event has different chargeW than what was signed
    tampered = {**event, "chargeW": 99999}
    body = _make_signed_write_body(private_key, key_id, event, tamper_top_level=tampered)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "event_mismatch"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_post_malformed_json_returns_400(hass):
    """Malformed JSON body → 400 bad_request."""
    _install_keystore(hass)
    _install_event_store(hass)

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, raise_json=True)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"


@pytest.mark.asyncio
async def test_post_stores_signed_copy_not_top_level(hass):
    """The signed copy (signedEventData) is stored, not the top-level event."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass)

    # Both top-level and signed are identical on the happy path —
    # we verify the SIGNED copy is what gets passed to async_upsert_event.
    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event)
    # signedEventData in body IS event (same content); we just confirm it's that
    # dict (not some intermediate object) that reaches the store.

    view = SvitgridEventsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 200
    stored = event_store.upsert_calls[0]
    assert stored == body["signedEventData"]


# ---------------------------------------------------------------------------
# PUT tests (same auth flow as POST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_valid_key_valid_sig_updates_event_returns_200(hass):
    """PUT with valid key + valid sig → 200; event upserted."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event = _make_event()
    event_store = _install_event_store(hass, initial_events=[event])

    updated_event = {**event, "chargeW": 5000}
    body = _make_signed_write_body(private_key, key_id, updated_event)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.put(request, event_id=EVENT_ID)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["event"]["chargeW"] == 5000
    assert len(event_store.upsert_calls) == 1


@pytest.mark.asyncio
async def test_put_no_island_key_returns_401(hass):
    """PUT without island key → 401."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, body=body)  # No island key
    resp = await view.put(request, event_id=EVENT_ID)

    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_put_bad_signature_returns_403(hass):
    """PUT with corrupted signature → 403 signature_invalid."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    event_store = _install_event_store(hass)

    event = _make_event()
    body = _make_signed_write_body(private_key, key_id, event, corrupt_sig=True)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.put(request, event_id=EVENT_ID)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "signature_invalid"
    assert event_store.upsert_calls == []


@pytest.mark.asyncio
async def test_put_bind_mismatch_returns_403(hass):
    """PUT with top-level event differing from signed copy → 403 event_mismatch."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    event_store = _install_event_store(hass)

    event = _make_event()
    tampered = {**event, "type": "malicious_override"}
    body = _make_signed_write_body(private_key, key_id, event, tamper_top_level=tampered)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.put(request, event_id=EVENT_ID)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "event_mismatch"
    assert event_store.upsert_calls == []


# ---------------------------------------------------------------------------
# DELETE tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_valid_key_valid_sig_correct_id_removes_event(hass):
    """DELETE with valid key + valid sig and matching event_id → 200; event removed."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event = _make_event()
    event_store = _install_event_store(hass, initial_events=[event])

    body = _make_signed_delete_body(private_key, key_id, EVENT_ID)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.delete(request, event_id=EVENT_ID)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["deleted"] is True

    # Event was removed from store
    assert EVENT_ID not in event_store._events


@pytest.mark.asyncio
async def test_delete_no_island_key_returns_401(hass):
    """DELETE without island key → 401."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    event_store = _install_event_store(hass, initial_events=[_make_event()])

    body = _make_signed_delete_body(private_key, key_id, EVENT_ID)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, body=body)  # No island key
    resp = await view.delete(request, event_id=EVENT_ID)

    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "unauthorized"
    assert event_store.delete_calls == []


@pytest.mark.asyncio
async def test_delete_bad_signature_returns_403_event_not_removed(hass):
    """DELETE with corrupted signature → 403; event not removed."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})
    event_store = _install_event_store(hass, initial_events=[_make_event()])

    body = _make_signed_delete_body(private_key, key_id, EVENT_ID, corrupt_sig=True)

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.delete(request, event_id=EVENT_ID)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "signature_invalid"
    assert event_store.delete_calls == []
    assert EVENT_ID in event_store._events  # Still present


@pytest.mark.asyncio
async def test_delete_id_mismatch_signed_id_differs_from_url_id_returns_403(hass):
    """DELETE where signed event_id != URL event_id → 403 event_mismatch."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    event_store = _install_event_store(hass, initial_events=[_make_event()])

    # Signed body claims to delete "ev-other-001" but URL says EVENT_ID
    body = _make_signed_delete_body(
        private_key, key_id, EVENT_ID,
        signed_event_id_override="ev-other-001",
    )

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.delete(request, event_id=EVENT_ID)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "event_mismatch"
    assert event_store.delete_calls == []
    assert EVENT_ID in event_store._events  # Not removed


@pytest.mark.asyncio
async def test_delete_nonexistent_event_returns_200_deleted_false(hass):
    """DELETE a non-existent event → 200 with deleted:False (idempotent)."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)
    _install_event_store(hass)  # Empty store

    body = _make_signed_delete_body(private_key, key_id, "ev-does-not-exist")

    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.delete(request, event_id="ev-does-not-exist")

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["deleted"] is False


@pytest.mark.asyncio
async def test_delete_missing_required_field_returns_400(hass):
    """DELETE body missing signingKeyId → 400 bad_request."""
    _install_keystore(hass)
    _install_event_store(hass)

    body = {
        # "signingKeyId" intentionally omitted
        "signedEventData": {"event_id": EVENT_ID},
        "signature": "abc",
    }
    view = SvitgridEventDetailView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.delete(request, event_id=EVENT_ID)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"
