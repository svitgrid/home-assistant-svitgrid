"""Tests for the per-household island key stored in SvitgridKeystore."""

from __future__ import annotations

import pytest

from custom_components.svitgrid.keystore import SvitgridKeystore, generate_island_key

# ---------------------------------------------------------------------------
# generate_island_key
# ---------------------------------------------------------------------------


def test_generate_island_key_is_url_safe_and_long_enough():
    key = generate_island_key()
    assert len(key) >= 32
    # URL-safe base64 characters only (no +, /, or =)
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert all(c in allowed for c in key), f"Non-URL-safe chars in key: {key!r}"


def test_generate_island_key_differs_across_calls():
    assert generate_island_key() != generate_island_key()


# ---------------------------------------------------------------------------
# async_get_island_key / async_set_island_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_keystore_returns_none(hass):
    ks = SvitgridKeystore(hass)
    assert await ks.async_get_island_key() is None


@pytest.mark.asyncio
async def test_set_then_get_roundtrip(hass):
    ks = SvitgridKeystore(hass)
    # Prime the store with the mandatory fields first (save requires them).
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="api-key-x",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk-1",
        trusted_key_ids=["sk-1"],
    )

    await ks.async_set_island_key("my-island-key")
    assert await ks.async_get_island_key() == "my-island-key"


@pytest.mark.asyncio
async def test_set_island_key_does_not_clobber_other_fields(hass):
    """Setting the island key must leave api_key, trusted_key_ids, and
    trusted_public_keys_hex untouched."""
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="original-api-key",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk-2",
        trusted_key_ids=["sk-2", "sk-3"],
        trusted_public_keys_hex={"sk-2": "04" + "ab" * 64, "sk-3": "04" + "cd" * 64},
    )

    await ks.async_set_island_key("island-key-zzz")

    loaded = await ks.load()
    assert loaded is not None
    assert loaded.api_key == "original-api-key"
    assert loaded.trusted_key_ids == ["sk-2", "sk-3"]
    assert loaded.trusted_public_keys_hex == {
        "sk-2": "04" + "ab" * 64,
        "sk-3": "04" + "cd" * 64,
    }
    assert loaded.island_key == "island-key-zzz"


@pytest.mark.asyncio
async def test_island_key_absent_in_legacy_blob_loads_none(hass):
    """Old stored state without island_key → load() gives island_key=None."""
    from homeassistant.helpers.storage import Store

    from custom_components.svitgrid.const import STORAGE_KEY, STORAGE_VERSION

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(
        {
            "api_key": "legacy-api-key",
            "public_key_hex": "04" + "ee" * 64,
            "private_key_pem": "dummy-pem",
            "signing_key_id": "legacy-sk",
            "trusted_key_ids": ["x"],
            "trusted_public_keys_hex": {},
            # NOTE: no island_key field
        }
    )

    ks = SvitgridKeystore(hass)
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.island_key is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pem(priv) -> str:
    from cryptography.hazmat.primitives import serialization

    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
