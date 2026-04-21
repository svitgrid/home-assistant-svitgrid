"""Command poller processes one polled command: verifies its signature
against the local trusted-keys cache, then signs + POSTs a rejection ACK."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.keystore import KeystoreState
from custom_components.svitgrid.signing import (
    generate_keypair,
    sign_payload,
    verify_payload,
)


def _make_keystore_state(priv, pub_hex, trusted_hex_by_id):
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return KeystoreState(
        api_key="our-api-key",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="our-key",
        trusted_key_ids=list(trusted_hex_by_id.keys()),
        trusted_public_keys_hex=dict(trusted_hex_by_id),
    )


@pytest.mark.asyncio
async def test_rejects_with_signed_ack_when_signature_valid():
    """Admin key known + valid signature → send signed rejection ACK."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-1", "command": "set_battery_charge"}
    admin_sig = sign_payload(signed_event_data, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "payload": {},
        "signature": admin_sig,
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }

    trusted_by_id = {"admin-key-id": admin_pub_hex}
    state = _make_keystore_state(our_priv, our_pub_hex, trusted_by_id)

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key=state.api_key,
        trusted_public_keys_hex=trusted_by_id,
        our_private_key=our_priv,
        our_signing_key_id=state.signing_key_id,
        executor_version="0.1.0",
    )

    api_client.ack_command.assert_called_once()
    kwargs = api_client.ack_command.call_args.kwargs
    assert kwargs["command_id"] == "cmd-1"
    ack_body = kwargs["body"]
    assert ack_body["rejected"] is True
    assert ack_body["reason"] == "unsupported"
    assert ack_body["success"] is False
    assert ack_body["signingKeyId"] == "our-key"
    assert "signature" in ack_body

    # ACK signature verifies under our OWN public key over commandId + ack fields.
    signed_ack = {
        "commandId": "cmd-1",
        **{k: v for k, v in ack_body.items() if k not in ("signature", "signingKeyId")},
    }
    assert verify_payload(signed_ack, ack_body["signature"], our_pub_hex)


@pytest.mark.asyncio
async def test_skips_command_with_invalid_signature():
    """Admin key known but signature doesn't verify → skip, don't ACK."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, _our_pub_hex = generate_keypair()

    # Signature over DIFFERENT payload — tampered.
    admin_sig = sign_payload({"commandId": "cmd-different"}, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "signature": admin_sig,
        "signingKeyId": "admin-key-id",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="key",
        trusted_public_keys_hex={"admin-key-id": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.1.0",
    )

    # Invalid signature → skip, don't ACK (server's retry / expiry handles it).
    api_client.ack_command.assert_not_called()


@pytest.mark.asyncio
async def test_skips_command_with_unknown_signing_key_when_cache_is_populated():
    """Cache populated but signingKeyId not in it → skip.
    Distinct from the empty-cache B1-bootstrap-mode fallback below."""
    admin_priv, admin_pub_hex = generate_keypair()
    other_priv, _ = generate_keypair()
    our_priv, _ = generate_keypair()
    admin_sig = sign_payload({"commandId": "cmd-1"}, other_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "signature": admin_sig,
        "signingKeyId": "UNKNOWN-KEY",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="key",
        # Non-empty cache but "UNKNOWN-KEY" isn't in it
        trusted_public_keys_hex={"some-other-known-key": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.1.0",
    )
    api_client.ack_command.assert_not_called()


@pytest.mark.asyncio
async def test_b1_bootstrap_fallback_sends_ack_when_cache_empty():
    """B1-specific degradation: when trusted_public_keys_hex is empty (we
    haven't received any add_trusted_key commands yet), skip admin-signature
    verification but STILL send the signed rejection ACK. This lets the
    wire-protocol contract be validated end-to-end against staging in B1.
    B2 populates the cache, after which this fallback no longer triggers."""
    admin_priv, _ = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()
    # Any signature — we won't verify it
    admin_sig = sign_payload({"commandId": "cmd-1"}, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "signature": admin_sig,
        "signingKeyId": "some-key",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="key",
        trusted_public_keys_hex={},  # empty — B1 bootstrap state
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.1.0",
    )

    api_client.ack_command.assert_called_once()
    ack_body = api_client.ack_command.call_args.kwargs["body"]
    assert ack_body["rejected"] is True
    assert ack_body["reason"] == "unsupported"

    # Our ACK signature still must verify under our public key
    signed = {
        "commandId": "cmd-1",
        **{k: v for k, v in ack_body.items() if k not in ("signature", "signingKeyId")},
    }
    assert verify_payload(signed, ack_body["signature"], our_pub_hex)
