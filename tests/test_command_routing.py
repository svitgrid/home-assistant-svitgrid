"""Tests for per-inverter command routing (Task 8).

process_command must route Arm-2 (dispatchable) commands to the executor
that matches payload.inverterId, and reject with a signed ACK when no
executor is registered for that inverterId.

Uses the same real-keypair signing approach as test_command_poller.py so
_send_signed_ack can produce a valid signature.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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


@pytest.mark.asyncio
async def test_a_failed_battery_charge_current_acks_its_code_and_known_values(hass):
    """A battery charge current failure ACKs a bare `error` code plus the
    prev/target/verify values it knew, as the board and the phone do."""
    from custom_components.svitgrid.harvest.charge_cap_guard import BatteryChargeCurrentError

    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, _ = generate_keypair()
    signed_event_data = {"commandId": "c1", "command": "set_battery_charge_current"}
    command = {
        "commandId": "c1",
        "command": "set_battery_charge_current",
        "payload": {"inverterId": "ha-aaa", "batteryChargeCurrentA": 0},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }
    ack = {
        "error": "verify_mismatch",
        "prevBatteryChargeCurrentA": 120,
        "targetBatteryChargeCurrentA": 0,
        "verifyBatteryChargeCurrentA": 120,
    }
    executor = AsyncMock()
    executor.dispatch.side_effect = BatteryChargeCurrentError("verify_mismatch", ack)
    api_client = AsyncMock()

    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={"admin-key-id": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="our-key",
        executor_version="0.3.0",
        keystore=None,
        executors_by_inverter={"ha-aaa": executor},
    )

    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["success"] is False
    assert body["result"] == ack
    assert body["reason"] == "executor_error: verify_mismatch"


# --- svitgrid D-1939: the signed envelope on a cloud command ---------------

_EVENT_ENVELOPE = {
    "scenarioId": "sc-1",
    "type": "scenario",
    "inverterId": "ha-bbb",
    "mode": "battery_charge",
    "authorizedCommands": ["set_battery_charge", "restore_battery_charge"],
    "schedule": {},
    "config": {"targetSoc": 80},
    "validUntil": "2099-01-01T00:00:00.000Z",
}


def _enveloped_cmd(command: str, admin_priv, signed_event_data: dict) -> dict:
    return {
        "commandId": "c9",
        "command": command,
        "payload": {"inverterId": "ha-bbb", "workMode": 0},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }


async def _process(command, admin_pub_hex, executor, *, envelope_enforce):
    our_priv, our_pub_hex = generate_keypair()
    api_client = AsyncMock()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={"admin-key-id": admin_pub_hex},
        our_private_key=our_priv,
        our_signing_key_id="our-key",
        executor_version="0.3.0",
        keystore=None,
        executors_by_inverter={"ha-bbb": executor},
        envelope_enforce=envelope_enforce,
    )
    return api_client, our_pub_hex


@pytest.mark.asyncio
async def test_enforcing_refuses_a_command_its_envelope_does_not_authorize(hass):
    from custom_components.svitgrid.signing import verify_payload

    admin_priv, admin_pub_hex = generate_keypair()
    executor = AsyncMock()
    api_client, our_pub_hex = await _process(
        _enveloped_cmd("set_work_mode", admin_priv, _EVENT_ENVELOPE),
        admin_pub_hex,
        executor,
        envelope_enforce=True,
    )

    executor.dispatch.assert_not_awaited()
    api_client.ack_command.assert_called_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    assert body["reason"] == "envelope_command_not_authorized"
    assert body["envelopeCheck"]["failed"] == ["command_not_authorized"]
    assert body["envelopeCheck"]["signingKeyId"] == "admin-key-id"
    # The report rides outside the signature, which the API rebuilds from
    # the fixed ack fields (enforce-ha-signed-ack.ts).
    signed = {
        "commandId": "c9",
        **{
            k: body[k]
            for k in ("success", "executorTime", "executorVersion", "rejected", "reason")
            if k in body
        },
    }
    assert verify_payload(signed, body["signature"], our_pub_hex)


@pytest.mark.asyncio
async def test_switch_off_runs_the_command_and_reports_the_check(hass):
    admin_priv, admin_pub_hex = generate_keypair()
    executor = AsyncMock()
    executor.dispatch.return_value = {"ok": True}
    api_client, _ = await _process(
        _enveloped_cmd("set_work_mode", admin_priv, _EVENT_ENVELOPE),
        admin_pub_hex,
        executor,
        envelope_enforce=False,
    )

    executor.dispatch.assert_awaited_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["success"] is True
    assert body["envelopeCheck"]["failed"] == ["command_not_authorized"]


@pytest.mark.asyncio
async def test_enforcing_runs_a_command_its_envelope_authorizes(hass):
    admin_priv, admin_pub_hex = generate_keypair()
    executor = AsyncMock()
    executor.dispatch.return_value = {"ok": True}
    api_client, _ = await _process(
        _enveloped_cmd("set_battery_charge", admin_priv, _EVENT_ENVELOPE),
        admin_pub_hex,
        executor,
        envelope_enforce=True,
    )

    executor.dispatch.assert_awaited_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["success"] is True
    assert "envelopeCheck" not in body


@pytest.mark.asyncio
async def test_enforcing_runs_an_inverter_mismatch_and_reports_it(hass):
    """The switch level enforces the board's mask only: a wrong inverter is
    reported, as on the S3, until the always level."""
    admin_priv, admin_pub_hex = generate_keypair()
    executor = AsyncMock()
    executor.dispatch.return_value = {"ok": True}
    envelope = {**_EVENT_ENVELOPE, "inverterId": "ha-other"}
    api_client, _ = await _process(
        _enveloped_cmd("set_battery_charge", admin_priv, envelope),
        admin_pub_hex,
        executor,
        envelope_enforce=True,
    )

    executor.dispatch.assert_awaited_once()
    body = api_client.ack_command.call_args.kwargs["body"]
    assert body["envelopeCheck"]["failed"] == ["inverter_mismatch"]
