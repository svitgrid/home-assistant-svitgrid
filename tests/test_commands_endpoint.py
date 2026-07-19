"""TDD tests for POST /api/svitgrid/commands (Task 5 — island mode SP1).

Written BEFORE implementation (RED phase). Tests cover:
- valid key + valid signature + known command → executor.dispatch called once → 200
- valid key + BAD signature → 403, executor NOT called
- valid key + unknown command (executor raises NotImplementedError) → 422
- valid key + executor raises RuntimeError → 502 with detail
- no/invalid key → 401, nothing verified or dispatched
- unknown inverterId → 404
- same commandId twice → second returns deduped:true, executor called once
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridCommandsView
from custom_components.svitgrid.signing import generate_keypair, sign_payload

ISLAND_KEY = "test-island-key-for-commands-endpoint"
INVERTER_ID = "inv-test-123"


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

    async def async_get_island_keys(self) -> list[str]:
        return [self._island_key] if self._island_key else []

    async def load(self) -> _FakeKeystoreState:
        return _FakeKeystoreState(self._trusted)


class _FakeHeaders(dict):
    """Case-insensitive header dict matching aiohttp CIMultiDictProxy semantics."""

    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    """Minimal aiohttp-style request mock for command endpoint tests."""

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


def _install_executor(hass, inverter_id: str, executor) -> None:
    """Wire an executor into hass.data[DOMAIN] under a fake entry key."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["__test_entry__"] = {"executors_by_inverter": {inverter_id: executor}}


def _make_signed_body(
    private_key,
    key_id: str,
    command: str,
    payload: dict,
    *,
    command_id: str | None = None,
    corrupt_sig: bool = False,
) -> dict:
    """Build a well-formed request body with a real ECDSA signature."""
    signed_event_data = {"command": command, "payload": payload}
    signature = sign_payload(signed_event_data, private_key)
    if corrupt_sig:
        # Flip the last char so signature is syntactically valid b64 but wrong.
        signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    body: dict = {
        "command": command,
        "payload": payload,
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }
    if command_id is not None:
        body["commandId"] = command_id
    return body


def _make_executor(result: dict | None = None, side_effect=None) -> AsyncMock:
    executor = MagicMock()
    if side_effect is not None:
        executor.dispatch = AsyncMock(side_effect=side_effect)
    else:
        executor.dispatch = AsyncMock(return_value=result or {"applied": True})
    return executor


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_key_valid_sig_known_cmd_returns_200_result(hass):
    """Valid island key + valid admin signature + known command → 200 {ok, result}."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"chargeW": 3000})
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 3000},
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    assert data["result"] == {"chargeW": 3000}
    executor.dispatch.assert_awaited_once_with(
        "set_battery_charge", {"inverterId": INVERTER_ID, "chargeW": 3000}
    )


@pytest.mark.asyncio
async def test_valid_key_bad_sig_returns_403_executor_not_called(hass):
    """Valid island key + BAD admin signature → 403, executor NOT called."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor()
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 3000},
        corrupt_sig=True,
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "signature_invalid"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_valid_key_unsupported_cmd_returns_422(hass):
    """Valid key + valid sig + executor raises NotImplementedError → 422."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(side_effect=NotImplementedError("not supported"))
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "unsupported_cmd",
        {"inverterId": INVERTER_ID},
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 422
    data = json.loads(resp.body)
    assert data["error"] == "unsupported"


@pytest.mark.asyncio
async def test_valid_key_executor_error_returns_502_with_detail(hass):
    """Valid key + valid sig + executor raises RuntimeError → 502 with detail."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(side_effect=RuntimeError("verify_failed"))
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 1000},
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 502
    data = json.loads(resp.body)
    assert data["error"] == "executor_error"
    assert "verify_failed" in data["detail"]


