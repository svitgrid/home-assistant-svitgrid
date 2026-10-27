"""True island over the LAN: cloud copy, leave island, revoke a key, status.

In true island the integration polls nothing from the cloud, so the jobs the
command poller carried come from the app directly. Each write carries the
phone's island key and a signature by a trusted admin key over `signedData`,
which names its `action` and carries a `nonce` and a `ts` in epoch
milliseconds.

The paths, fields, status codes and error codes are the contract the Svitgrid
app is built against (true-island-contract.md, 2026-10-15). Do not change one
to make a test pass.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.island_lan import (
    NONCE_MEMORY,
    SvitgridIslandCloudCopyView,
    SvitgridIslandLeaveView,
    SvitgridIslandStatusView,
    SvitgridKeysRevokeView,
)
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload

ISLAND_KEY = "p" * 43
OTHER_ISLAND_KEY = "q" * 43
NOW_MS = 1_760_000_000_000


class _Req:
    def __init__(self, hass, body=None, *, island_key: str | None = ISLAND_KEY, raw=None):
        self.app = {"hass": hass}
        self.headers = {"X-Island-Key": island_key} if island_key else {}
        self.query: dict = {}
        self._body = body
        self._raw = raw

    def get(self, key, default=None):
        return default

    async def json(self):
        if self._raw is not None:
            return json.loads(self._raw)
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Admin:
    """A phone's signing key."""

    def __init__(self) -> None:
        self.priv, self.pub = generate_keypair()
        self.key_id = compute_key_id(self.pub)

    def request(self, action: str, **fields) -> dict:
        signed = {"action": action, "nonce": uuid.uuid4().hex, "ts": NOW_MS, **fields}
        return self.signed(signed)

    def signed(self, signed: dict) -> dict:
        return {
            "signingKeyId": self.key_id,
            "signedData": signed,
            "signature": sign_payload(signed, self.priv),
        }


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


@pytest.fixture
def reload_mock(hass):
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as m:
        yield m


async def _keystore(hass, *admins: _Admin) -> SvitgridKeystore:
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="cloud-api-key",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-1",
        trusted_key_ids=[a.key_id for a in admins],
        trusted_public_keys_hex={a.key_id: a.pub for a in admins},
    )
    await ks.async_add_island_key("phone-1", ISLAND_KEY, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = ks
    return ks


def _entry(hass, *, local_only=False, cloud_ingest=False) -> MockConfigEntry:
    data = (
        {"local_only": True, "cloud_ingest_enabled": False}
        if local_only
        else {"api_key": "k", "edge_device_id": "ed-1", "cloud_ingest_enabled": cloud_ingest}
    )
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=data)
    entry.add_to_hass(hass)
    return entry


# ── cloud copy ──────────────────────────────────────────────────────────────


async def test_cloud_copy_on_flips_the_entry_and_reloads_it(hass: HomeAssistant, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass)

    resp = await SvitgridIslandCloudCopyView().post(
        _Req(hass, admin.request("set_cloud_copy", enabled=True))
    )
    await hass.async_block_till_done()

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "cloudCopy": True}
    assert entry.data["cloud_ingest_enabled"] is True
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_cloud_copy_off_flips_the_entry_back(hass: HomeAssistant, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass, cloud_ingest=True)

    resp = await SvitgridIslandCloudCopyView().post(
        _Req(hass, admin.request("set_cloud_copy", enabled=False))
    )
    await hass.async_block_till_done()

    assert _body(resp) == {"ok": True, "cloudCopy": False}
    assert entry.data["cloud_ingest_enabled"] is False
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_cloud_copy_unchanged_does_not_reload(hass: HomeAssistant, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass, cloud_ingest=False)

    resp = await SvitgridIslandCloudCopyView().post(
        _Req(hass, admin.request("set_cloud_copy", enabled=False))
    )
    await hass.async_block_till_done()

    assert resp.status == 200
    reload_mock.assert_not_awaited()


