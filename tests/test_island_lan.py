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
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

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
            "settings_lan",
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


# ── edge boxes: adopt, forget, status (edge island plan, Task 15) ───────────


_INV_DEYE = {
    "inverterId": "inv-1",
    "modelId": "deye_sg04lp3",
    "protocolId": "solarman_v5",
    "transport": "solarman",
    "ipAddress": "192.168.1.50",
    "loggerSerial": "2712345678",
    "slaveId": 1,
    "phases": 1,
    "hasBattery": True,
    "pvStrings": 2,
    "batteryCapacityWh": 10240,
    "batterySignInverted": False,
    "powerScaleOverrides": {"pvPower": 10},
}


def _inv(inverter_id: str, **fields) -> dict:
    return {**_INV_DEYE, "inverterId": inverter_id, **fields}


class _EdgeStore:
    """The parts of the reading store the edge status reads."""

    def __init__(self, box_states=None, snapshots=None) -> None:
        self.box_states = dict(box_states or {})
        self.snapshots = dict(snapshots or {})

    async def get_edge_box_state(self, device_id):
        return self.box_states.get(device_id)

    async def get_settings_snapshot(self, inverter_id):
        return self.snapshots.get(inverter_id)


def _entry_state(hass, entry, *, store=None, queue=None) -> dict:
    state = {"store": store or _EdgeStore()}
    if queue is not None:
        state["edge_queue"] = queue
    hass.data[DOMAIN][entry.entry_id] = state
    return state


async def _adopt(hass, admin, device_id, *inverters, **fields):
    from custom_components.svitgrid.island_lan import SvitgridEdgeAdoptView

    return await SvitgridEdgeAdoptView().post(
        _Req(
            hass,
            admin.request(
                "edge_adopt", deviceId=device_id, inverters=[dict(i) for i in inverters], **fields
            ),
        )
    )


async def _forget(hass, admin, device_id):
    from custom_components.svitgrid.island_lan import SvitgridEdgeForgetView

    return await SvitgridEdgeForgetView().post(
        _Req(hass, admin.request("edge_forget", deviceId=device_id))
    )


async def _edge_status(hass, **req):
    from custom_components.svitgrid.island_lan import SvitgridEdgeStatusView

    return await SvitgridEdgeStatusView().get(_Req(hass, **req))


async def test_edge_adopt_creates_box_and_issues_code(hass: HomeAssistant):
    from custom_components.svitgrid.const import PAIRING_CODE_ALPHABET, PAIRING_CODE_LENGTH

    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)

    resp = await _adopt(hass, admin, "EC-1", _INV_DEYE)

    assert resp.status == 200, _body(resp)
    body = _body(resp)
    assert body["ok"] is True
    code = body["pairCode"]
    assert len(code) == PAIRING_CODE_LENGTH
    assert set(code) <= set(PAIRING_CODE_ALPHABET)
    assert body["expiresAt"].endswith("Z")
    state = await ks.load()
    box = state.edge_boxes["EC-1"]
    assert box.api_key is None
    assert box.inverter_ids == ["inv-1"]
    assert box.inverters == {"inv-1": _INV_DEYE}
    # Only the hash is kept, and the code is the box's live one.
    assert box.pair_code_hash != code
    assert state.edge_box_for_pair_code(code) == (box, "ok")


async def test_edge_adopt_twice_reissues_code_and_drops_binding(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)
    first = _body(await _adopt(hass, admin, "EC-1", _INV_DEYE))["pairCode"]
    await ks.async_bind_edge_box("EC-1", "box-api-key")

    resp = await _adopt(hass, admin, "EC-1", _inv("inv-2"))

    assert resp.status == 200, _body(resp)
    second = _body(resp)["pairCode"]
    state = await ks.load()
    box = state.edge_boxes["EC-1"]
    assert box.api_key is None
    assert state.edge_box_for_api_key("box-api-key") is None
    assert box.inverter_ids == ["inv-2"]
    assert list(box.inverters) == ["inv-2"]
    assert state.edge_box_for_pair_code(second) == (box, "ok")
    if first != second:
        assert state.edge_box_for_pair_code(first) == (None, "invalid")


async def test_edge_adopt_three_boxes(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)

    codes = {}
    for n in (1, 2, 3):
        resp = await _adopt(hass, admin, f"EC-{n}", _inv(f"inv-{n}"))
        assert resp.status == 200, _body(resp)
        codes[f"EC-{n}"] = _body(resp)["pairCode"]

    state = await ks.load()
    assert sorted(state.edge_boxes) == ["EC-1", "EC-2", "EC-3"]
    for device_id, code in codes.items():
        box, verdict = state.edge_box_for_pair_code(code)
        assert verdict == "ok"
        assert box.device_id == device_id


async def test_edge_adopt_inverter_held_by_other_box_is_409(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)
    assert (await _adopt(hass, admin, "EC-1", _INV_DEYE)).status == 200
    before = await ks.load()
    code_hash = before.edge_boxes["EC-1"].pair_code_hash

    resp = await _adopt(hass, admin, "EC-2", _inv("inv-9"), _inv("inv-1"))

    assert resp.status == 409
    assert _body(resp) == {"error": "inverter_held_by_other_box"}
    state = await ks.load()
    assert list(state.edge_boxes) == ["EC-1"]
    assert state.edge_boxes["EC-1"].pair_code_hash == code_hash


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"deviceId": "", "inverters": [_INV_DEYE]}, id="empty-device-id"),
        pytest.param({"deviceId": 7, "inverters": [_INV_DEYE]}, id="device-id-not-string"),
        pytest.param({"deviceId": "EC-1"}, id="no-inverters"),
        pytest.param({"deviceId": "EC-1", "inverters": {}}, id="inverters-not-a-list"),
        pytest.param({"deviceId": "EC-1", "inverters": ["inv-1"]}, id="inverter-not-a-dict"),
        pytest.param(
            {"deviceId": "EC-1", "inverters": [{"inverterId": "inv-1"}]}, id="no-model-id"
        ),
        pytest.param({"deviceId": "EC-1", "inverters": [{"modelId": "m"}]}, id="no-inverter-id"),
        pytest.param(
            {"deviceId": "EC-1", "inverters": [_INV_DEYE, _INV_DEYE]}, id="duplicate-inverter"
        ),
    ],
)
async def test_edge_adopt_with_a_malformed_body_is_bad_request(hass: HomeAssistant, fields):
    from custom_components.svitgrid.island_lan import SvitgridEdgeAdoptView

    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridEdgeAdoptView().post(_Req(hass, admin.request("edge_adopt", **fields)))
    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"
    assert (await ks.load()).edge_boxes == {}


async def test_edge_adopt_needs_signature_and_fresh_nonce(hass: HomeAssistant):
    from custom_components.svitgrid.island_lan import SvitgridEdgeAdoptView

    admin = _Admin()
    ks = await _keystore(hass, admin)
    _entry(hass)
    fields = {"deviceId": "EC-1", "inverters": [_INV_DEYE]}

    no_key = await SvitgridEdgeAdoptView().post(
        _Req(hass, admin.request("edge_adopt", **fields), island_key=None)
    )
    assert no_key.status == 401
    stranger = _Admin()
    untrusted = await SvitgridEdgeAdoptView().post(
        _Req(hass, stranger.request("edge_adopt", **fields))
    )
    assert untrusted.status == 403
    assert _body(untrusted) == {"error": "signature_invalid"}
    other_action = await SvitgridEdgeAdoptView().post(
        _Req(hass, admin.request("edge_forget", **fields))
    )
    assert other_action.status == 400
    stale = await SvitgridEdgeAdoptView().post(
        _Req(hass, admin.signed({"action": "edge_adopt", "nonce": "n-0", "ts": 0, **fields}))
    )
    assert _body(stale) == {"error": "stale_request"}
    assert (await ks.load()).edge_boxes == {}

    signed = admin.request("edge_adopt", **fields)
    assert (await SvitgridEdgeAdoptView().post(_Req(hass, signed))).status == 200
    replayed = await SvitgridEdgeAdoptView().post(_Req(hass, signed))
    assert replayed.status == 403
    assert _body(replayed) == {"error": "replayed"}


async def test_edge_forget_removes_only_that_box(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)
    await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    await _adopt(hass, admin, "EC-2", _inv("inv-2"))
    gone = await queue.async_enqueue(
        "EC-1", "set_work_mode", {}, signing=None, inverter_id="inv-1", source="test"
    )
    kept = await queue.async_enqueue(
        "EC-2", "set_work_mode", {}, signing=None, inverter_id="inv-2", source="test"
    )

    resp = await _forget(hass, admin, "EC-1")

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True}
    assert list((await ks.load()).edge_boxes) == ["EC-2"]
    assert await queue.async_wait(gone, 0) is None
    assert (await queue.async_wait(kept, 0))["state"] == "pending"
    # Idempotent: a box already forgotten is not an error.
    again = await _forget(hass, admin, "EC-1")
    assert again.status == 200
    assert _body(again) == {"ok": True}
    assert list((await ks.load()).edge_boxes) == ["EC-2"]


async def test_edge_forget_needs_a_device_id(hass: HomeAssistant):
    from custom_components.svitgrid.island_lan import SvitgridEdgeForgetView

    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    resp = await SvitgridEdgeForgetView().post(_Req(hass, admin.request("edge_forget")))
    assert resp.status == 400


