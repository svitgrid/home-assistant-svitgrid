"""Tests for per-inverter command routing (Task 8).

process_command must route Arm-2 (dispatchable) commands to the executor
that matches payload.inverterId, and reject with a signed ACK when no
executor is registered for that inverterId.

Uses the same real-keypair signing approach as test_command_poller.py so
_send_signed_ack can produce a valid signature.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.signing import generate_keypair, sign_payload


def _signed_cmd(inverter_id: str, admin_priv, admin_key_id: str = "admin-key-id") -> dict:
    """Build a signed set_battery_charge command targeting the given inverter."""
    signed_event_data = {
        "commandId": "c1",
        "command": "set_battery_charge",
    }
    return {
        "commandId": "c1",
        "command": "set_battery_charge",
        "payload": {"inverterId": inverter_id, "chargePowerLimitW": 2000},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": admin_key_id,
        "signedEventData": signed_event_data,
    }


@pytest.mark.asyncio
async def test_routes_command_to_matching_inverter_executor(hass):
    """Command with payload.inverterId='ha-bbb' → exec_b dispatched, exec_a not called."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()
    trusted_by_id = {"admin-key-id": admin_pub_hex}

    exec_a = AsyncMock()
    exec_b = AsyncMock()
    exec_b.dispatch.return_value = {"service": "modbus.write_register", "args": {}}

    api_client = AsyncMock()

    await process_command(
        command=_signed_cmd("ha-bbb", admin_priv),
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_by_id,
        our_private_key=our_priv,
        our_signing_key_id="our-key",
        executor_version="0.3.0",
        keystore=None,
        executors_by_inverter={"ha-aaa": exec_a, "ha-bbb": exec_b},
    )

    exec_b.dispatch.assert_awaited_once()
    exec_a.dispatch.assert_not_awaited()
    # ACK should have been sent with success=True
    api_client.ack_command.assert_called_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["success"] is True


@pytest.mark.asyncio
async def test_rejects_command_for_unknown_inverter(hass):
    """Command with payload.inverterId not in executors_by_inverter → rejected ACK."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, _ = generate_keypair()
    trusted_by_id = {"admin-key-id": admin_pub_hex}

    exec_a = AsyncMock()
    api_client = AsyncMock()

    await process_command(
        command=_signed_cmd("ha-zzz", admin_priv),
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex=trusted_by_id,
        our_private_key=our_priv,
        our_signing_key_id="our-key",
        executor_version="0.3.0",
        keystore=None,
        executors_by_inverter={"ha-aaa": exec_a},
    )

    exec_a.dispatch.assert_not_awaited()
    api_client.ack_command.assert_called_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["rejected"] is True
    assert body["success"] is False
    assert "no_executor" in body["reason"]
