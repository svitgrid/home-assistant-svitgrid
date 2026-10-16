"""`POST /api/svitgrid/trust-key` needs an already-trusted admin key (2026-10-16).

The endpoint used to accept any phone that held an island key and proved it
held the signing key it asked to trust. A view-only phone holds an island key,
so it could trust its own signing key and give itself control. The request is
now a signed write, like `POST /api/svitgrid/keys/revoke`: an already-trusted
admin key signs `signedData = {action: "trust_key", keyId, publicKeyHex,
nonce, ts}`. LAN pairing (`pair-local`) stays the only path that trusts a
first key, and it has the owner's approval in Home Assistant behind it.

Contract: true-island-contract.md, "Hardening (added 2026-10-16)".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridTrustKeyDetailView, SvitgridTrustKeyView
from custom_components.svitgrid.island_lan import SvitgridKeysRevokeView
from custom_components.svitgrid.signing import sign_payload
from tests.test_extra_phone_pairing import (
    OWNER_KEY,
    SECOND_ISLAND_KEY,
    _add,
    _phone_body,
)
from tests.test_extra_phone_pairing import (
    _station as _paired_station,
)
from tests.test_island_lan import ISLAND_KEY, NOW_MS, _Admin, _body, _keystore, _Req
from tests.test_local_pairing import _cloud, _code, _start_flow


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


def _trust_request(signer: _Admin, new: _Admin, **overrides) -> dict:
    fields = {"keyId": new.key_id, "publicKeyHex": new.pub, **overrides}
    return signer.request("trust_key", **fields)


async def _trust(hass, body: dict, *, island_key: str = ISLAND_KEY):
    return await SvitgridTrustKeyView().post(_Req(hass, body, island_key=island_key))


# ── a control key can trust another key ─────────────────────────────────────


async def test_a_trusted_admin_key_can_trust_another_key(hass: HomeAssistant):
    admin, new = _Admin(), _Admin()
    ks = await _keystore(hass, admin)

    resp = await _trust(hass, _trust_request(admin, new))

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "trustedKeyIds": sorted([admin.key_id, new.key_id])}
    state = await ks.load()
    assert state.trusted_public_keys_hex[new.key_id] == new.pub


async def test_a_trusted_key_reaches_the_running_poller(hass: HomeAssistant):
    """The keystore's trust listeners hear the write, as they do for revoke."""
    admin, new = _Admin(), _Admin()
    ks = await _keystore(hass, admin)
    seen: list[dict] = []
    ks.subscribe_trust(seen.append)

    assert (await _trust(hass, _trust_request(admin, new))).status == 200

    assert new.key_id in seen[-1]


# ── nobody without control can ──────────────────────────────────────────────


async def test_a_key_cannot_trust_itself(hass: HomeAssistant):
    """The self-signed request the endpoint used to accept."""
    admin, phone = _Admin(), _Admin()
    ks = await _keystore(hass, admin)

    resp = await _trust(hass, _trust_request(phone, phone))

    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_the_old_self_signed_body_trusts_nothing(hass: HomeAssistant):
    admin, phone = _Admin(), _Admin()
    ks = await _keystore(hass, admin)
    old_body = {
        "signingKeyId": phone.key_id,
        "publicKeyHex": phone.pub,
        "signature": sign_payload(
            {"signingKeyId": phone.key_id, "publicKeyHex": phone.pub}, phone.priv
        ),
    }

    resp = await _trust(hass, old_body)

    assert resp.status in (400, 403)
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_a_view_only_phone_cannot_give_itself_control(
    hass: HomeAssistant, enable_custom_integrations
):
    """End to end: the owner approves a second phone for viewing only, and
    that phone asks `/trust-key` for its own key with its own island key."""
    await _paired_station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, _ = await _add(hass, result["flow_id"], body, "pair_approve_view")
    assert _body(resp)["role"] == "view"

    viewer = _Admin.__new__(_Admin)
    viewer.priv, viewer.pub, viewer.key_id = (
        body["_priv"],
        body["publicKeyHex"],
        body["signingKeyId"],
    )
    resp = await _trust(hass, _trust_request(viewer, viewer), island_key=SECOND_ISLAND_KEY)

    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}
    state = await hass.data[DOMAIN]["keystore"].load()
    assert viewer.key_id not in state.trusted_public_keys_hex


async def test_without_an_island_key_it_is_401(hass: HomeAssistant):
    admin, new = _Admin(), _Admin()
    await _keystore(hass, admin)

    resp = await _trust(hass, _trust_request(admin, new), island_key="w" * 43)

    assert resp.status == 401


# ── the signed-write rules ──────────────────────────────────────────────────


async def test_a_request_signed_for_another_endpoint_is_refused(hass: HomeAssistant):
    admin, new = _Admin(), _Admin()
    ks = await _keystore(hass, admin)

    body = admin.request("revoke_key", keyId=new.key_id, publicKeyHex=new.pub)
    resp = await _trust(hass, body)

    assert resp.status == 400
    assert new.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_a_replayed_request_is_refused(hass: HomeAssistant):
    admin, new = _Admin(), _Admin()
    ks = await _keystore(hass, admin)
    body = _trust_request(admin, new)
    assert (await _trust(hass, body)).status == 200
    revoked = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=new.key_id))
    )
    assert revoked.status == 200

    resp = await _trust(hass, body)

    assert resp.status == 403
    assert _body(resp) == {"error": "replayed"}
    assert new.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_a_key_id_that_is_not_the_fingerprint_is_refused(hass: HomeAssistant):
    admin, new, other = _Admin(), _Admin(), _Admin()
    ks = await _keystore(hass, admin)

    resp = await _trust(hass, _trust_request(admin, new, keyId=other.key_id))

    assert resp.status == 400
    assert _body(resp)["error"] == "key_id_mismatch"
    assert set((await ks.load()).trusted_public_keys_hex) == {admin.key_id}