async def test_edge_status_reports_binding_and_last_refusal(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid import keystore as keystore_mod
    from custom_components.svitgrid.edge_api import EdgeMeView
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    store = _EdgeStore(
        box_states={"EC-1": {"lastContactAt": "2026-10-29T10:05:00.000Z", "version": "2.172.0"}},
        snapshots={
            "inv-1": {
                "inverter_id": "inv-1",
                "model_id": _INV_DEYE["modelId"],
                "read_at": "2026-10-29T10:04:00Z",
            }
        },
    )
    _entry_state(hass, entry, store=store, queue=queue)
    await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    code_2 = _body(await _adopt(hass, admin, "EC-2", _inv("inv-2")))["pairCode"]
    await ks.async_bind_edge_box("EC-1", "box-1-key")
    await queue.async_enqueue(
        "EC-1", "set_work_mode", {}, signing=None, inverter_id="inv-1", source="test"
    )

    # EC-2 presents its code after its ten minutes: the edge view refuses it,
    # and the status says so.
    later = keystore_mod._utcnow() + keystore_mod.EDGE_PAIR_CODE_TTL
    with patch.object(keystore_mod, "_utcnow", return_value=later):
        me = _Req(hass)
        me.headers = {"X-API-Key": "box-2-key", "X-Pair-Code": code_2}
        me.match_info = {}
        refused = await EdgeMeView().get(me)
    assert refused.status == 403
    assert _body(refused) == {"error": "pair_code_expired"}

    resp = await _edge_status(hass)

    assert resp.status == 200, _body(resp)
    boxes = {b["deviceId"]: b for b in _body(resp)["boxes"]}
    assert boxes["EC-1"] == {
        "deviceId": "EC-1",
        "bound": True,
        "codeExpiresAt": None,
        "lastContactAt": "2026-10-29T10:05:00.000Z",
        "version": "2.172.0",
        "pendingCommands": 1,
        "inverterIds": ["inv-1"],
        "lastSettingsReadAt": "2026-10-29T10:04:00Z",
        "lastRefusal": None,
    }
    ec2 = boxes["EC-2"]
    assert ec2["bound"] is False
    assert ec2["codeExpiresAt"].endswith("Z")
    assert ec2["lastContactAt"] is None
    assert ec2["version"] is None
    assert ec2["pendingCommands"] == 0
    assert ec2["inverterIds"] == ["inv-2"]
    assert ec2["lastSettingsReadAt"] is None
    assert ec2["lastRefusal"]["error"] == "pair_code_expired"
    assert ec2["lastRefusal"]["at"].endswith("Z")

    # Adopting the box again issues a fresh code and clears the refusal.
    await _adopt(hass, admin, "EC-2", _inv("inv-2"))
    boxes = {b["deviceId"]: b for b in _body(await _edge_status(hass))["boxes"]}
    assert boxes["EC-2"]["lastRefusal"] is None


async def test_edge_status_with_no_boxes_is_an_empty_list(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass)
    resp = await _edge_status(hass)
    assert resp.status == 200
    assert _body(resp) == {"boxes": []}


async def test_edge_status_needs_the_island_key(hass: HomeAssistant):
    await _keystore(hass, _Admin())
    _entry(hass)
    assert (await _edge_status(hass, island_key=None)).status == 401
    assert (await _edge_status(hass, island_key=OTHER_ISLAND_KEY)).status == 401


async def test_the_edge_views_are_registered_with_the_others(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert {
        "/api/svitgrid/edge/adopt",
        "/api/svitgrid/edge/forget",
        "/api/svitgrid/edge/status",
    } <= urls


# ── edge boxes: leave and the command view (edge island plan, Task 16) ──────


def _box_command(admin: _Admin, **signed_fields) -> dict:
    """The phone-signed `clear_island_endpoint` the box itself verifies."""
    signed = {
        "command": "clear_island_endpoint",
        "payload": {},
        "validUntil": "2026-10-29T10:20:00.000Z",
        **signed_fields,
    }
    return {
        "signedEventData": signed,
        "signature": sign_payload(signed, admin.priv),
        "signingKeyId": admin.key_id,
    }


async def _leave(hass, admin, device_id, **fields):
    from custom_components.svitgrid.island_lan import SvitgridEdgeLeaveView

    return await SvitgridEdgeLeaveView().post(
        _Req(hass, admin.request("edge_leave", deviceId=device_id, **fields))
    )


async def _edge_command(hass, command_id, **req):
    from custom_components.svitgrid.island_lan import SvitgridEdgeCommandView

    return await SvitgridEdgeCommandView().get(_Req(hass, **req), command_id)


async def _leave_setup(hass, tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)
    await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    await _adopt(hass, admin, "EC-2", _inv("inv-2"))
    return admin, queue


async def test_edge_leave_enqueues_clear_island_endpoint_with_the_box_signature(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, queue = await _leave_setup(hass, tmp_path)
    box_command = _box_command(admin)

    resp = await _leave(hass, admin, "EC-1", boxCommand=box_command)

    assert resp.status == 200, _body(resp)
    command_id = _body(resp)["commandId"]
    assert _body(resp) == {"commandId": command_id}
    assert await queue.async_pending_count("EC-2") == 0
    row = await queue.async_next_pending("EC-1")
    assert row["id"] == command_id
    assert row["command"] == "clear_island_endpoint"
    assert row["payload"] == {}
    assert row["source"] == "leave"
    assert row["inverter_id"] is None
    assert row["signing"] == box_command
    # The box receives the phone's signature untouched.
    sent = _command_for_box(row)
    assert sent["signedEventData"] == box_command["signedEventData"]
    assert sent["signature"] == box_command["signature"]
    assert sent["signingKeyId"] == box_command["signingKeyId"]


@pytest.mark.parametrize(
    "box_command",
    [
        pytest.param(None, id="absent"),
        pytest.param("signed", id="not-a-dict"),
        pytest.param({"signature": "s", "signingKeyId": "k"}, id="no-signed-event-data"),
        pytest.param(
            {"signedEventData": {"command": "clear_island_endpoint", "payload": {}}},
            id="no-signature",
        ),
        pytest.param(
            {
                "signedEventData": {"command": "clear_island_endpoint", "payload": {}},
                "signature": "s",
            },
            id="no-signing-key-id",
        ),
    ],
)
async def test_edge_leave_without_box_signature_is_400(hass: HomeAssistant, tmp_path, box_command):
    admin, queue = await _leave_setup(hass, tmp_path)
    fields = {} if box_command is None else {"boxCommand": box_command}

    resp = await _leave(hass, admin, "EC-1", **fields)

    assert resp.status == 400
    assert _body(resp) == {"error": "box_signature_required"}
    assert await queue.async_pending_count("EC-1") == 0


@pytest.mark.parametrize(
    "signed_fields",
    [
        pytest.param({"command": "set_island_endpoint"}, id="another-command"),
        pytest.param({"payload": {"url": "http://192.168.1.2:8123"}}, id="payload-with-url"),
    ],
)
async def test_edge_leave_box_command_must_be_an_empty_clear(
    hass: HomeAssistant, tmp_path, signed_fields
):
    admin, queue = await _leave_setup(hass, tmp_path)

    resp = await _leave(hass, admin, "EC-1", boxCommand=_box_command(admin, **signed_fields))

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"
    assert await queue.async_pending_count("EC-1") == 0


async def test_edge_leave_carries_no_url(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, queue = await _leave_setup(hass, tmp_path)

    resp = await _leave(
        hass,
        admin,
        "EC-1",
        boxCommand=_box_command(admin),
        url="http://192.168.1.2:8123",
        cloudEndpoint="https://api.example.com",
    )

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert row["payload"] == {}
    sent = json.dumps(_command_for_box(row))
    assert "http" not in sent
    assert "192.168" not in sent


async def test_edge_leave_unknown_box_is_404(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)

    resp = await _leave(hass, admin, "EC-9", boxCommand=_box_command(admin))

    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_box"}
    assert await queue.async_pending_count("EC-9") == 0


async def test_edge_leave_needs_signature_and_a_device_id(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.island_lan import SvitgridEdgeLeaveView

    admin, queue = await _leave_setup(hass, tmp_path)
    fields = {"deviceId": "EC-1", "boxCommand": _box_command(admin)}

    no_key = await SvitgridEdgeLeaveView().post(
        _Req(hass, admin.request("edge_leave", **fields), island_key=None)
    )
    assert no_key.status == 401
    untrusted = await SvitgridEdgeLeaveView().post(
        _Req(hass, _Admin().request("edge_leave", **fields))
    )
    assert _body(untrusted) == {"error": "signature_invalid"}
    other_action = await SvitgridEdgeLeaveView().post(
        _Req(hass, admin.request("edge_forget", **fields))
    )
    assert other_action.status == 400
    no_device = await SvitgridEdgeLeaveView().post(
        _Req(hass, admin.request("edge_leave", boxCommand=_box_command(admin)))
    )
    assert no_device.status == 400
    assert _body(no_device)["error"] == "bad_request"
    assert await queue.async_pending_count("EC-1") == 0


async def test_edge_command_view_reports_state(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)
    command_id = _body(await _leave(hass, admin, "EC-1", boxCommand=_box_command(admin)))[
        "commandId"
    ]

    pending = await _edge_command(hass, command_id)
    assert pending.status == 200, _body(pending)
    assert _body(pending) == {
        "id": command_id,
        "state": "pending",
        "result": None,
        "reason": None,
        "ackedAt": None,
    }

    await queue.async_next_pending("EC-1")
    assert _body(await _edge_command(hass, command_id))["state"] == "delivered"

    # The box acks before it reboots onto its cloud endpoint.
    await queue.async_ack(
        command_id, success=True, result={"cleared": True}, rejected=None, reason=None
    )
    acked = _body(await _edge_command(hass, command_id))
    assert acked["state"] == "success"
    assert acked["result"] == {"cleared": True}
    assert acked["reason"] is None
    assert acked["ackedAt"].endswith("Z")


async def test_edge_command_view_unknown_id_is_404(hass: HomeAssistant, tmp_path):
    await _leave_setup(hass, tmp_path)
    resp = await _edge_command(hass, "no-such-command")
    assert resp.status == 404
    assert _body(resp) == {"error": "unknown_command"}


async def test_edge_command_view_needs_the_island_key(hass: HomeAssistant, tmp_path):
    admin, _ = await _leave_setup(hass, tmp_path)
    command_id = _body(await _leave(hass, admin, "EC-1", boxCommand=_box_command(admin)))[
        "commandId"
    ]
    assert (await _edge_command(hass, command_id, island_key=None)).status == 401
    assert (await _edge_command(hass, command_id, island_key=OTHER_ISLAND_KEY)).status == 401


async def test_the_edge_leave_views_are_registered_with_the_others(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert {
        "/api/svitgrid/edge/leave",
        "/api/svitgrid/edge/commands/{command_id}",
    } <= urls


# ── island status lists edge boxes (edge island plan, Task 8) ──────────────


async def test_island_status_lists_edge_boxes(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass, cloud_ingest=False)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    store = _EdgeStore(
        box_states={"EC-1": {"lastContactAt": "2026-10-29T10:05:00.000Z", "version": "2.172.0"}}
    )
    _entry_state(hass, entry, store=store, queue=queue)
    await _adopt(hass, admin, "EC-1", _inv("inv-1"), _inv("inv-2"))
    await _adopt(hass, admin, "EC-2", _inv("inv-3"))
    await queue.async_enqueue(
        "EC-1", "set_work_mode", {}, signing=None, inverter_id="inv-1", source="test"
    )

    resp = await SvitgridIslandStatusView().get(_Req(hass))

    assert resp.status == 200, _body(resp)
    body = _body(resp)
    assert sorted(body["edgeBoxes"], key=lambda b: b["deviceId"]) == [
        {
            "deviceId": "EC-1",
            "lastContactAt": "2026-10-29T10:05:00.000Z",
            "version": "2.172.0",
            "pendingCommands": 1,
            "inverterIds": ["inv-1", "inv-2"],
        },
        {
            "deviceId": "EC-2",
            "lastContactAt": None,
            "version": None,
            "pendingCommands": 0,
            "inverterIds": ["inv-3"],
        },
    ]


async def test_island_status_without_edge_boxes_is_unchanged(hass: HomeAssistant):
    """An install that never adopted a box sees exactly the status it saw on
    0.25.0: the same capabilities and no `edgeBoxes` key."""
    await _keystore(hass, _Admin())
    _entry(hass, cloud_ingest=False)
    resp = await SvitgridIslandStatusView().get(_Req(hass))
    assert resp.status == 200
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
            "settings_lan",
        ],
    }


# ── a reload after adopt and forget (svitgrid#1252) ─────────────────────────


@pytest.fixture
def schedule_reload(hass):
    with patch.object(hass.config_entries, "async_schedule_reload") as m:
        yield m


def _loaded_entry(hass) -> MockConfigEntry:
    from homeassistant.config_entries import ConfigEntryState

    entry = _entry(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    _entry_state(hass, entry)
    return entry


async def test_adopt_schedules_a_reload_after_answering(hass: HomeAssistant, schedule_reload):
    """Setup builds the edge queue and the box's executors, so the entry is
    reloaded; but only once the answer, which carries the pairing code, is out."""
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _loaded_entry(hass)

    resp = await _adopt(hass, admin, "EC-1", _INV_DEYE)

    assert resp.status == 200, _body(resp)
    schedule_reload.assert_not_called()
    await asyncio.sleep(0)
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_adopting_an_adopted_box_again_schedules_a_reload(
    hass: HomeAssistant, schedule_reload
):
    """The app's retry after `pair_code_expired` may change the box's inverters."""
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _loaded_entry(hass)
    await _adopt(hass, admin, "EC-1", _INV_DEYE)
    await asyncio.sleep(0)
    schedule_reload.reset_mock()

    resp = await _adopt(hass, admin, "EC-1", _inv("inv-2"))
    await asyncio.sleep(0)

    assert resp.status == 200, _body(resp)
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_a_refused_adopt_does_not_reload(hass: HomeAssistant, schedule_reload):
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    _loaded_entry(hass)
    await _adopt(hass, admin, "EC-1", _INV_DEYE)
    await asyncio.sleep(0)
    schedule_reload.reset_mock()

    resp = await _adopt(hass, admin, "EC-2", _INV_DEYE)
    await asyncio.sleep(0)

    assert resp.status == 409
    schedule_reload.assert_not_called()


async def test_forget_of_a_held_box_schedules_a_reload(hass: HomeAssistant, schedule_reload):
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _loaded_entry(hass)
    await _adopt(hass, admin, "EC-1", _INV_DEYE)
    await asyncio.sleep(0)
    schedule_reload.reset_mock()

    resp = await _forget(hass, admin, "EC-1")

    assert resp.status == 200, _body(resp)
    schedule_reload.assert_not_called()
    await asyncio.sleep(0)
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_forget_of_an_unknown_box_does_not_reload(hass: HomeAssistant, schedule_reload):
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    _loaded_entry(hass)

    resp = await _forget(hass, admin, "EC-9")
    await asyncio.sleep(0)

    assert _body(resp) == {"ok": True}
    schedule_reload.assert_not_called()


async def test_adopt_reloads_only_entries_that_are_set_up(hass: HomeAssistant, schedule_reload):
    """An entry that is not loaded is not set up by an adopt."""
    import asyncio

    admin = _Admin()
    await _keystore(hass, admin)
    loaded = _loaded_entry(hass)
    _entry(hass)

    await _adopt(hass, admin, "EC-1", _INV_DEYE)
    await asyncio.sleep(0)

    schedule_reload.assert_called_once_with(loaded.entry_id)


# ── trusted keys reach the box (edge island plan, Task 17) ─────────────────


def _box_key_command(signer: _Admin, public_key_hex: str) -> dict:
    """The phone-signed `add_trusted_key` the box verifies: the signature
    covers the canonical `payload`, by a key the box already trusts."""
    payload = {"publicKeyHex": public_key_hex}
    return {
        "payload": payload,
        "signature": sign_payload(payload, signer.priv),
        "signerKeyId": signer.key_id,
    }


async def _key_add(hass, admin, new: _Admin, **fields):
    from custom_components.svitgrid.island_lan import SvitgridEdgeKeyAddView

    fields.setdefault("keyId", new.key_id)
    fields.setdefault("boxCommand", _box_key_command(admin, new.pub))
    return await SvitgridEdgeKeyAddView().post(_Req(hass, admin.request("edge_key_add", **fields)))


async def _trust_over_lan(hass, admin, new: _Admin):
    from custom_components.svitgrid.http_views import SvitgridTrustKeyView

    return await SvitgridTrustKeyView().post(
        _Req(hass, admin.request("trust_key", keyId=new.key_id, publicKeyHex=new.pub))
    )


async def _box_ack(hass, command_id, *, api_key, success=True, **body):
    from custom_components.svitgrid.edge_api import EdgeCommandAckView

    req = _Req(hass, {"success": success, **body})
    req.headers["X-API-Key"] = api_key
    return await EdgeCommandAckView().post(req, command_id)


async def _drain(queue, device_id) -> list[dict]:
    rows = []
    while (row := await queue.async_next_pending(device_id)) is not None:
        rows.append(row)
    return rows


async def test_edge_key_add_enqueues_add_trusted_key_with_box_signature(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, queue = await _leave_setup(hass, tmp_path)
    phone = _Admin()
    await _trust_over_lan(hass, admin, phone)
    box_command = _box_key_command(admin, phone.pub)

    resp = await _key_add(hass, admin, phone, boxCommand=box_command)

    assert resp.status == 200, _body(resp)
    command_ids = _body(resp)["commandIds"]
    assert set(command_ids) == {"EC-1", "EC-2"}
    for device_id in ("EC-1", "EC-2"):
        [row] = await _drain(queue, device_id)
        assert row["id"] == command_ids[device_id]
        assert row["command"] == "add_trusted_key"
        assert row["payload"] == {"publicKeyHex": phone.pub}
        assert row["inverter_id"] is None
        # The box reads the signature and its signer at the top level of the
        # command (`zigbee_poller.c` handle_key_management_command), not
        # under `signedEventData`.
        sent = _command_for_box(row)
        assert sent["command"] == "add_trusted_key"
        assert sent["payload"] == box_command["payload"]
        assert sent["signature"] == box_command["signature"]
        assert sent["signerKeyId"] == admin.key_id
        assert "signedEventData" not in sent


async def test_edge_key_add_success_clears_pending(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)
    ks = hass.data[DOMAIN]["keystore"]
    await ks.async_bind_edge_box("EC-1", "box-key-1")
    await ks.async_bind_edge_box("EC-2", "box-key-2")
    phone = _Admin()
    await _trust_over_lan(hass, admin, phone)
    assert [k["keyId"] for k in await ks.async_list_pending_box_keys()] == [phone.key_id]

    command_ids = _body(await _key_add(hass, admin, phone))["commandIds"]
    await _drain(queue, "EC-1")
    await _drain(queue, "EC-2")

    # A refusal leaves the key pending.
    await _box_ack(
        hass,
        command_ids["EC-1"],
        api_key="box-key-1",
        success=False,
        rejected=True,
        reason="unknown_signer_key",
    )
    assert [k["keyId"] for k in await ks.async_list_pending_box_keys()] == [phone.key_id]

    # One box of two holding it is not enough.
    await _box_ack(hass, command_ids["EC-2"], api_key="box-key-2", result={"keyAdded": True})
    assert [k["keyId"] for k in await ks.async_list_pending_box_keys()] == [phone.key_id]

    await _box_ack(hass, command_ids["EC-1"], api_key="box-key-1", result={"keyAdded": True})
    # The first answer stands for a command (`async_ack`), so re-add for EC-1.
    retry = _body(await _key_add(hass, admin, phone))["commandIds"]
    await _drain(queue, "EC-1")
    await _box_ack(hass, retry["EC-1"], api_key="box-key-1", result={"keyAdded": True})
    assert await ks.async_list_pending_box_keys() == []


async def test_pending_box_keys_list_each_keys_public_key_hex(hass: HomeAssistant, tmp_path):
    admin, _ = await _leave_setup(hass, tmp_path)
    phone = _Admin()
    await _trust_over_lan(hass, admin, phone)

    expected = [{"keyId": phone.key_id, "label": None, "publicKeyHex": phone.pub}]
    edge = _body(await _edge_status(hass))
    assert edge["pendingBoxKeys"] == expected
    island = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert island["pendingBoxKeys"] == expected


async def test_pending_box_keys_name_the_phone_a_key_is_linked_to(hass: HomeAssistant, tmp_path):
    admin, _ = await _leave_setup(hass, tmp_path)
    ks = hass.data[DOMAIN]["keystore"]
    phone = _Admin()
    await ks.async_add_island_key("phone-2", OTHER_ISLAND_KEY, label="Pixel 9")
    await ks.async_link_signing_key(phone.key_id, "phone-2")
    await _trust_over_lan(hass, admin, phone)

    [pending] = await ks.async_list_pending_box_keys()
    assert pending["label"] == "Pixel 9"


async def test_a_revoked_key_is_no_longer_pending(hass: HomeAssistant, tmp_path):
    admin, _ = await _leave_setup(hass, tmp_path)
    phone = _Admin()
    await _trust_over_lan(hass, admin, phone)

    await SvitgridKeysRevokeView().post(_Req(hass, admin.request("revoke_key", keyId=phone.key_id)))

    assert _body(await _edge_status(hass))["pendingBoxKeys"] == []


async def test_revoke_enqueues_revoke_trusted_key_for_each_box(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)
    phone = _Admin()
    await _trust_over_lan(hass, admin, phone)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=phone.key_id))
    )

    assert resp.status == 200, _body(resp)
    for device_id in ("EC-1", "EC-2"):
        [row] = await _drain(queue, device_id)
        assert row["command"] == "revoke_trusted_key"
        assert row["payload"] == {"keyIdToRevoke": phone.key_id}
        # The box takes a revocation without a signature (`command_auth.c`).
        assert row["signing"] is None


async def test_a_refused_revoke_enqueues_nothing(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)

    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=admin.key_id))
    )

    assert resp.status == 409
    assert await queue.async_pending_count("EC-1") == 0
    assert await queue.async_pending_count("EC-2") == 0


