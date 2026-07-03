"""TDD tests for POST /api/svitgrid/trust-key (Task 1 — island LAN trust
provisioning).

Written BEFORE implementation (RED phase). Covers:
- compute_key_id matches sha256(bytes.fromhex(public_key_hex)).hexdigest()
- valid island key + self-signed payload + matching keyId → 200, key merged
- missing/invalid island key → 401, keystore not updated
- self-signature by the WRONG key → 403, keystore not updated
- signingKeyId that doesn't match the fingerprint of publicKeyHex → 400

Import note: importing `custom_components.svitgrid.http_views` normally
triggers `custom_components/svitgrid/__init__.py` -> `.panel` ->
`homeassistant.components.http.StaticPathConfig`, which doesn't exist on
this env's pinned homeassistant (pre-existing, documented collection
failure — see other test files' `_load_views` helpers). So both
`signing.py` and `http_views.py` are loaded here by file path via
importlib, with http_views's sibling deps pre-registered in
`sys.modules` under their expected dotted names so its `from .xxx import
yyy` relative imports resolve without importing the real package
`__init__.py`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

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


class _Req:
    def __init__(self, headers, body, hass):
        self.headers = headers
        self._body = body
        self.app = {"hass": hass}

    async def json(self):
        return self._body


def _hass_with(island_key, trusted):
    ks = MagicMock()
    ks.async_get_island_key = AsyncMock(return_value=island_key)
    state = MagicMock()
    state.trusted_public_keys_hex = dict(trusted)
    ks.load = AsyncMock(return_value=state)
    ks.update_trusted_keys_hex = AsyncMock()
    hass = MagicMock()
    hass.data = {"svitgrid": {"keystore": ks}}
    return hass, ks


def _load_views():
    """Load http_views.py, working around the panel-import collection failure.

    Try the plain package import first (works if the environment's HA
    package happens to be compatible); fall back to file-path loading with
    sibling modules pre-injected into sys.modules under
    `custom_components.svitgrid.<name>` so `from .signing import ...` etc.
    resolve without executing the real (broken-in-this-env) package
    `__init__.py`.
    """
    try:
        import custom_components.svitgrid.http_views as hv

        return hv
    except ImportError:
        pass

    pkg_name = "custom_components.svitgrid"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [BASE]
        sys.modules["custom_components"] = types.ModuleType("custom_components")
        sys.modules["custom_components"].__path__ = [os.path.join(BASE, "..")]
        sys.modules[pkg_name] = pkg

    for name in ("signing", "const", "command_auth", "hourly_energy", "island_auth"):
        full_name = f"{pkg_name}.{name}"
        if full_name not in sys.modules:
            sys.modules[full_name] = _load(full_name, os.path.join(BASE, f"{name}.py"))

    return _load(f"{pkg_name}.http_views", os.path.join(BASE, "http_views.py"))


@pytest.mark.asyncio
async def test_post_adds_self_signed_key():
    from aiohttp import web  # noqa: F401

    http_views = _load_views()
    priv, pub_hex, key_id = _make_key()
    sig = signing.sign_payload({"signingKeyId": key_id, "publicKeyHex": pub_hex}, priv)
    hass, ks = _hass_with("island-abc", {})
    view = http_views.SvitgridTrustKeyView()
    req = _Req(
        {"X-Island-Key": "island-abc"},
        {"signingKeyId": key_id, "publicKeyHex": pub_hex, "signature": sig},
        hass,
    )
    resp = await view.post(req)
    assert resp.status == 200
    ks.update_trusted_keys_hex.assert_awaited_once()
    saved = ks.update_trusted_keys_hex.await_args.args[0]
    assert saved[key_id] == pub_hex


@pytest.mark.asyncio
async def test_post_rejects_without_island_key():
    http_views = _load_views()
    priv, pub_hex, key_id = _make_key()
    sig = signing.sign_payload({"signingKeyId": key_id, "publicKeyHex": pub_hex}, priv)
    hass, ks = _hass_with("island-abc", {})
    view = http_views.SvitgridTrustKeyView()
    req = _Req({}, {"signingKeyId": key_id, "publicKeyHex": pub_hex, "signature": sig}, hass)
    resp = await view.post(req)
    assert resp.status == 401
    ks.update_trusted_keys_hex.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_rejects_bad_self_signature():
    http_views = _load_views()
    priv, pub_hex, key_id = _make_key()
    other_priv, _, _ = _make_key()
    bad = signing.sign_payload({"signingKeyId": key_id, "publicKeyHex": pub_hex}, other_priv)
    hass, ks = _hass_with("island-abc", {})
    view = http_views.SvitgridTrustKeyView()
    req = _Req(
        {"X-Island-Key": "island-abc"},
        {"signingKeyId": key_id, "publicKeyHex": pub_hex, "signature": bad},
        hass,
    )
    resp = await view.post(req)
    assert resp.status == 403
    ks.update_trusted_keys_hex.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_rejects_keyid_mismatch():
    http_views = _load_views()
    priv, pub_hex, key_id = _make_key()
    sig = signing.sign_payload({"signingKeyId": "deadbeef", "publicKeyHex": pub_hex}, priv)
    hass, ks = _hass_with("island-abc", {})
    view = http_views.SvitgridTrustKeyView()
    req = _Req(
        {"X-Island-Key": "island-abc"},
        {"signingKeyId": "deadbeef", "publicKeyHex": pub_hex, "signature": sig},
        hass,
    )
    resp = await view.post(req)
    assert resp.status == 400
    ks.update_trusted_keys_hex.assert_not_awaited()