async def test_cloud_copy_on_for_a_local_only_entry_is_no_cloud_identity(
    hass: HomeAssistant, reload_mock
):
    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass, local_only=True)

    resp = await SvitgridIslandCloudCopyView().post(
        _Req(hass, admin.request("set_cloud_copy", enabled=True))
    )

    assert resp.status == 409
    assert _body(resp) == {"error": "no_cloud_identity"}
    assert entry.data["cloud_ingest_enabled"] is False
    reload_mock.assert_not_awaited()


async def test_cloud_copy_needs_a_boolean(hass: HomeAssistant, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridIslandCloudCopyView().post(
        _Req(hass, admin.request("set_cloud_copy", enabled="yes"))
    )
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


# ── leave island ────────────────────────────────────────────────────────────


async def test_leave_turns_the_cloud_copy_on_and_keeps_the_island_keys(
    hass: HomeAssistant, reload_mock
):
    """Same effect as the cloud's `disable_island`."""
    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)

    resp = await SvitgridIslandLeaveView().post(_Req(hass, admin.request("leave_island")))
    await hass.async_block_till_done()

    assert resp.status == 200
    assert _body(resp) == {"ok": True}
    assert entry.data["cloud_ingest_enabled"] is True
    reload_mock.assert_awaited_once_with(entry.entry_id)
    assert "phone-1" in (await ks.load()).island_keys


async def test_leave_for_a_local_only_entry_is_no_cloud_identity(hass: HomeAssistant, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass, local_only=True)
    resp = await SvitgridIslandLeaveView().post(_Req(hass, admin.request("leave_island")))
    assert resp.status == 409
    assert _body(resp) == {"error": "no_cloud_identity"}
    reload_mock.assert_not_awaited()


# ── revoke ──────────────────────────────────────────────────────────────────


async def test_revoke_untrusts_the_key_and_removes_its_phone(hass: HomeAssistant):
    admin, other = _Admin(), _Admin()
    ks = await _keystore(hass, admin, other)
    await ks.async_add_island_key("phone-2", OTHER_ISLAND_KEY, label="iPhone")
    await ks.async_link_signing_key(other.key_id, "phone-2")
    _entry(hass)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=other.key_id))
    )

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True}
    state = await ks.load()
    assert other.key_id not in state.trusted_public_keys_hex
    assert admin.key_id in state.trusted_public_keys_hex
    assert "phone-2" not in state.island_keys
    assert "phone-1" in state.island_keys
    assert other.key_id not in state.key_devices


async def test_revoke_a_key_with_no_phone_keeps_every_island_key(hass: HomeAssistant):
    """A key trusted through the cloud has no phone row to take with it."""
    admin, cloud_key = _Admin(), _Admin()
    ks = await _keystore(hass, admin, cloud_key)
    _entry(hass)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=cloud_key.key_id))
    )

    assert resp.status == 200
    state = await ks.load()
    assert cloud_key.key_id not in state.trusted_public_keys_hex
    assert set(state.island_keys) == {"phone-1"}


async def test_revoke_a_view_only_phone_removes_its_island_key(hass: HomeAssistant):
    """A view-only phone's key is never trusted, but it names the phone."""
    admin, viewer = _Admin(), _Admin()
    ks = await _keystore(hass, admin)
    await ks.async_add_island_key("phone-2", OTHER_ISLAND_KEY)
    await ks.async_link_signing_key(viewer.key_id, "phone-2")
    _entry(hass)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=viewer.key_id))
    )

    assert resp.status == 200
    state = await ks.load()
    assert "phone-2" not in state.island_keys
    assert set(state.trusted_public_keys_hex) == {admin.key_id}


async def test_revoke_the_last_admin_key_is_refused(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)
    await ks.async_link_signing_key(admin.key_id, "phone-1")
    _entry(hass)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=admin.key_id))
    )

    assert resp.status == 409
    assert _body(resp) == {"error": "last_admin_key"}
    state = await ks.load()
    assert admin.key_id in state.trusted_public_keys_hex
    assert "phone-1" in state.island_keys


async def test_revoke_an_unknown_key_is_ok(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId="nobody"))
    )
    assert resp.status == 200