async def test_without_an_adopted_box_trust_and_revoke_enqueue_nothing(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)
    phone = _Admin()

    assert (await _trust_over_lan(hass, admin, phone)).status == 200
    assert await ks.async_list_pending_box_keys() == []
    state = await ks.load()
    assert state.box_pending_keys == {}
    resp = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=phone.key_id))
    )
    assert resp.status == 200
    assert await queue.async_recent("EC-1") == []
    status = _body(await SvitgridIslandStatusView().get(_Req(hass)))
    assert "pendingBoxKeys" not in status


async def test_edge_key_add_without_an_adopted_box_is_404(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)
    phone = _Admin()

    resp = await _key_add(hass, admin, phone)

    assert resp.status == 404
    assert _body(resp) == {"error": "no_edge_boxes"}


@pytest.mark.parametrize(
    "box_command",
    [
        pytest.param(None, id="absent"),
        pytest.param("signed", id="not-a-dict"),
        pytest.param({"signature": "s", "signerKeyId": "k"}, id="no-payload"),
        pytest.param({"payload": {"publicKeyHex": "04"}, "signerKeyId": "k"}, id="no-signature"),
        pytest.param({"payload": {"publicKeyHex": "04"}, "signature": "s"}, id="no-signer"),
    ],
)
async def test_edge_key_add_without_box_signature_is_400(
    hass: HomeAssistant, tmp_path, box_command
):
    from custom_components.svitgrid.island_lan import SvitgridEdgeKeyAddView

    admin, queue = await _leave_setup(hass, tmp_path)
    phone = _Admin()
    fields = {"keyId": phone.key_id}
    if box_command is not None:
        fields["boxCommand"] = box_command
    resp = await SvitgridEdgeKeyAddView().post(_Req(hass, admin.request("edge_key_add", **fields)))

    assert resp.status == 400
    assert _body(resp) == {"error": "box_signature_required"}
    assert await queue.async_pending_count("EC-1") == 0


