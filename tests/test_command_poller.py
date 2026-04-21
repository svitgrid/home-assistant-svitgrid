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
    """Admin key known + valid signature on a non-dispatchable command →
    send signed rejection ACK with reason='unsupported' (Arm 3)."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-1", "command": "some_future_command"}
    admin_sig = sign_payload(signed_event_data, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "some_future_command",
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
        keystore=AsyncMock(),
        executor=None,
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
        keystore=AsyncMock(),
        executor=None,
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
        keystore=AsyncMock(),
        executor=None,
    )
    api_client.ack_command.assert_not_called()


@pytest.mark.asyncio
async def test_add_trusted_key_command_updates_cache_and_acks_success():
    """add_trusted_key is an internal command: extract publicKeyHex +
    signingKeyId from payload, update in-memory cache + persist via
    keystore, ACK with success."""
    our_priv, our_pub_hex = generate_keypair()
    trusted_cache: dict[str, str] = {}

    command = {
        "commandId": "add-trust-1",
        "command": "add_trusted_key",
        "payload": {
            "publicKeyHex": "04" + "dd" * 64,
            "signingKeyId": "new-admin-key",
        },
        # Internal commands don't require a signed admin payload
        "signature": None,
        "signingKeyId": None,
        "signedEventData": None,
    }

    api_client = AsyncMock()
    keystore = AsyncMock()
    keystore.update_trusted_keys_hex = AsyncMock()

    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_cache,
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=keystore,
        executor=None,
    )

    # In-memory cache mutated
    assert trusted_cache == {"new-admin-key": "04" + "dd" * 64}
    # Persisted via keystore
    keystore.update_trusted_keys_hex.assert_called_once_with({"new-admin-key": "04" + "dd" * 64})
    # ACK with success
    api_client.ack_command.assert_called_once()
    ack_body = api_client.ack_command.call_args.kwargs["body"]
    assert ack_body["success"] is True


@pytest.mark.asyncio
async def test_revoke_trusted_key_command_removes_from_cache():
    our_priv, _ = generate_keypair()
    trusted_cache = {
        "admin-a": "04" + "aa" * 64,
        "admin-b": "04" + "bb" * 64,
    }

    command = {
        "commandId": "revoke-1",
        "command": "revoke_trusted_key",
        "payload": {"signingKeyId": "admin-b"},
        "signature": None,
        "signingKeyId": None,
        "signedEventData": None,
    }

    api_client = AsyncMock()
    keystore = AsyncMock()
    keystore.update_trusted_keys_hex = AsyncMock()

    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_cache,
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=keystore,
        executor=None,
    )

    assert trusted_cache == {"admin-a": "04" + "aa" * 64}
    keystore.update_trusted_keys_hex.assert_called_once_with({"admin-a": "04" + "aa" * 64})
    ack_body = api_client.ack_command.call_args.kwargs["body"]
    assert ack_body["success"] is True


@pytest.mark.asyncio
async def test_empty_cache_rejects_write_command_no_fallback():
    """B1-fallback is GONE. Empty trusted-keys cache + writable command →
    skip (no ACK), same as unknown signing key."""
    admin_priv, _ = generate_keypair()
    our_priv, _ = generate_keypair()
    admin_sig = sign_payload({"commandId": "cmd-1"}, admin_priv)

    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "payload": {"chargePowerLimitW": 3000},
        "signature": admin_sig,
        "signingKeyId": "some-admin",
        "signedEventData": {"commandId": "cmd-1"},
    }

    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},  # empty cache — no fallback anymore
        our_private_key=our_priv,
        our_signing_key_id="us",
        executor_version="0.2.0",
        keystore=AsyncMock(),
        executor=None,
    )

    # No ACK sent — command is skipped, let server expiry handle it.
    api_client.ack_command.assert_not_called()
