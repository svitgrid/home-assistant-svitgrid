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
import time
import uuid
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


# ---------------------------------------------------------------------------
# set_inverter_setting on an edge island (svitgrid D-2282)
#
# The app signs the board's own event, not {command, payload}: the
# integration relays the phone's signature to the board untouched, and the
# board verifies that event. So the signed map has no `payload` key, and the
# top-level payload is bound against the event's own fields.
# ---------------------------------------------------------------------------


class _RelayingExecutor:
    """An executor that relays the phone's signature, as EdgeBoxExecutor does."""

    relays_signature = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict | None]] = []

    async def dispatch(self, command_name, payload, *, signing=None):
        self.calls.append((command_name, payload, signing))
        return {"ok": True, "readBack": payload.get("value")}


def _app_set_inverter_setting_body(
    private_key,
    key_id: str,
    *,
    setting: str = "batteryWarningSoc",
    value=25,
    inverter_id: str = INVERTER_ID,
    payload_extra: dict | None = None,
    top_level_overrides: dict | None = None,
) -> dict:
    """The body the app posts, copied from apps/mobile.

    `sendInverterSettingsCommandOnHost` (settings_host_messages.dart) builds
    the payload `{...payload, inverterId}`, signs
    `buildInverterSettingSignedEventData` (inverter_setting_event_builder.dart)
    and adds the event's `validUntil` to the payload.
    `IslandCommandSender._signAndPost` (island_command_sender.dart) adds
    `nonce` and `ts` (`withReplayGuard`) to the signed map and posts
    `{command, payload, signedEventData, signature, signingKeyId, commandId}`.
    """
    valid_until = "2099-01-01T00:00:00.000Z"
    event = {
        "command": "set_inverter_setting",
        "deviceId": "edge-ac276ed060e0",
        "inverterId": inverter_id,
        "setting": setting,
        "value": value,
        "validUntil": valid_until,
        "nonce": str(uuid.uuid4()),
        "ts": int(time.time() * 1000),
    }
    payload = {
        "inverterId": inverter_id,
        "setting": setting,
        "value": value,
        **(payload_extra or {}),
        "validUntil": valid_until,
    }
    payload.update(top_level_overrides or {})
    signature = sign_payload(event, private_key)
    return {
        "command": "set_inverter_setting",
        "payload": payload,
        "signedEventData": event,
        "signature": signature,
        "signingKeyId": key_id,
        "commandId": str(uuid.uuid4()),
    }


def _trusted_key(hass):
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    return private_key


@pytest.mark.asyncio
async def test_app_set_inverter_setting_body_reaches_a_relaying_executor(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 200, resp.body
    assert json.loads(resp.body) == {"ok": True, "result": {"ok": True, "readBack": 25}}
    assert executor.calls == [
        (
            "set_inverter_setting",
            body["payload"],
            {
                "signedEventData": body["signedEventData"],
                "signature": body["signature"],
                "signingKeyId": "admin-key-1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_app_anenji_body_keeps_its_unsigned_pack_voltage_hint(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(
        private_key,
        "admin-key-1",
        setting="bulkChargeVoltage",
        value=56.4,
        payload_extra={"nominalPackVoltage": 48},
    )

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 200, resp.body
    assert executor.calls[0][1] == body["payload"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": 26},
        {"value": "25"},
        {"setting": "batteryLowSoc"},
        {"inverterId": "inv-other"},
        {"validUntil": "2099-12-31T00:00:00.000Z"},
        {"workMode": 1},
    ],
)
@pytest.mark.asyncio
async def test_top_level_payload_that_disagrees_with_the_signed_event_is_refused(hass, overrides):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    other = _RelayingExecutor()
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["__test_entry__"] = {
        "executors_by_inverter": {INVERTER_ID: executor, "inv-other": other}
    }
    body = _app_set_inverter_setting_body(private_key, "admin-key-1", top_level_overrides=overrides)

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "command_mismatch"
    assert executor.calls == [] and other.calls == []


@pytest.mark.asyncio
async def test_top_level_payload_missing_a_signed_field_is_refused(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")
    del body["payload"]["value"]

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 403
    assert executor.calls == []


@pytest.mark.asyncio
async def test_board_event_to_an_executor_that_does_not_relay_is_still_refused(hass):
    private_key = _trusted_key(hass)
    executor = _make_executor(result={"applied": True})
    executor.relays_signature = False
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 400
    assert json.loads(resp.body)["error"] == "bad_request"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_board_event_shape_for_another_command_is_still_refused(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")
    event = {**body["signedEventData"], "command": "set_work_mode"}
    body.update(
        command="set_work_mode",
        signedEventData=event,
        signature=sign_payload(event, private_key),
    )

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 400
    assert executor.calls == []


@pytest.mark.asyncio
async def test_board_event_with_a_bad_signature_is_refused(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")
    body["signedEventData"] = {**body["signedEventData"], "value": 90}

    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 403
    assert json.loads(resp.body)["error"] == "signature_invalid"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_board_event_replayed_with_a_new_command_id_is_refused(hass):
    private_key = _trusted_key(hass)
    executor = _RelayingExecutor()
    _install_executor(hass, INVERTER_ID, executor)
    body = _app_set_inverter_setting_body(private_key, "admin-key-1")
    view = SvitgridCommandsView()

    first = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))
    replay = await view.post(
        _FakeRequest(
            hass, island_key_header=ISLAND_KEY, body={**body, "commandId": str(uuid.uuid4())}
        )
    )

    assert first.status == 200
    assert replay.status == 403
    assert json.loads(replay.body)["error"] == "replayed"
    assert len(executor.calls) == 1