async def test_edge_key_add_payload_must_be_the_key_named(hass: HomeAssistant, tmp_path):
    admin, queue = await _leave_setup(hass, tmp_path)
    phone, other = _Admin(), _Admin()

    mismatch = await _key_add(hass, admin, phone, boxCommand=_box_key_command(admin, other.pub))
    assert mismatch.status == 400
    assert _body(mismatch)["error"] == "key_id_mismatch"

    not_a_key = await _key_add(hass, admin, phone, boxCommand=_box_key_command(admin, "zz"))
    assert not_a_key.status == 400
    assert _body(not_a_key)["error"] == "bad_public_key"

    extra = _box_key_command(admin, phone.pub)
    extra["payload"] = {**extra["payload"], "keyId": phone.key_id}
    assert (await _key_add(hass, admin, phone, boxCommand=extra)).status == 400
    assert await queue.async_pending_count("EC-1") == 0


async def test_edge_key_add_needs_signature_and_island_key(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.island_lan import SvitgridEdgeKeyAddView

    admin, queue = await _leave_setup(hass, tmp_path)
    phone, stranger = _Admin(), _Admin()
    body = admin.request(
        "edge_key_add", keyId=phone.key_id, boxCommand=_box_key_command(admin, phone.pub)
    )

    no_key = await SvitgridEdgeKeyAddView().post(_Req(hass, body, island_key=None))
    assert no_key.status == 401
    untrusted = stranger.request(
        "edge_key_add", keyId=phone.key_id, boxCommand=_box_key_command(admin, phone.pub)
    )
    assert (await SvitgridEdgeKeyAddView().post(_Req(hass, untrusted))).status == 403
    wrong_action = admin.request(
        "edge_leave", keyId=phone.key_id, boxCommand=_box_key_command(admin, phone.pub)
    )
    assert (await SvitgridEdgeKeyAddView().post(_Req(hass, wrong_action))).status == 400
    assert await queue.async_pending_count("EC-1") == 0


async def test_the_edge_key_add_view_is_registered_with_the_others(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert "/api/svitgrid/edge/keys/add" in urls


# ── island status advertises each inverter's writes (edge island plan, Task 35) ─

_VOCAB = json.loads(
    (Path(__file__).parent / "fixtures" / "island_capability_vocab.json").read_text()
)


def _ha_entry(hass, *inverter_ids) -> MockConfigEntry:
    """A cloud-paired true island whose inverters Home Assistant harvests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "api_key": "k",
            "edge_device_id": "ed-1",
            "cloud_ingest_enabled": False,
            "inverters": [
                {"inverter_id": i, "harvest_config": {"model_id": "deye_sg04lp3"}}
                for i in inverter_ids
            ],
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_island_status_without_edge_boxes_lists_ha_inverters_only(hass: HomeAssistant):
    """An install that never adopted a box keeps its 0.25.0 status body, plus
    `inverters`. A Home Assistant inverter names only its host: with no
    `writableActions` the app keeps today's rule for it."""
    await _keystore(hass, _Admin())
    _ha_entry(hass, "inv-ha-1", "inv-ha-2")
    resp = await SvitgridIslandStatusView().get(_Req(hass))
    assert resp.status == 200
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
            "settings_lan",
        ],
        "inverters": {"inv-ha-1": {"host": "ha"}, "inv-ha-2": {"host": "ha"}},
    }


async def test_island_status_advertises_each_edge_inverters_writes(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _ha_entry(hass, "inv-ha")
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)
    await _adopt(
        hass,
        admin,
        "EC-1",
        _inv("inv-deye"),
        _inv("inv-pi30", modelId="edecoa_em_624a", transport="pi30_serial", protocolId="pi30"),
        _inv("inv-must", modelId="must_pv18_3224", transport="modbus_rtu", protocolId="must"),
        _inv(
            "inv-sna",
            modelId="luxpower_sna_5000_wpv",
            transport="modbus_rtu",
            protocolId="modbus_rtu",
        ),
    )
    await _adopt(
        hass,
        admin,
        "EC-2",
        _inv("inv-solax", modelId="solax_x3_hybrid_g4", transport="modbus_tcp"),
        _inv("inv-solis", modelId="solis_30k_5g"),
    )

    body = _body(await SvitgridIslandStatusView().get(_Req(hass)))

    deye = _VOCAB["brandKinds"]["deye"]
    assert body["inverters"] == {
        "inv-ha": {"host": "ha"},
        "inv-deye": {"host": "edge", **deye},
        # Battery charge and use battery run as priority profiles, and each
        # protocol writes its own thresholds (svitgrid#1255). sell_to_grid has
        # no profile, so the island does not write it.
        "inv-pi30": {
            "host": "edge",
            "writableActions": ["pi30_thresholds"],
            "modes": ["battery_charge", "use_battery"],
        },
        "inv-must": {
            "host": "edge",
            "writableActions": ["must_thresholds"],
            "modes": ["battery_charge", "use_battery"],
        },
        # An SNA has no battery_first profile.
        "inv-sna": {"host": "edge", "writableActions": [], "modes": ["battery_charge"]},
        # Review focus 5: SolaX on an edge island offers no writable action.
        "inv-solax": {"host": "edge", "writableActions": [], "modes": []},
        "inv-solis": {"host": "edge", "writableActions": [], "modes": []},
    }


async def test_island_status_advert_stays_inside_the_vocabulary(hass: HomeAssistant, tmp_path):
    """Every host, action and mode the advert names is in the vocabulary the
    app is built against, and within what that brand kind may advertise."""
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
    from custom_components.svitgrid.harvest.brand_commands import brand_kind

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _ha_entry(hass, "inv-ha")
    _entry_state(hass, entry, queue=EdgeCommandQueue(str(tmp_path / "q.db"), None))
    inverters = [
        _inv("i-deye"),
        _inv("i-pi30", modelId="edecoa_em_624a", transport="pi30_serial", protocolId="pi30"),
        _inv("i-must", modelId="must_pv18_3224", transport="modbus_rtu", protocolId="must"),
        _inv("i-smg2", modelId="anenji_anj_4200", transport=None, protocolId="eybond_at"),
        _inv("i-srne", modelId="srne_hsi_1500", transport=None, protocolId=None),
        _inv("i-solax", modelId="solax_x3_hybrid_g4", transport="modbus_tcp"),
        _inv("i-victron", modelId="victron_multiplus_ii", transport="modbus_tcp"),
    ]
    await _adopt(hass, admin, "EC-1", *inverters)

    advert = _body(await SvitgridIslandStatusView().get(_Req(hass)))["inverters"]

    for inv in inverters:
        item = advert[inv["inverterId"]]
        assert item["host"] in _VOCAB["hosts"]
        ceiling = _VOCAB["brandKinds"][brand_kind(inv)]
        assert set(item["writableActions"]) <= set(ceiling["writableActions"])
        assert set(item["writableActions"]) <= set(_VOCAB["actionTypes"])
        assert set(item["modes"]) <= set(ceiling["modes"])
        assert set(item["modes"]) <= set(_VOCAB["writingModes"])
    assert advert["inv-ha"]["host"] in _VOCAB["hosts"]


async def test_island_status_advert_matches_what_the_evaluator_writes(
    hass: HomeAssistant, tmp_path
):
    """An action the advert lists is one the island evaluator writes for that
    inverter, and a vocabulary action it leaves out is one the evaluator
    refuses with unsupported_field: the app never offers what the scheduler
    then skips (review focus 5)."""
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
    from custom_components.svitgrid.harvest.brand_commands import brand_kind
    from custom_components.svitgrid.harvest.event_evaluator import evaluate_event

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass, cloud_ingest=False)
    _entry_state(hass, entry, queue=EdgeCommandQueue(str(tmp_path / "q.db"), None))
    inverters = [
        _inv("i-deye"),
        _inv("i-pi30", modelId="edecoa_em_624a", transport="pi30_serial", protocolId="pi30"),
        _inv("i-must", modelId="must_pv18_3224", transport="modbus_rtu", protocolId="must"),
        _inv("i-solax", modelId="solax_x3_hybrid_g4", transport="modbus_tcp"),
    ]
    await _adopt(hass, admin, "EC-1", *inverters)

    advert = _body(await SvitgridIslandStatusView().get(_Req(hass)))["inverters"]

    now = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)
    for inv in inverters:
        kind = brand_kind(inv)
        listed = set(advert[inv["inverterId"]]["writableActions"])
        for action in _VOCAB["brandKinds"][kind]["writableActions"]:
            event = {
                "id": "evt-1",
                "inverterId": inv["inverterId"],
                "enabled": True,
                "mode": "custom",
                "schedule": {"recurrence": "daily", "startTime": "00:00", "endTime": "23:59"},
                "config": {"customActions": [{"type": action}]},
            }
            decision = evaluate_event(
                event, {"batterySoc": 50}, {"status": "idle"}, now, "UTC", inverter=inv
            )
            refused = decision.skip_reason == "unsupported_field"
            assert refused is (action not in listed), (inv["inverterId"], action)


# ── Zigbee over the LAN for the app (edge island plan, Task 43) ─────────────