async def test_revoke_needs_a_key_id(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridKeysRevokeView().post(_Req(hass, admin.request("revoke_key")))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


# ── auth, shared by the three writes ────────────────────────────────────────


def _writes():
    return [
        pytest.param(SvitgridIslandCloudCopyView, "set_cloud_copy", {"enabled": True}, id="copy"),
        pytest.param(SvitgridIslandLeaveView, "leave_island", {}, id="leave"),
        pytest.param(SvitgridKeysRevokeView, "revoke_key", {"keyId": "x"}, id="revoke"),
    ]


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_no_island_key_is_unauthorized(hass, reload_mock, view, action, fields):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await view().post(_Req(hass, admin.request(action, **fields), island_key=None))
    assert resp.status == 401
    assert _body(resp) == {"error": "unauthorized"}
    resp = await view().post(
        _Req(hass, admin.request(action, **fields), island_key=OTHER_ISLAND_KEY)
    )
    assert resp.status == 401


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_an_untrusted_signer_is_signature_invalid(hass, reload_mock, view, action, fields):
    """A view-only phone holds an island key but no trusted signing key."""
    admin, viewer = _Admin(), _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await view().post(_Req(hass, viewer.request(action, **fields)))
    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}
    reload_mock.assert_not_awaited()


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_a_tampered_body_is_signature_invalid(hass, reload_mock, view, action, fields):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    body = admin.request(action, **fields)
    body["signedData"] = {**body["signedData"], "nonce": "changed"}
    resp = await view().post(_Req(hass, body))
    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_a_request_signed_for_another_action_is_refused(
    hass, reload_mock, view, action, fields
):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    other = "leave_island" if action != "leave_island" else "set_cloud_copy"
    resp = await view().post(_Req(hass, admin.request(other, **fields)))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"
    reload_mock.assert_not_awaited()


@pytest.mark.parametrize("skew_ms", [5 * 60 * 1000 + 1, -(5 * 60 * 1000 + 1)])
@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_a_ts_more_than_five_minutes_away_is_stale(
    hass, reload_mock, view, action, fields, skew_ms
):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    signed = {"action": action, "nonce": uuid.uuid4().hex, "ts": NOW_MS + skew_ms, **fields}
    resp = await view().post(_Req(hass, admin.signed(signed)))
    assert resp.status == 403
    assert _body(resp) == {"error": "stale_request"}


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_a_ts_within_five_minutes_passes(hass, reload_mock, view, action, fields):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    signed = {"action": action, "nonce": uuid.uuid4().hex, "ts": NOW_MS - 299_000, **fields}
    resp = await view().post(_Req(hass, admin.signed(signed)))
    assert resp.status == 200, _body(resp)


@pytest.mark.parametrize(("view", "action", "fields"), _writes())
async def test_a_reused_nonce_is_replayed(hass, reload_mock, view, action, fields):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    body = admin.request(action, **fields)
    assert (await view().post(_Req(hass, body))).status == 200
    resp = await view().post(_Req(hass, body))
    assert resp.status == 403
    assert _body(resp) == {"error": "replayed"}


