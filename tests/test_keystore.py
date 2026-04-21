"""Round-trip keypair + API key through HA's Store. Uses the HA-in-process
fixture from pytest-homeassistant-custom-component."""

from __future__ import annotations

import pytest

from custom_components.svitgrid.keystore import SvitgridKeystore


@pytest.mark.asyncio
async def test_absent_returns_none(hass):
    ks = SvitgridKeystore(hass)
    assert await ks.load() is None


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair, public_key_to_hex

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="test-api-key",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="key-1",
        trusted_key_ids=["key-1"],
    )

    loaded = await ks.load()
    assert loaded is not None
    assert loaded.api_key == "test-api-key"
    assert loaded.public_key_hex == pub_hex
    assert loaded.signing_key_id == "key-1"
    assert loaded.trusted_key_ids == ["key-1"]
    # Private key can be re-constructed
    reloaded_priv = loaded.load_private_key()
    assert public_key_to_hex(reloaded_priv.public_key()) == pub_hex


@pytest.mark.asyncio
async def test_update_trusted_keys(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="s",
        trusted_key_ids=["a"],
    )
    await ks.update_trusted_keys(["a", "b"])
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_key_ids == ["a", "b"]


def _pem(priv):
    """Helper: serialize a private key to PEM bytes for testing."""
    from cryptography.hazmat.primitives import serialization

    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.mark.asyncio
async def test_trusted_public_keys_hex_roundtrip(hass):
    """KeystoreState gains a trusted_public_keys_hex dict that round-trips
    through HA Store alongside the existing trusted_key_ids list."""
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk",
        trusted_key_ids=["admin-1", "admin-2"],
        trusted_public_keys_hex={"admin-1": "04" + "aa" * 64, "admin-2": "04" + "bb" * 64},
    )
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_public_keys_hex == {
        "admin-1": "04" + "aa" * 64,
        "admin-2": "04" + "bb" * 64,
    }


@pytest.mark.asyncio
async def test_trusted_public_keys_hex_defaults_empty_when_missing(hass):
    """Legacy stored state (from v0.1.0) doesn't have trusted_public_keys_hex.
    Loading it should default to an empty dict, not raise."""
    from homeassistant.helpers.storage import Store

    from custom_components.svitgrid.const import STORAGE_KEY, STORAGE_VERSION

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    await store.async_save(
        {
            "api_key": "legacy-key",
            "public_key_hex": "04" + "cc" * 64,
            "private_key_pem": "dummy-pem",
            "signing_key_id": "legacy-sk",
            "trusted_key_ids": ["a", "b"],
            # NOTE: no trusted_public_keys_hex field
        }
    )

    ks = SvitgridKeystore(hass)
    loaded = await ks.load()
    assert loaded is not None
    assert loaded.trusted_public_keys_hex == {}


@pytest.mark.asyncio
async def test_update_trusted_keys_hex_replaces_atomically(hass):
    ks = SvitgridKeystore(hass)
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=_pem(priv),
        signing_key_id="sk",
        trusted_key_ids=["a"],
        trusted_public_keys_hex={"a": "04" + "11" * 64},
    )
    await ks.update_trusted_keys_hex({"b": "04" + "22" * 64, "c": "04" + "33" * 64})
    loaded = await ks.load()
    assert loaded.trusted_public_keys_hex == {"b": "04" + "22" * 64, "c": "04" + "33" * 64}
    # Keys stay in sync with the ids list
    assert sorted(loaded.trusted_key_ids) == ["b", "c"]