PLUG_IEEE = "A4:C1:38:11:11:46:FF:FF"
PLUG_ID = "zb-a4c138111146ffff"


class _ZReq(_Req):
    def __init__(self, hass, body=None, *, query=None, **kwargs):
        super().__init__(hass, body, **kwargs)
        self.query = dict(query or {})


async def _zigbee_setup(hass, tmp_path, *, adopt=True):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
    from custom_components.svitgrid.zigbee_devices import ZigbeeDeviceStore

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    devices = ZigbeeDeviceStore(str(tmp_path / "zigbee_devices.db"), None)
    _entry_state(hass, entry, queue=queue)["zigbee_devices"] = devices
    if adopt:
        await _adopt(hass, admin, "EC-1", _inv("inv-1"))
        await devices.async_add("EC-1", PLUG_IEEE, "0x1234", 1)
    return admin, queue, devices


def _set_state_box_command(signer: _Admin, on: bool = True) -> dict:
    """The phone-signed `set_state` the box verifies, naming HA's device id."""
    signed = {
        "command": "set_state",
        "payload": {"on": on},
        "smartDeviceId": PLUG_ID,
        "validUntil": "2026-10-30T10:20:00.000Z",
    }
    return {
        "signedEventData": signed,
        "signature": sign_payload(signed, signer.priv),
        "signingKeyId": signer.key_id,
    }


async def _switch(hass, signer: _Admin, box_command, device_id=PLUG_ID, **req):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    body = signer.request("edge_zigbee_command", command="set_state", boxCommand=box_command)
    return await ZigbeeDeviceCommandView().post(_ZReq(hass, body, **req), device_id)