async def test_a_nonce_is_shared_across_the_endpoints(hass, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    nonce = "same-nonce"
    first = admin.signed({"action": "leave_island", "nonce": nonce, "ts": NOW_MS})
    assert (await SvitgridIslandLeaveView().post(_Req(hass, first))).status == 200
    second = admin.signed(
        {"action": "set_cloud_copy", "enabled": False, "nonce": nonce, "ts": NOW_MS}
    )
    resp = await SvitgridIslandCloudCopyView().post(_Req(hass, second))
    assert _body(resp) == {"error": "replayed"}


async def test_only_the_last_500_nonces_are_kept(hass, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    first = admin.signed({"action": "leave_island", "nonce": "n-0", "ts": NOW_MS})
    assert (await SvitgridIslandLeaveView().post(_Req(hass, first))).status == 200
    for i in range(1, NONCE_MEMORY + 1):
        body = admin.signed({"action": "leave_island", "nonce": f"n-{i}", "ts": NOW_MS})
        assert (await SvitgridIslandLeaveView().post(_Req(hass, body))).status == 200
    assert NONCE_MEMORY == 500
    # n-0 has been forgotten; n-500 has not.
    assert (await SvitgridIslandLeaveView().post(_Req(hass, first))).status == 200
    last = admin.signed({"action": "leave_island", "nonce": f"n-{NONCE_MEMORY}", "ts": NOW_MS})
    assert _body(await SvitgridIslandLeaveView().post(_Req(hass, last))) == {"error": "replayed"}


async def test_an_unsigned_request_spends_no_nonce(hass, reload_mock):
    """A stranger on the LAN with a stolen island key cannot burn the app's
    nonces with requests it could not sign."""
    admin, stranger = _Admin(), _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    forged = stranger.signed({"action": "leave_island", "nonce": "n-1", "ts": NOW_MS})
    assert (await SvitgridIslandLeaveView().post(_Req(hass, forged))).status == 403
    real = admin.signed({"action": "leave_island", "nonce": "n-1", "ts": NOW_MS})
    assert (await SvitgridIslandLeaveView().post(_Req(hass, real))).status == 200


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"signedData": {}, "signature": "x"}, id="no key id"),
        pytest.param({"signingKeyId": "k", "signature": "x"}, id="no signedData"),
        pytest.param({"signingKeyId": "k", "signedData": []}, id="signedData not an object"),
        pytest.param([], id="not an object"),
    ],
)
async def test_a_malformed_body_is_bad_request(hass, reload_mock, body):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridIslandLeaveView().post(_Req(hass, body))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


@pytest.mark.parametrize(
    "signed",
    [
        pytest.param({"action": "leave_island", "ts": NOW_MS}, id="no nonce"),
        pytest.param({"action": "leave_island", "nonce": "n"}, id="no ts"),
        pytest.param({"action": "leave_island", "nonce": "n", "ts": "now"}, id="ts not a number"),
    ],
)
async def test_signed_data_without_nonce_or_ts_is_bad_request(hass, reload_mock, signed):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridIslandLeaveView().post(_Req(hass, admin.signed(signed)))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


async def test_a_body_that_is_not_json_is_bad_request(hass, reload_mock):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridIslandLeaveView().post(_Req(hass, raw=b"not json"))
    assert resp.status == 400


# ── status ──────────────────────────────────────────────────────────────────


async def test_status_of_a_true_island(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass, cloud_ingest=False)
    resp = await SvitgridIslandStatusView().get(_Req(hass))
    assert resp.status == 200
    # The installed version, read from the manifest so a release bump keeps it.
    manifest = Path(__file__).parents[1] / "custom_components" / "svitgrid" / "manifest.json"
    version = json.loads(manifest.read_text())["version"]
    assert _body(resp) == {
        "cloudCopy": False,
        "localOnly": False,
        "trueIsland": True,
        "version": version,
        "capabilities": [
            "cloud_copy_lan",
            "leave_lan",
            "revoke_lan",
            "pair_extra_phone",
            "rules",
        ],
    }


async def test_status_of_an_island_with_cloud_copy(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass, cloud_ingest=True)
    body = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert body["cloudCopy"] is True
    assert body["trueIsland"] is False
    assert body["localOnly"] is False


async def test_status_of_a_local_only_station(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass, local_only=True)
    body = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert body["cloudCopy"] is False
    assert body["localOnly"] is True
    assert body["trueIsland"] is True


async def test_status_needs_the_island_key(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass)
    resp = await SvitgridIslandStatusView().get(_Req(hass, island_key=None))
    assert resp.status == 401
    resp = await SvitgridIslandStatusView().get(_Req(hass, island_key=OTHER_ISLAND_KEY))
    assert resp.status == 401


async def test_the_views_are_registered_with_the_others(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert {
        "/api/svitgrid/island/cloud-copy",
        "/api/svitgrid/island/leave",
        "/api/svitgrid/keys/revoke",
        "/api/svitgrid/island/status",
    } <= urls
