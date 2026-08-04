"""Local (LAN) recovery path for cloud sync — POST /api/svitgrid/commands.

Until now every local command was routed to a per-inverter WriteExecutor:

    inverter_id = signed_payload.get("inverterId")
    executor = executors.get(inverter_id)
    if executor is None:
        return 404 unknown_inverter

`set_cloud_ingest` is integration-level — it has no inverter — so it fell
straight through to 404. That left recovery from "cloud sync off" dependent on
a CLOUD-delivered command, which is circular in the one mode explicitly built
for the cloud being unreachable: a household could switch its uploads off and
then need the very channel it had just disabled to switch them back on.

This adds an integration-level branch BEFORE the executor lookup, so a phone on
the same WiFi can re-enable uploads with no cloud involvement at all. Auth is
unchanged and still enforced: island key + ECDSA admin signature + the
signed-vs-top-level binding check.

The reload is deliberately NOT awaited — see the hanging-reload test.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridCommandsView
from custom_components.svitgrid.signing import generate_keypair, sign_payload

ISLAND_KEY = "test-island-key-for-commands-endpoint"


class _FakeKeystoreState:
    def __init__(self, trusted_public_keys_hex: dict) -> None:
        self.trusted_public_keys_hex = trusted_public_keys_hex


class _FakeKeystore:
    def __init__(self, island_key, trusted_public_keys_hex=None) -> None:
        self._island_key = island_key
        self._trusted = trusted_public_keys_hex or {}

    async def async_get_island_keys(self) -> list[str]:
        return [self._island_key] if self._island_key else []

    async def load(self) -> _FakeKeystoreState:
        return _FakeKeystoreState(self._trusted)


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)

    def __setitem__(self, key, value):  # noqa: D102
        super().__setitem__(key.lower(), value)


class _FakeRequest:
    def __init__(self, hass_obj, *, island_key_header=None, body=None) -> None:
        self.app = {"hass": hass_obj}
        self.headers = _FakeHeaders()
        if island_key_header is not None:
            self.headers["x-island-key"] = island_key_header
        self._body = body or {}

    async def json(self):  # noqa: D102
        return self._body


def _install_keystore(hass, island_key=ISLAND_KEY, trusted_public_keys_hex=None) -> None:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["keystore"] = _FakeKeystore(island_key, trusted_public_keys_hex)


def _make_signed_body(private_key, key_id, command, payload, *, corrupt_sig=False) -> dict:
    signed_event_data = {"command": command, "payload": payload}
    signature = sign_payload(signed_event_data, private_key)
    if corrupt_sig:
        signature = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    return {
        "command": command,
        "payload": payload,
        "signingKeyId": key_id,
        "signedEventData": signed_event_data,
        "signature": signature,
    }


def _install_entry(hass, monkeypatch, entry_data=None):
    """Give hass a single fake Svitgrid ConfigEntry + capture reload scheduling."""
    entry = MagicMock()
    entry.data = entry_data if entry_data is not None else {"cloud_ingest_enabled": False}
    entry.entry_id = "entry-1"

    fake_ce = MagicMock()
    fake_ce.async_entries = MagicMock(return_value=[entry])
    fake_ce.async_update_entry = MagicMock()
    fake_ce.async_reload = AsyncMock()
    monkeypatch.setattr(hass, "config_entries", fake_ce)

    scheduled: list = []

    def _capture(coro, *a, **k):
        # Close the coroutine so the loop doesn't warn about it never being awaited.
        if asyncio.iscoroutine(coro):
            coro.close()
        scheduled.append(coro)
        return MagicMock()

    monkeypatch.setattr(hass, "async_create_task", MagicMock(side_effect=_capture))
    return entry, fake_ce, scheduled


def _body_json(response) -> dict:
    return json.loads(response.body.decode() if isinstance(response.body, bytes) else response.text)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_true_enables_upload_without_cloud(hass, monkeypatch):
    """The whole point: a phone on the LAN re-enables uploads with no cloud hop."""
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    entry, fake_ce, scheduled = _install_entry(
        hass, monkeypatch, {"cloud_ingest_enabled": False, "island_key": "K"}
    )

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": True})
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 200, _body_json(resp)
    fake_ce.async_update_entry.assert_called_once()
    updated = fake_ce.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is True
    # Island access must survive — this command is orthogonal to island mode.
    assert updated.get("island_key") == "K"
    assert len(scheduled) == 1, "entry reload must be scheduled so the sender starts"


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_false_disables_upload(hass, monkeypatch):
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    _entry, fake_ce, _scheduled = _install_entry(hass, monkeypatch, {"cloud_ingest_enabled": True})

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": False})
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 200
    assert fake_ce.async_update_entry.call_args.kwargs["data"]["cloud_ingest_enabled"] is False


@pytest.mark.asyncio
async def test_handler_does_not_await_the_reload(hass, monkeypatch):
    """The reload tears down this integration. If the handler awaited it, the
    request would block on its own teardown; a caller would see a hung request
    and could not tell whether the change applied."""
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    entry = MagicMock()
    entry.data = {"cloud_ingest_enabled": False}
    entry.entry_id = "entry-1"

    never_completes = asyncio.Event()

    fake_ce = MagicMock()
    fake_ce.async_entries = MagicMock(return_value=[entry])
    fake_ce.async_update_entry = MagicMock()

    async def _hanging_reload(_entry_id):
        await never_completes.wait()

    fake_ce.async_reload = AsyncMock(side_effect=_hanging_reload)
    monkeypatch.setattr(hass, "config_entries", fake_ce)

    real_tasks: list[asyncio.Task] = []
    monkeypatch.setattr(
        hass,
        "async_create_task",
        MagicMock(side_effect=lambda coro, *a, **k: real_tasks.append(asyncio.ensure_future(coro))),
    )

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": True})

    resp = await asyncio.wait_for(
        view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)),
        timeout=2.0,
    )
    assert resp.status == 200

    for t in real_tasks:
        t.cancel()


# ---------------------------------------------------------------------------
# Rejections — nothing may half-apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_rejects_non_bool_enabled(hass, monkeypatch):
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    _entry, fake_ce, scheduled = _install_entry(hass, monkeypatch)

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": "yes"})
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 400
    fake_ce.async_update_entry.assert_not_called()
    assert scheduled == []


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_requires_island_key(hass, monkeypatch):
    """Auth is unchanged: without the island key nothing is read or applied."""
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    _entry, fake_ce, _scheduled = _install_entry(hass, monkeypatch)

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": True})
    resp = await view.post(_FakeRequest(hass, island_key_header="wrong-key", body=body))

    assert resp.status == 401
    fake_ce.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_requires_valid_admin_signature(hass, monkeypatch):
    """A valid island key alone must not be enough — the LAN is not a trust
    boundary on its own."""
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    _entry, fake_ce, _scheduled = _install_entry(hass, monkeypatch)

    view = SvitgridCommandsView()
    body = _make_signed_body(
        private_key, "admin-key-1", "set_cloud_ingest", {"enabled": True}, corrupt_sig=True
    )
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 403
    fake_ce.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_local_set_cloud_ingest_with_no_config_entry_returns_404(hass, monkeypatch):
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})

    fake_ce = MagicMock()
    fake_ce.async_entries = MagicMock(return_value=[])
    fake_ce.async_update_entry = MagicMock()
    monkeypatch.setattr(hass, "config_entries", fake_ce)

    view = SvitgridCommandsView()
    body = _make_signed_body(private_key, "admin-key-1", "set_cloud_ingest", {"enabled": True})
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 404
    fake_ce.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Regression: the new branch must not swallow executor commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inverter_command_still_routes_to_executor(hass, monkeypatch):
    """Integration-level branching must not intercept per-inverter commands."""
    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"admin-key-1": pub_hex})
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"chargeW": 3000})
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["__test_entry__"] = {"executors_by_inverter": {"inv-1": executor}}

    view = SvitgridCommandsView()
    body = _make_signed_body(
        private_key,
        "admin-key-1",
        "set_battery_charge",
        {"inverterId": "inv-1", "chargeW": 3000},
    )
    resp = await view.post(_FakeRequest(hass, island_key_header=ISLAND_KEY, body=body))

    assert resp.status == 200
    executor.dispatch.assert_awaited_once()