async def test_set_state_reaches_the_box_with_address_endpoint_and_expiry(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.edge_api import _command_for_box

    admin, queue, devices = await _zigbee_setup(hass, tmp_path)
    box_command = _set_state_box_command(admin)

    resp = await _switch(hass, admin, box_command)

    assert resp.status == 200, _body(resp)
    command_id = _body(resp)["commandId"]
    row = await queue.async_next_pending("EC-1")
    assert row["id"] == command_id
    sent = _command_for_box(row)
    assert sent["command"] == "set_state"
    assert sent["payload"] == {"on": True}
    # `zigbee_poller.c` reads these beside `payload`, not inside it.
    assert sent["zigbeeAddr"] == "0x1234"
    assert sent["zigbeeEndpoint"] == 1
    assert 0 < sent["expiresInMs"] <= 60_000
    # The phone's signature reaches the box untouched.
    assert sent["signedEventData"] == box_command["signedEventData"]
    assert sent["signature"] == box_command["signature"]
    assert sent["signingKeyId"] == box_command["signingKeyId"]
    history = await devices.async_history(PLUG_ID)
    assert [(h["event"], h["state"], h["commandId"]) for h in history] == [
        ("command_sent", {"on": True}, command_id)
    ]


async def test_an_unsigned_set_state_is_refused(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    admin, queue, _ = await _zigbee_setup(hass, tmp_path)

    # The island key alone, no signature at all.
    plain = await ZigbeeDeviceCommandView().post(
        _ZReq(hass, {"command": "set_state", "payload": {"on": True}}), PLUG_ID
    )
    # A trusted request that carries no phone signature for the box.
    no_box = await _switch(hass, admin, None)

    assert plain.status == 400
    assert _body(plain) == {"error": "box_signature_required"}
    assert no_box.status == 400
    assert _body(no_box) == {"error": "box_signature_required"}
    assert await queue.async_pending_count("EC-1") == 0


async def test_a_member_may_rename_but_not_switch(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceView

    _, queue, devices = await _zigbee_setup(hass, tmp_path)
    # A view-only phone: an island key, and a signing key HA does not trust.
    member = _Admin()

    switched = await _switch(hass, member, _set_state_box_command(member))
    renamed = await ZigbeeDeviceView().patch(
        _ZReq(hass, {"name": "Бойлер", "roomId": "kitchen"}), PLUG_ID
    )

    assert switched.status == 403
    assert _body(switched) == {"error": "signature_invalid"}
    assert await queue.async_pending_count("EC-1") == 0
    assert renamed.status == 200, _body(renamed)
    device = await devices.async_get(PLUG_ID)
    assert (device["name"], device["room"]) == ("Бойлер", "kitchen")


async def test_permit_join_opens_the_window_task_41_reads(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeePermitJoinView

    admin, queue, devices = await _zigbee_setup(hass, tmp_path)
    signed = {
        "command": "permit_join",
        "payload": {"duration": 120},
        "hardwareId": "EC-1",
        "validUntil": "2026-10-30T10:20:00.000Z",
    }
    box_command = {
        "signedEventData": signed,
        "signature": sign_payload(signed, admin.priv),
        "signingKeyId": admin.key_id,
    }
    assert not await devices.async_permit_join_open("EC-1")

    resp = await ZigbeePermitJoinView().post(
        _ZReq(hass, admin.request("edge_permit_join", deviceId="EC-1", boxCommand=box_command))
    )

    assert resp.status == 200, _body(resp)
    assert await devices.async_permit_join_open("EC-1")
    row = await queue.async_next_pending("EC-1")
    assert row["command"] == "permit_join"
    assert row["payload"] == {"duration": 120}
    assert row["signing"] == box_command


async def test_a_member_cannot_open_pairing(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeePermitJoinView

    _, queue, devices = await _zigbee_setup(hass, tmp_path)
    member = _Admin()
    signed = {"command": "permit_join", "payload": {"duration": 120}}
    box_command = {
        "signedEventData": signed,
        "signature": sign_payload(signed, member.priv),
        "signingKeyId": member.key_id,
    }

    resp = await ZigbeePermitJoinView().post(
        _ZReq(hass, member.request("edge_permit_join", deviceId="EC-1", boxCommand=box_command))
    )

    assert resp.status == 403
    assert not await devices.async_permit_join_open("EC-1")
    assert await queue.async_pending_count("EC-1") == 0


async def test_zigbee_devices_read_back_in_the_cloud_shape(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import (
        ZigbeeDeviceHistoryView,
        ZigbeeDevicesView,
        ZigbeeDeviceView,
    )

    _, _, devices = await _zigbee_setup(hass, tmp_path)
    await devices.async_update(PLUG_ID, {"on": True, "model": "TS011F", "name": "Бойлер"})
    await devices.async_store_socket_readings(
        "EC-1",
        "2026-10-30T10:00:00.000Z",
        [{"addr": "0x1234", "power": 1500, "voltage": 230, "current": 6.5, "energy": 1200}],
        profiles={},
    )

    listed = await ZigbeeDevicesView().get(_ZReq(hass))
    one = await ZigbeeDeviceView().get(_ZReq(hass), PLUG_ID)
    history = await ZigbeeDeviceHistoryView().get(_ZReq(hass), PLUG_ID)
    unknown = await ZigbeeDeviceView().get(_ZReq(hass), "zb-0000")

    assert listed.status == 200
    [plug] = _body(listed)["smartDevices"]
    assert _body(one) == plug
    assert plug["id"] == PLUG_ID
    assert plug["edgeDeviceId"] == "EC-1"
    assert plug["type"] == "switch"
    assert plug["protocol"] == "zigbee"
    assert (plug["zigbeeAddr"], plug["zigbeeIeee"], plug["zigbeeEndpoint"]) == (
        "0x1234",
        PLUG_IEEE,
        1,
    )
    assert plug["name"] == "Бойлер"
    assert plug["status"] == "online"
    assert plug["state"]["on"] is True
    assert plug["state"]["power"] == 1500
    assert plug["state"]["powerAt"] == "2026-10-30T10:00:00.000Z"
    assert _body(history) == {"history": []}
    assert unknown.status == 404


async def test_sensor_readings_come_back_in_ten_minute_slots(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceReadingsView

    _, _, devices = await _zigbee_setup(hass, tmp_path)
    now = datetime.now(UTC)
    await devices.async_store_sensor_readings(
        "EC-1",
        [
            {"addr": "0x1234", "ep": 1, "ageMs": 0, "temperature": 2150, "humidity": 4000},
            {"addr": "0x1234", "ep": 1, "ageMs": 1000, "temperature": 2160, "battery": 90},
        ],
        now,
    )

    resp = await ZigbeeDeviceReadingsView().get(_ZReq(hass, query={"hours": "2"}), PLUG_ID)

    assert resp.status == 200, _body(resp)
    body = _body(resp)
    assert (body["hours"], body["bucketMinutes"]) == (2, 10)
    [point] = body["points"]
    assert (point["temperature"], point["humidity"], point["battery"]) == (2155, 4000, 90)


async def test_zigbee_views_answer_no_edge_boxes_before_auth(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import (
        ZigbeeBoxCommandView,
        ZigbeeDeviceCommandView,
        ZigbeeDevicesView,
        ZigbeePermitJoinView,
    )

    await _zigbee_setup(hass, tmp_path, adopt=False)

    for resp in (
        await ZigbeeDevicesView().get(_ZReq(hass, island_key=None)),
        await ZigbeeDeviceCommandView().post(
            _ZReq(hass, {"command": "identify"}, island_key=None), PLUG_ID
        ),
        await ZigbeeBoxCommandView().post(_ZReq(hass, {}, island_key=None)),
        await ZigbeePermitJoinView().post(_ZReq(hass, {}, island_key=None)),
    ):
        assert resp.status == 404
        assert _body(resp) == {"error": "no_edge_boxes"}


async def test_the_zigbee_reads_need_the_island_key(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDevicesView

    await _zigbee_setup(hass, tmp_path)

    resp = await ZigbeeDevicesView().get(_ZReq(hass, island_key=OTHER_ISLAND_KEY))

    assert resp.status == 401


async def test_remove_device_queues_the_leave_and_forgets_the_device(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    _, queue, devices = await _zigbee_setup(hass, tmp_path)

    resp = await ZigbeeDeviceCommandView().post(_ZReq(hass, {"command": "remove_device"}), PLUG_ID)

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert (row["command"], row["payload"]) == (
        "remove_device",
        {"addr": "0x1234", "ieee": PLUG_IEEE},
    )
    assert row["signing"] is None
    assert await devices.async_get(PLUG_ID) is None


async def test_identify_carries_the_address_and_endpoint(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    _, queue, _ = await _zigbee_setup(hass, tmp_path)

    resp = await ZigbeeDeviceCommandView().post(
        _ZReq(hass, {"command": "identify", "payload": {"seconds": 5}}), PLUG_ID
    )

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    assert (row["command"], row["payload"]) == (
        "identify",
        {"seconds": 5, "addr": "0x1234", "ep": 1},
    )


async def test_box_commands_with_the_island_key_and_owner_only_ones(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeBoxCommandView

    admin, queue, _ = await _zigbee_setup(hass, tmp_path)
    member = _Admin()

    listed = await ZigbeeBoxCommandView().post(
        _ZReq(hass, {"deviceId": "EC-1", "command": "list_devices"})
    )
    unsigned_reset = await ZigbeeBoxCommandView().post(
        _ZReq(hass, {"deviceId": "EC-1", "command": "zigbee_factory_reset"})
    )
    member_reset = await ZigbeeBoxCommandView().post(
        _ZReq(
            hass,
            member.request(
                "edge_zigbee_box_command", deviceId="EC-1", command="zigbee_factory_reset"
            ),
        )
    )
    owner_restart = await ZigbeeBoxCommandView().post(
        _ZReq(
            hass,
            admin.request("edge_zigbee_box_command", deviceId="EC-1", command="zigbee_restart"),
        )
    )
    unknown_box = await ZigbeeBoxCommandView().post(
        _ZReq(hass, {"deviceId": "EC-9", "command": "mesh_snapshot"})
    )

    assert listed.status == 200, _body(listed)
    assert (unsigned_reset.status, _body(unsigned_reset)) == (403, {"error": "owner_only"})
    assert member_reset.status == 403
    assert owner_restart.status == 200, _body(owner_restart)
    assert unknown_box.status == 404
    commands = []
    while (row := await queue.async_next_pending("EC-1")) is not None:
        commands.append(row["command"])
        await queue.async_ack(row["id"], success=True, result=None, rejected=None, reason=None)
    assert commands == ["list_devices", "zigbee_restart"]


async def test_appliance_type_is_stored_and_served_back(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDevicesView, ZigbeeDeviceView

    _, _, devices = await _zigbee_setup(hass, tmp_path)
    fresh = await ZigbeeDeviceView().get(_ZReq(hass), PLUG_ID)

    set_ = await ZigbeeDeviceView().patch(_ZReq(hass, {"applianceType": "water_heater"}), PLUG_ID)
    one = await ZigbeeDeviceView().get(_ZReq(hass), PLUG_ID)
    [listed] = _body(await ZigbeeDevicesView().get(_ZReq(hass)))["smartDevices"]
    cleared = await ZigbeeDeviceView().patch(_ZReq(hass, {"applianceType": None}), PLUG_ID)

    assert _body(fresh)["applianceType"] is None
    assert set_.status == 200, _body(set_)
    assert _body(one)["applianceType"] == "water_heater"
    assert listed["applianceType"] == "water_heater"
    assert cleared.status == 200, _body(cleared)
    assert (await devices.async_get(PLUG_ID))["applianceType"] is None


async def test_a_bad_appliance_type_is_refused(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceView

    _, _, devices = await _zigbee_setup(hass, tmp_path)
    await devices.async_update(PLUG_ID, {"applianceType": "oven"})

    for bad in ("", "Fridge", "space ship", 3, "x" * 65):
        resp = await ZigbeeDeviceView().patch(_ZReq(hass, {"applianceType": bad}), PLUG_ID)
        assert resp.status == 400, bad

    assert (await devices.async_get(PLUG_ID))["applianceType"] == "oven"


async def test_a_sensor_has_no_appliance_type(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceView

    _, _, devices = await _zigbee_setup(hass, tmp_path)
    await devices.async_store_sensor_readings(
        "EC-1", [{"addr": "0x1234", "ep": 1, "ageMs": 0, "temperature": 2150}], datetime.now(UTC)
    )

    given = await ZigbeeDeviceView().patch(_ZReq(hass, {"applianceType": "fridge"}), PLUG_ID)
    cleared = await ZigbeeDeviceView().patch(_ZReq(hass, {"applianceType": None}), PLUG_ID)

    assert given.status == 400
    assert cleared.status == 200, _body(cleared)
    assert (await devices.async_get(PLUG_ID))["applianceType"] is None


async def test_a_store_written_before_appliance_type_gains_it(tmp_path):
    import sqlite3

    from custom_components.svitgrid import zigbee_devices
    from custom_components.svitgrid.zigbee_devices import ZigbeeDeviceStore

    db = tmp_path / "zigbee_devices.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE zigbee_devices (id TEXT PRIMARY KEY, ieee TEXT NOT NULL,"
        " addr TEXT NOT NULL, endpoint INTEGER NOT NULL, device_id TEXT NOT NULL,"
        " on_state INTEGER, reachable INTEGER NOT NULL DEFAULT 1, unreachable_since TEXT,"
        " caps INTEGER, model TEXT, manufacturer TEXT, name TEXT, room TEXT,"
        " added_at TEXT NOT NULL, last_seen TEXT)"
    )
    conn.execute(
        "INSERT INTO zigbee_devices (id, ieee, addr, endpoint, device_id, added_at)"
        " VALUES (?, ?, '0x1234', 1, 'EC-1', '2026-10-30T10:00:00.000Z')",
        (PLUG_ID, PLUG_IEEE),
    )
    conn.commit()
    conn.close()
    zigbee_devices._INITIALIZED.discard(str(db))
    devices = ZigbeeDeviceStore(str(db), None)

    assert (await devices.async_get(PLUG_ID))["applianceType"] is None
    await devices.async_update(PLUG_ID, {"applianceType": "fridge"})
    assert (await devices.async_get(PLUG_ID))["applianceType"] == "fridge"


async def test_set_startup_state_is_queued_with_the_address_and_endpoint(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    _, queue, _ = await _zigbee_setup(hass, tmp_path)

    resp = await ZigbeeDeviceCommandView().post(
        _ZReq(hass, {"command": "set_startup_state", "payload": {"mode": "off"}}), PLUG_ID
    )

    assert resp.status == 200, _body(resp)
    row = await queue.async_next_pending("EC-1")
    # `zigbee_poller.c` reads addr, ep and mode from the payload.
    assert (row["command"], row["payload"]) == (
        "set_startup_state",
        {"mode": "off", "addr": "0x1234", "ep": 1},
    )
    # The firmware does not require a signature for it (`command_auth.c`).
    assert row["signing"] is None


async def test_set_startup_state_needs_a_mode_the_box_knows(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.zigbee_lan import ZigbeeDeviceCommandView

    _, queue, _ = await _zigbee_setup(hass, tmp_path)

    for payload in (None, {}, {"mode": "Restore"}, {"mode": "toggle"}, {"mode": 1}):
        body = {"command": "set_startup_state"}
        if payload is not None:
            body["payload"] = payload
        resp = await ZigbeeDeviceCommandView().post(_ZReq(hass, body), PLUG_ID)
        assert resp.status == 400, payload

    assert await queue.async_pending_count("EC-1") == 0


async def test_the_zigbee_views_are_registered(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert {
        "/api/svitgrid/zigbee/devices",
        "/api/svitgrid/zigbee/devices/{device_id}",
        "/api/svitgrid/zigbee/devices/{device_id}/readings",
        "/api/svitgrid/zigbee/devices/{device_id}/history",
        "/api/svitgrid/edge/zigbee/{device_id}/command",
        "/api/svitgrid/edge/zigbee/command",
        "/api/svitgrid/edge/permit-join",
    } <= urls


# ── Battery packs over the LAN for the app (edge island plan, Task 44) ─────


_BMS_REPORT = json.loads((Path(__file__).parent / "fixtures" / "bms_report.json").read_text())
_BMS_REPORT.pop("_comment")
PACK_ID = "a4c138000001"


async def _bms_setup(hass, tmp_path, *, adopt=True):
    from custom_components.svitgrid.bms_packs import BmsPackStore

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass)
    packs = BmsPackStore(str(tmp_path / "bms_packs.db"), None)
    _entry_state(hass, entry)["bms_packs"] = packs
    if adopt:
        await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    return packs


async def test_packs_read_back_in_the_clouds_shape(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import (
        BmsPackHistoryView,
        BmsPacksView,
        BmsReportView,
    )

    packs = await _bms_setup(hass, tmp_path)
    received = datetime.now(UTC)
    await packs.async_store_report("EC-1", _BMS_REPORT, received_at=received)

    listed = await BmsPacksView().get(_ZReq(hass))

    assert listed.status == 200, _body(listed)
    body = _body(listed)
    good, unread, refused = body["packs"]
    assert good == {
        "packId": PACK_ID,
        "assignedEdgeId": "EC-1",
        "driver": "jk_bd6a",
        "driverSource": "owner",
        "driverSetAt": None,
        "suggestedDriver": None,
        "addrType": None,
        "label": "Гараж",
        "nominalMah": 280000,
        "adoptedAt": 1761800000,
        "lastOkAt": int(received.timestamp() * 1000),
        "rosterError": None,
        "move": None,
    }
    assert (unread["packId"], unread["lastOkAt"], unread["driver"]) == (
        "a4c138000002",
        None,
        None,
    )
    # A pack the box refused to adopt is listed with the reason.
    assert (refused["packId"], refused["assignedEdgeId"], refused["rosterError"]) == (
        "c0ffee000003",
        "EC-1",
        "unknown_driver",
    )
    board = body["boards"]["EC-1"]
    assert board["lastReportAt"] is not None
    assert "reportIntervalMs" in board
    # The box's live report, as the cloud keeps it on `edgeDevices/{id}.bms`.
    assert [p["id"] for p in board["bms"]["packs"]] == [PACK_ID, "a4c138000002"]

    now_ms = int(received.timestamp() * 1000)
    history = await BmsPackHistoryView().get(
        _ZReq(hass, query={"from": str(now_ms - 3_600_000), "to": str(now_ms + 60_000)}), PACK_ID
    )
    assert history.status == 200, _body(history)
    hbody = _body(history)
    assert (hbody["packId"], hbody["cells"]) == (PACK_ID, True)
    [point] = hbody["points"]
    assert point["soc"] == 81
    assert point["spread"] == max(point["c"]) - min(point["c"])

    bad = await BmsPackHistoryView().get(_ZReq(hass, query={"from": "5", "to": "1"}), PACK_ID)
    assert bad.status == 400

    today = received.astimezone(ZoneInfo(hass.config.time_zone)).date().isoformat()
    report = await BmsReportView().get(_ZReq(hass, query={"from": today, "to": today}))
    assert report.status == 200, _body(report)
    rbody = _body(report)
    assert (rbody["from"], rbody["to"]) == (today, today)
    [rpack, _unread] = rbody["packs"]
    assert rpack["packId"] == PACK_ID
    [day] = rpack["days"]
    assert (day["date"], day["live"], day["socEnd"]) == (today, True, 81)

    too_long = await BmsReportView().get(
        _ZReq(hass, query={"from": "2024-01-01", "to": "2026-01-01"})
    )
    assert too_long.status == 400
    assert _body(too_long)["error"] == "range_too_long"


async def test_bms_views_answer_no_edge_boxes_before_auth(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import (
        BmsPackHistoryView,
        BmsPacksView,
        BmsReportView,
    )

    await _bms_setup(hass, tmp_path, adopt=False)

    for resp in (
        await BmsPacksView().get(_ZReq(hass, island_key=None)),
        await BmsPackHistoryView().get(_ZReq(hass, island_key=None), PACK_ID),
        await BmsReportView().get(_ZReq(hass, island_key=None)),
    ):
        assert resp.status == 404
        assert _body(resp) == {"error": "no_edge_boxes"}


async def test_bms_views_need_the_island_key(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import BmsPacksView, BmsReportView

    await _bms_setup(hass, tmp_path)

    assert (await BmsPacksView().get(_ZReq(hass, island_key="wrong"))).status == 401
    assert (await BmsReportView().get(_ZReq(hass, island_key=None))).status == 401


# ── Battery pack roster and commands over the LAN (edge island plan, Task 45) ─


async def _roster_setup(hass, tmp_path):
    from custom_components.svitgrid.bms_packs import BmsPackStore
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    await _keystore(hass, admin)
    entry = _entry(hass)
    packs = BmsPackStore(str(tmp_path / "bms_packs.db"), None)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)["bms_packs"] = packs
    await _adopt(hass, admin, "EC-1", _inv("inv-1"))
    await _adopt(hass, admin, "EC-2", _inv("inv-2"))
    await packs.async_store_report("EC-1", _BMS_REPORT)
    return admin, packs, queue


async def _bms_command(hass, device_id, command, **payload):
    from custom_components.svitgrid.bms_lan import BmsCommandView

    return await BmsCommandView().post(
        _ZReq(hass, {"deviceId": device_id, "command": command, "payload": payload})
    )


async def _drain_box(queue, device_id="EC-1") -> list[dict]:
    rows = []
    while (row := await queue.async_next_pending(device_id)) is not None:
        rows.append(row)
        await queue.async_ack(row["id"], success=True, result=None, rejected=None, reason=None)
    return rows


async def test_adopting_a_pack_sends_bms_sync_with_the_next_generation(
    hass: HomeAssistant, tmp_path
):
    _, _, queue = await _roster_setup(hass, tmp_path)

    resp = await _bms_command(
        hass, "EC-1", "bms_adopt", packId="c0ffee000004", label="Комора", driver="daly"
    )

    assert resp.status == 200, _body(resp)
    [sync] = await _drain_box(queue)
    assert sync["id"] == _body(resp)["commandId"]
    assert sync["command"] == "bms_sync"
    assert sync["signing"] is None
    payload = sync["payload"]
    # The box held the cloud's epoch g-7f3a/3; the island's set is the next n
    # of it, so the H2 does not refuse it as older.
    assert (payload["generation"], payload["n"]) == ("g-7f3a", 4)
    assert payload["householdId"] == hass.config_entries.async_entries(DOMAIN)[0].entry_id
    ids = [p["packId"] for p in payload["packs"]]
    assert ids == [PACK_ID, "a4c138000002", "c0ffee000004"]
    adopted = payload["packs"][-1]
    assert adopted["driver"] == "daly"
    assert isinstance(adopted["adoptedAt"], int)
    # Labels stay in Home Assistant (the H2's field is 24 bytes).
    assert "label" not in adopted
    # A roster change outlives a box that is offline for a while.
    created = datetime.fromisoformat(sync["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(sync["expires_at"].replace("Z", "+00:00"))
    assert (expires - created).total_seconds() == 24 * 3600

    from custom_components.svitgrid.bms_lan import BmsPacksView

    listed = _body(await BmsPacksView().get(_ZReq(hass)))
    new = next(p for p in listed["packs"] if p["packId"] == "c0ffee000004")
    assert (new["assignedEdgeId"], new["label"], new["driver"]) == ("EC-1", "Комора", "daly")

    # A second change is the next n again, and supersedes an undelivered sync.
    await _bms_command(hass, "EC-1", "bms_rename", packId="c0ffee000004", label="Підвал")
    await _bms_command(hass, "EC-1", "bms_set_driver", packId="c0ffee000004", driver="jk_bd6a")
    await _bms_command(hass, "EC-1", "bms_move", packId="c0ffee000004", toDeviceId="EC-2")
    [last] = await _drain_box(queue, "EC-1")
    assert last["payload"]["n"] == 6
    assert "c0ffee000004" not in [p["packId"] for p in last["payload"]["packs"]]
    [moved] = await _drain_box(queue, "EC-2")
    [pack] = moved["payload"]["packs"]
    assert (pack["packId"], pack["driver"], pack["driverSource"]) == (
        "c0ffee000004",
        "jk_bd6a",
        "owner",
    )
    listed = _body(await BmsPacksView().get(_ZReq(hass)))
    new = next(p for p in listed["packs"] if p["packId"] == "c0ffee000004")
    assert (new["assignedEdgeId"], new["label"]) == ("EC-2", "Підвал")


async def test_a_pack_another_box_reads_is_not_adopted_twice(hass: HomeAssistant, tmp_path):
    _, _, queue = await _roster_setup(hass, tmp_path)

    resp = await _bms_command(hass, "EC-2", "bms_adopt", packId=PACK_ID)

    assert resp.status == 409
    assert _body(resp)["error"] == "PACK_READ_BY_OTHER_BOARD"
    assert await _drain_box(queue, "EC-2") == []


async def test_a_repeated_scan_is_deduplicated(hass: HomeAssistant, tmp_path):
    _, _, queue = await _roster_setup(hass, tmp_path)

    first = await _bms_command(hass, "EC-1", "bms_scan", durationS=20)
    second = await _bms_command(hass, "EC-1", "bms_scan", durationS=30)
    await _bms_command(hass, "EC-1", "bms_read_now")
    await _bms_command(hass, "EC-1", "bms_read_now")
    capture = await _bms_command(hass, "EC-1", "bms_capture", packId=PACK_ID)

    assert first.status == second.status == capture.status == 200
    superseded = await queue.async_get(_body(first)["commandId"])
    assert (superseded["state"], superseded["reason"]) == ("expired", "superseded")
    rows = await _drain_box(queue)
    assert [(r["command"], r["payload"]) for r in rows] == [
        ("bms_scan", {"durationS": 30}),
        ("bms_read_now", {}),
        ("bms_capture", {"packId": PACK_ID}),
    ]


async def test_a_member_cannot_forget_a_pack(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import BmsCommandView

    admin, _, queue = await _roster_setup(hass, tmp_path)
    member = _Admin()

    unsigned = await _bms_command(hass, "EC-1", "bms_forget", packId=PACK_ID)
    by_member = await BmsCommandView().post(
        _ZReq(
            hass,
            member.request(
                "edge_bms_command",
                deviceId="EC-1",
                command="bms_forget",
                payload={"packId": PACK_ID},
            ),
        )
    )
    assert (unsigned.status, _body(unsigned)) == (403, {"error": "owner_only"})
    assert by_member.status == 403
    assert await _drain_box(queue) == []

    by_owner = await BmsCommandView().post(
        _ZReq(
            hass,
            admin.request(
                "edge_bms_command",
                deviceId="EC-1",
                command="bms_forget",
                payload={"packId": PACK_ID},
            ),
        )
    )
    assert by_owner.status == 200, _body(by_owner)
    [sync] = await _drain_box(queue)
    assert [p["packId"] for p in sync["payload"]["packs"]] == ["a4c138000002"]


async def test_bms_commands_answer_no_edge_boxes_before_auth(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import BmsCommandView

    await _bms_setup(hass, tmp_path, adopt=False)

    resp = await BmsCommandView().post(_ZReq(hass, {"deviceId": "EC-1"}, island_key=None))
    assert (resp.status, _body(resp)) == (404, {"error": "no_edge_boxes"})


async def test_the_bms_command_view_is_registered(hass: HomeAssistant):
    from unittest.mock import MagicMock

    from custom_components.svitgrid.http_views import register_views

    hass.http = MagicMock()
    register_views(hass, store=MagicMock())
    urls = {call.args[0].url for call in hass.http.register_view.call_args_list}
    assert "/api/svitgrid/edge/bms/command" in urls


# ── Copying the cloud's Zigbee devices and pack labels in (plan Task 48) ──────


@pytest.fixture
def config_dir(hass, tmp_path):
    """Home Assistant's config folder, where the adopt opens the device store."""
    hass.config.config_dir = str(tmp_path / "config")
    os.makedirs(hass.config.config_dir)


def _seeded_store(hass):
    from custom_components.svitgrid.const import READINGS_DB_SUBDIR
    from custom_components.svitgrid.zigbee_devices import ZigbeeDeviceStore

    folder = hass.config.path(READINGS_DB_SUBDIR)
    os.makedirs(folder, exist_ok=True)
    return ZigbeeDeviceStore(os.path.join(folder, "zigbee_devices.db"), None)


_CLOUD_PLUG = {
    "ieee": PLUG_IEEE,
    "addr": "0x1234",
    "endpoint": 1,
    "name": "Бойлер",
    "room": "Ванна",
    "caps": 3,
    "model": "TS011F",
}


async def test_devices_are_in_home_assistant_before_the_box_switches(
    hass: HomeAssistant, config_dir
):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)

    resp = await _adopt(hass, admin, "EC-1", _inv("inv-1"), zigbeeDevices=[_CLOUD_PLUG])

    assert resp.status == 200, _body(resp)
    assert _body(resp)["zigbeeSkipped"] == []
    [device] = await _seeded_store(hass).async_list("EC-1")
    assert device["id"] == PLUG_ID
    assert (device["ieee"], device["addr"], device["endpoint"]) == (PLUG_IEEE, "0x1234", 1)
    assert (device["name"], device["room"], device["caps"], device["model"]) == (
        "Бойлер",
        "Ванна",
        3,
        "TS011F",
    )


async def test_a_seeded_device_rejoining_outside_a_window_is_not_kicked(
    hass: HomeAssistant, tmp_path, config_dir
):
    from custom_components.svitgrid.edge_api import EdgeZigbeeDeviceJoinedView
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

    admin = _Admin()
    ks = await _keystore(hass, admin)
    entry = _entry(hass)
    await _adopt(hass, admin, "EC-1", _inv("inv-1"), zigbeeDevices=[_CLOUD_PLUG])
    await ks.async_bind_edge_box("EC-1", "box-api-key")
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    _entry_state(hass, entry, queue=queue)["zigbee_devices"] = _seeded_store(hass)

    req = _Req(hass, {"addr": "0x9999", "ieee": PLUG_IEEE, "endpoint": 1}, island_key=None)
    req.headers = {"X-API-Key": "box-api-key"}
    resp = await EdgeZigbeeDeviceJoinedView().post(req)

    assert resp.status == 200, resp.body
    assert await queue.async_pending("EC-1", "remove_device") == []


async def test_an_adopt_without_zigbee_devices_is_unchanged(hass: HomeAssistant, config_dir):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)

    resp = await _adopt(hass, admin, "EC-1", _inv("inv-1"))

    assert resp.status == 200
    assert set(_body(resp)) == {"ok", "pairCode", "expiresAt"}
    assert await _seeded_store(hass).async_list() == []


async def test_a_retried_adopt_does_not_duplicate_a_device(hass: HomeAssistant, config_dir):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)

    await _adopt(hass, admin, "EC-1", _inv("inv-1"), zigbeeDevices=[_CLOUD_PLUG])
    resp = await _adopt(
        hass, admin, "EC-1", _inv("inv-1"), zigbeeDevices=[{**_CLOUD_PLUG, "name": "Бойлер 2"}]
    )

    assert resp.status == 200, _body(resp)
    [device] = await _seeded_store(hass).async_list()
    assert (device["deviceId"], device["name"]) == ("EC-1", "Бойлер 2")


async def test_malformed_zigbee_devices_are_skipped_and_named(hass: HomeAssistant, config_dir):
    admin = _Admin()
    await _keystore(hass, admin)
    _entry(hass)

    resp = await _adopt(
        hass,
        admin,
        "EC-1",
        _inv("inv-1"),
        zigbeeDevices=[{"ieee": "00:11", "addr": 7}, "plug", _CLOUD_PLUG],
    )

    assert resp.status == 200, _body(resp)
    assert _body(resp)["zigbeeSkipped"] == [{"index": 0, "ieee": "00:11"}, {"index": 1}]
    assert [d["id"] for d in await _seeded_store(hass).async_list("EC-1")] == [PLUG_ID]


async def _bms_import(hass, signer, device_id, packs, *, signed=True, **req):
    from custom_components.svitgrid.bms_lan import BmsImportView

    body = {"deviceId": device_id, "packs": packs}
    if signed:
        body = signer.request("bms_import", **body)
    return await BmsImportView().post(_ZReq(hass, body, **req))


async def test_bms_import_sets_label_and_driver(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid.bms_lan import BmsPacksView

    admin, _, queue = await _roster_setup(hass, tmp_path)

    resp = await _bms_import(
        hass,
        admin,
        "EC-1",
        [
            {"packId": "a4c138000002", "label": "Підвал", "driver": "daly"},
            {"packId": "c0ffee000009", "label": "Ще не бачив"},
        ],
    )

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {
        "imported": ["a4c138000002"],
        "pending": ["c0ffee000009"],
        "skipped": [],
    }
    listed = _body(await BmsPacksView().get(_ZReq(hass)))
    pack = next(p for p in listed["packs"] if p["packId"] == "a4c138000002")
    assert (pack["label"], pack["driver"], pack["driverSource"]) == ("Підвал", "daly", "owner")
    # The box already reads its packs: an import sends it nothing.
    assert await _drain_box(queue) == []

    # The first roster change keeps what was imported.
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000004")
    [sync] = await _drain_box(queue)
    imported = next(p for p in sync["payload"]["packs"] if p["packId"] == "a4c138000002")
    assert (imported["driver"], imported["driverSource"]) == ("daly", "owner")
    listed = _body(await BmsPacksView().get(_ZReq(hass)))
    pack = next(p for p in listed["packs"] if p["packId"] == "a4c138000002")
    assert pack["label"] == "Підвал"


def _report_with(*pack_ids):
    extra = [{**_BMS_REPORT["packs"][1], "id": pack_id} for pack_id in pack_ids]
    return {**_BMS_REPORT, "packs": [*_BMS_REPORT["packs"], *extra]}


async def test_an_import_for_an_unreported_pack_applies_when_the_pack_appears(
    hass: HomeAssistant, tmp_path
):
    from custom_components.svitgrid.bms_lan import BmsPacksView

    admin, packs, _ = await _roster_setup(hass, tmp_path)

    resp = await _bms_import(
        hass, admin, "EC-1", [{"packId": "c0ffee000009", "label": "Льох", "driver": "daly"}]
    )

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"imported": [], "pending": ["c0ffee000009"], "skipped": []}
    # Another box reporting the pack does not take the copy meant for EC-1.
    await packs.async_store_report("EC-2", _report_with("c0ffee000009"))
    assert "c0ffee000009" not in await packs.async_imported()
    await packs.async_store_report("EC-2", _BMS_REPORT)

    await packs.async_store_report("EC-1", _report_with("c0ffee000009"))

    listed = _body(await BmsPacksView().get(_ZReq(hass)))
    pack = next(p for p in listed["packs"] if p["packId"] == "c0ffee000009")
    assert (pack["assignedEdgeId"], pack["label"], pack["driver"], pack["driverSource"]) == (
        "EC-1",
        "Льох",
        "daly",
        "owner",
    )


async def test_a_pending_import_never_creates_a_roster(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid import bms_roster

    admin, packs, queue = await _roster_setup(hass, tmp_path)

    await _bms_import(hass, admin, "EC-1", [{"packId": "c0ffee000009", "driver": "daly"}])
    report = _report_with("c0ffee000009")
    await packs.async_store_report("EC-1", report)

    assert await packs.async_rosters() == {}
    assert (
        await bms_roster.async_converge(packs, queue, None, "EC-1", report["rosterEpoch"]) is None
    )
    assert await _drain_box(queue) == []


async def test_a_pending_copy_reaches_a_managed_box_roster(hass: HomeAssistant, tmp_path):
    admin, packs, queue = await _roster_setup(hass, tmp_path)
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000009")
    await _bms_import(hass, admin, "EC-1", [{"packId": "c0ffee000004", "driver": "daly"}])
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000004")
    n = (await packs.async_roster("EC-1"))["n"]

    await packs.async_store_report("EC-1", _report_with("c0ffee000004"))

    roster = await packs.async_roster("EC-1")
    entry = next(p for p in roster["packs"] if p["packId"] == "c0ffee000004")
    assert (entry["driver"], entry["driverSource"]) == ("daly", "owner")
    # A new driver is a new epoch, which the report's converge sends.
    assert roster["n"] == n + 1


async def test_a_direct_import_drops_an_older_pending_copy(hass: HomeAssistant, tmp_path):
    admin, packs, _ = await _roster_setup(hass, tmp_path)
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000009")
    await _bms_import(hass, admin, "EC-1", [{"packId": "c0ffee000004", "label": "Старе"}])
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000004")
    await _bms_import(hass, admin, "EC-1", [{"packId": "c0ffee000004", "label": "Нове"}])

    await packs.async_store_report("EC-1", _report_with("c0ffee000004"))

    roster = await packs.async_roster("EC-1")
    assert next(p for p in roster["packs"] if p["packId"] == "c0ffee000004")["label"] == "Нове"


async def test_an_app_that_ignores_pending_still_reads_imported_and_skipped(
    hass: HomeAssistant, tmp_path
):
    admin, _, _ = await _roster_setup(hass, tmp_path)

    resp = await _bms_import(
        hass,
        admin,
        "EC-1",
        [{"packId": PACK_ID, "label": "Гараж 2"}, {"packId": "c0ffee000009"}, {"packId": 7}],
    )

    body = _body(resp)
    assert body["imported"] == [PACK_ID]
    assert body["skipped"] == [{"reason": "bad_request"}]
    assert body["pending"] == ["c0ffee000009"]


async def test_bms_import_never_creates_a_roster(hass: HomeAssistant, tmp_path):
    from custom_components.svitgrid import bms_roster

    admin, packs, queue = await _roster_setup(hass, tmp_path)

    resp = await _bms_import(hass, admin, "EC-1", [{"packId": PACK_ID, "label": "Комора"}])

    assert resp.status == 200, _body(resp)
    assert await packs.async_rosters() == {}
    # So the box's reports still leave it alone.
    epoch = {"generation": "g-old", "n": 1}
    assert await bms_roster.async_converge(packs, queue, None, "EC-1", epoch) is None


async def test_bms_import_on_a_managed_box_changes_its_roster(hass: HomeAssistant, tmp_path):
    admin, packs, queue = await _roster_setup(hass, tmp_path)
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000004")
    await _drain_box(queue)

    resp = await _bms_import(
        hass, admin, "EC-1", [{"packId": "c0ffee000004", "label": "Комора", "driver": "daly"}]
    )

    assert _body(resp)["imported"] == ["c0ffee000004"]
    roster = await packs.async_roster("EC-1")
    entry = next(p for p in roster["packs"] if p["packId"] == "c0ffee000004")
    assert (entry["label"], entry["driver"], entry["driverSource"]) == ("Комора", "daly", "owner")
    [sync] = await _drain_box(queue)
    assert sync["payload"]["n"] == roster["n"]


async def test_bms_import_refuses_a_viewer(hass: HomeAssistant, tmp_path):
    admin, packs, _ = await _roster_setup(hass, tmp_path)

    unsigned = await _bms_import(hass, admin, "EC-1", [{"packId": PACK_ID}], signed=False)
    no_key = await _bms_import(hass, admin, "EC-1", [{"packId": PACK_ID}], island_key=None)
    stranger = await _bms_import(hass, _Admin(), "EC-1", [{"packId": PACK_ID, "label": "x"}])

    assert (unsigned.status, _body(unsigned)["error"]) == (403, "owner_only")
    assert no_key.status == 401
    assert (stranger.status, _body(stranger)["error"]) == (403, "signature_invalid")
    listed = (await packs.async_state("EC-1"))["packs"]
    assert next(p for p in listed if p["id"] == PACK_ID)["label"] == "Гараж"


async def test_bms_import_without_an_adopted_box_is_404(hass: HomeAssistant, tmp_path):
    admin = _Admin()
    await _bms_setup(hass, tmp_path, adopt=False)

    resp = await _bms_import(hass, admin, "EC-1", [], island_key=None)

    assert (resp.status, _body(resp)["error"]) == (404, "no_edge_boxes")


async def test_bms_import_of_labels_alone_sends_a_managed_box_nothing(
    hass: HomeAssistant, tmp_path
):
    admin, packs, queue = await _roster_setup(hass, tmp_path)
    await _bms_command(hass, "EC-1", "bms_adopt", packId="c0ffee000004")
    await _drain_box(queue)

    resp = await _bms_import(hass, admin, "EC-1", [{"packId": "c0ffee000004", "label": "Комора"}])

    assert _body(resp)["imported"] == ["c0ffee000004"]
    assert await _drain_box(queue) == []
