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
