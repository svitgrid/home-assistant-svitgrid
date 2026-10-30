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