async def test_a_malformed_public_key_is_refused(hass: HomeAssistant):
    admin, new = _Admin(), _Admin()
    await _keystore(hass, admin)

    resp = await _trust(hass, _trust_request(admin, new, publicKeyHex="04zz"))

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_public_key"


async def test_a_missing_key_is_bad_request(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)

    resp = await _trust(hass, admin.request("trust_key"))

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"


async def test_a_key_revoked_over_the_lan_is_still_409(hass: HomeAssistant):
    admin, phone = _Admin(), _Admin()
    ks = await _keystore(hass, admin, phone)
    revoked = await SvitgridKeysRevokeView().post(
        _Req(hass, admin.request("revoke_key", keyId=phone.key_id))
    )
    assert revoked.status == 200

    resp = await _trust(hass, _trust_request(admin, phone))

    assert resp.status == 409
    assert _body(resp) == {"error": "key_revoked"}
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


# ── DELETE /trust-key/{id} needs control too ────────────────────────────────
#
# It needed only an island key, so a view-only phone could untrust any key,
# the last admin key included. It is now an alias of `POST /keys/revoke`:
# the same signed body (`action: "revoke_key"`, `keyId` equal to the path),
# the same `last_admin_key` guard, and the same local denylist.


async def _delete(hass, key_id: str, body: dict | None, *, island_key: str = ISLAND_KEY):
    return await SvitgridTrustKeyDetailView().delete(
        _Req(hass, body, island_key=island_key), key_id
    )


async def test_delete_signed_by_an_admin_untrusts_and_denylists(hass: HomeAssistant):
    admin, other = _Admin(), _Admin()
    ks = await _keystore(hass, admin, other)

    resp = await _delete(hass, other.key_id, admin.request("revoke_key", keyId=other.key_id))

    assert resp.status == 200, _body(resp)
    assert _body(resp) == {"ok": True, "revoked": True, "trustedKeyIds": [admin.key_id]}
    state = await ks.load()
    assert other.key_id not in state.trusted_public_keys_hex
    assert other.key_id in state.revoked_key_ids


async def test_delete_with_only_an_island_key_is_refused(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)

    resp = await _delete(hass, admin.key_id, None)

    assert resp.status == 400
    assert admin.key_id in (await ks.load()).trusted_public_keys_hex


async def test_a_view_only_phone_cannot_delete_a_key(
    hass: HomeAssistant, enable_custom_integrations
):
    await _paired_station(hass, local_only=False)
    with _cloud(reachable=False):
        result = await _start_flow(hass)
        body = _phone_body(_code(hass))
        resp, _ = await _add(hass, result["flow_id"], body, "pair_approve_view")
    assert _body(resp)["role"] == "view"
    viewer = _Admin.__new__(_Admin)
    viewer.priv, viewer.pub, viewer.key_id = (
        body["_priv"],
        body["publicKeyHex"],
        body["signingKeyId"],
    )
    owner_key_id = OWNER_KEY[0]

    resp = await _delete(
        hass,
        owner_key_id,
        viewer.request("revoke_key", keyId=owner_key_id),
        island_key=SECOND_ISLAND_KEY,
    )

    assert resp.status == 403
    assert _body(resp) == {"error": "signature_invalid"}
    state = await hass.data[DOMAIN]["keystore"].load()
    assert owner_key_id in state.trusted_public_keys_hex
    assert owner_key_id not in state.revoked_key_ids


async def test_delete_keeps_the_last_admin_key(hass: HomeAssistant):
    admin = _Admin()
    ks = await _keystore(hass, admin)

    resp = await _delete(hass, admin.key_id, admin.request("revoke_key", keyId=admin.key_id))

    assert resp.status == 409
    assert _body(resp) == {"error": "last_admin_key"}
    state = await ks.load()
    assert admin.key_id in state.trusted_public_keys_hex
    assert state.revoked_key_ids == []


async def test_delete_with_a_key_id_other_than_the_path_is_refused(hass: HomeAssistant):
    admin, other, third = _Admin(), _Admin(), _Admin()
    ks = await _keystore(hass, admin, other, third)

    resp = await _delete(hass, other.key_id, admin.request("revoke_key", keyId=third.key_id))

    assert resp.status == 400
    assert set((await ks.load()).trusted_public_keys_hex) == {
        admin.key_id,
        other.key_id,
        third.key_id,
    }


async def test_delete_of_an_unknown_key_is_ok_and_revokes_nothing(hass: HomeAssistant):
    admin = _Admin()
    await _keystore(hass, admin)

    resp = await _delete(hass, "nope", admin.request("revoke_key", keyId="nope"))

    assert resp.status == 200
    assert _body(resp)["revoked"] is False