@pytest.mark.asyncio
async def test_no_island_key_returns_401_nothing_dispatched(hass):
    """No X-Island-Key header → 401; keystore never loaded, executor never called."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor()
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 1000},
    )
    view = SvitgridCommandsView()
    # No island_key_header
    request = _FakeRequest(hass, body=body)
    resp = await view.post(request)

    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "unauthorized"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_island_key_returns_401(hass):
    """Wrong X-Island-Key value → 401."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor()
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 1000},
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header="wrong-key", body=body)
    resp = await view.post(request)

    assert resp.status == 401
    data = json.loads(resp.body)
    assert data["error"] == "unauthorized"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_inverter_id_returns_404(hass):
    """Valid key + valid sig + unknown inverterId → 404."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor()
    _install_executor(hass, "other-inverter-id", executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        # inverterId does NOT match installed executor
        {"inverterId": "totally-unknown-inverter"},
    )
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 404
    data = json.loads(resp.body)
    assert data["error"] == "unknown_inverter"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_same_command_id_twice_deduped_executor_called_once(hass):
    """Same commandId submitted twice → second returns deduped:true; executor called once."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    body = _make_signed_body(
        private_key,
        key_id,
        "set_battery_charge",
        {"inverterId": INVERTER_ID, "chargeW": 2000},
        command_id="cmd-uuid-abc123",
    )
    view = SvitgridCommandsView()

    # First request — should dispatch normally.
    req1 = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp1 = await view.post(req1)
    assert resp1.status == 200
    data1 = json.loads(resp1.body)
    assert data1["ok"] is True
    assert "deduped" not in data1

    # Second request with same commandId — should be deduped.
    req2 = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp2 = await view.post(req2)
    assert resp2.status == 200
    data2 = json.loads(resp2.body)
    assert data2["ok"] is True
    assert data2.get("deduped") is True

    # Executor dispatched exactly once.
    executor.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_json_body_returns_400(hass):
    """Malformed JSON body → 400 bad_request."""
    _install_keystore(hass)
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, raise_json=True)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"


@pytest.mark.asyncio
async def test_missing_required_field_returns_400(hass):
    """Body missing required fields → 400 bad_request."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    _install_keystore(hass, trusted_public_keys_hex={key_id: pub_hex})

    # Missing 'command' field
    body = {
        "payload": {"inverterId": INVERTER_ID},
        "signingKeyId": key_id,
        "signedEventData": {},
        "signature": "abc",
    }
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"


# ---------------------------------------------------------------------------
# CRITICAL: signature↔command binding gap tests (RED phase)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_mismatch_top_level_command_differs_from_signed(hass):
    """Valid key + valid sig over {command:A,payload:P} but top-level command=B → 403 command_mismatch, executor NOT called."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    # Build a body where signed command is "set_battery_charge" but top-level is different
    signed_payload = {"inverterId": INVERTER_ID, "chargeW": 3000}
    from custom_components.svitgrid.signing import sign_payload

    signed_event_data = {"command": "set_battery_charge", "payload": signed_payload}
    signature = sign_payload(signed_event_data, private_key)

    body = {
        "command": "set_work_mode",  # Different from what was signed!
        "payload": signed_payload,
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "command_mismatch"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_command_mismatch_top_level_payload_differs_from_signed(hass):
    """Valid key + valid sig but top-level payload differs from signed payload → 403, executor NOT called."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    from custom_components.svitgrid.signing import sign_payload

    signed_payload = {"inverterId": INVERTER_ID, "chargeW": 3000}
    signed_event_data = {"command": "set_battery_charge", "payload": signed_payload}
    signature = sign_payload(signed_event_data, private_key)

    # Top-level payload has higher chargeW than what was signed
    body = {
        "command": "set_battery_charge",
        "payload": {"inverterId": INVERTER_ID, "chargeW": 99999},  # Different!
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 403
    data = json.loads(resp.body)
    assert data["error"] == "command_mismatch"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_signed_event_data_missing_command_field_returns_400(hass):
    """Valid key + sig where signedEventData is missing 'command' → 400 bad_request."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    from custom_components.svitgrid.signing import sign_payload

    # signedEventData is missing 'command'
    signed_event_data = {"payload": {"inverterId": INVERTER_ID}}
    signature = sign_payload(signed_event_data, private_key)

    body = {
        "command": "set_battery_charge",
        "payload": {"inverterId": INVERTER_ID},
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_signed_event_data_non_dict_payload_returns_400(hass):
    """Valid key + sig where signedEventData has non-dict payload → 400 bad_request."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    from custom_components.svitgrid.signing import sign_payload

    # signedEventData has a list as payload (not a dict)
    signed_event_data = {"command": "set_battery_charge", "payload": ["this", "is", "a", "list"]}
    signature = sign_payload(signed_event_data, private_key)

    body = {
        "command": "set_battery_charge",
        "payload": {"inverterId": INVERTER_ID},
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 400
    data = json.loads(resp.body)
    assert data["error"] == "bad_request"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_top_level_matches_signed_dispatches_correctly(hass):
    """Happy path: top-level command+payload match what was signed → 200, executor called once."""
    private_key, pub_hex = generate_keypair()
    key_id = "admin-key-1"
    trusted = {key_id: pub_hex}
    _install_keystore(hass, trusted_public_keys_hex=trusted)

    executor = _make_executor(result={"applied": True})
    _install_executor(hass, INVERTER_ID, executor)

    cmd_payload = {"inverterId": INVERTER_ID, "chargeW": 5000}
    body = _make_signed_body(private_key, key_id, "set_battery_charge", cmd_payload)
    view = SvitgridCommandsView()
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    resp = await view.post(request)

    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["ok"] is True
    executor.dispatch.assert_awaited_once_with("set_battery_charge", cmd_payload)
