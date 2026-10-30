"""Tests for the signing key fingerprint, `compute_key_id`.

Both `POST /api/svitgrid/trust-key` and `DELETE /api/svitgrid/trust-key/{id}`
need an already-trusted admin key since 2026-10-16; their tests are in
test_trust_key_needs_control.py.

`signing.py` is loaded by file path, as other test files do.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from unittest.mock import patch

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import SvitgridTrustKeyView
from tests.test_island_lan import NOW_MS, _Admin, _body, _leave_setup, _Req

BASE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "svitgrid")


def _load(mod_name: str, path: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


signing = _load("svitgrid_signing", os.path.join(BASE, "signing.py"))


def _make_key():
    priv, pub_hex = signing.generate_keypair()
    key_id = signing.compute_key_id(pub_hex)
    return priv, pub_hex, key_id


def test_compute_key_id_matches_sha256_of_point():
    _, pub_hex, key_id = _make_key()
    assert key_id == hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()


def test_compute_key_id_golden_vector_matches_mobile():
    # Real (publicKeyHex -> keyId) pair produced by the mobile app
    # (CommandSigner.computeKeyId). Pins the Python fingerprint byte-for-byte
    # to the mobile one; a divergence would silently 400 all LAN trust
    # provisioning, so lock it here. The mobile CommandSigner test MUST assert
    # this identical pair (add there when Phase 2 lands).
    public_key_hex = (
        "04c91df558ded295a6bf71a7a845c7432ef05cc399208353b6087870776b4ee5"
        "ebee40b4332f8b8d0bd48312b339c5f5182232ae0428cab23d3345bcaba506930b"
    )
    expected_key_id = "843f39e4beba1b7faefca63aaf2ce743de4fdc3d24c78107de115b2471aab1aa"
    assert signing.compute_key_id(public_key_hex) == expected_key_id


# ── a key trusted while a box is adopted waits for the box (Task 17) ──────


async def test_trust_key_with_adopted_box_marks_pending(hass, tmp_path):
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        admin, queue = await _leave_setup(hass, tmp_path)
        phone = _Admin()

        resp = await SvitgridTrustKeyView().post(
            _Req(hass, admin.request("trust_key", keyId=phone.key_id, publicKeyHex=phone.pub))
        )

    assert resp.status == 200, _body(resp)
    ks = hass.data[DOMAIN]["keystore"]
    state = await ks.load()
    assert phone.key_id in state.trusted_public_keys_hex
    assert sorted(state.box_pending_keys[phone.key_id]) == ["EC-1", "EC-2"]
    # Trusting puts nothing on the queue: only a phone the box trusts can.
    assert await queue.async_pending_count("EC-1") == 0
