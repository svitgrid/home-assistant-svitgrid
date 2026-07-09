"""TDD for the set_read_source command-poller arm: switch an inverter
between relay (edge-forwarded) and native (direct Modbus harvest) read
sources. Mirrors set_harvest_config (Arm 1c-bis) — native mode probes the
Modbus endpoint before applying; relay mode clears harvest_config without
probing."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import SET_READ_SOURCE_COMMAND


def _base_kwargs(hass, entry):
    return dict(
        api_client=AsyncMock(),
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=MagicMock(),
        our_signing_key_id="add-on-key",
        executor_version="1.0.0",
        keystore=None,
        hass=hass,
        entry=entry,
    )


@pytest.mark.asyncio
async def test_set_read_source_native_probe_ok_applies_snake_cased_and_acks_success():
    hass, entry = MagicMock(), MagicMock()
    cmd = {
        "commandId": "c1",
        "command": SET_READ_SOURCE_COMMAND,
        "payload": {
            "inverterId": "ha-1",
            "mode": "native",
            "harvestConfig": {
                "protocol": "solarman_v5",
                "ip": "192.168.1.50",
                "port": 8899,
                "slaveId": 1,
                "modelId": "deye_sg04lp3",
                "loggerSerial": "1234567890",
            },
        },
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.probe_modbus_reachable",
            new=AsyncMock(return_value=True),
        ) as probe,
        patch(
            "custom_components.svitgrid.command_poller.apply_read_source_change", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=cmd, **_base_kwargs(hass, entry))

    probe.assert_awaited_once_with("192.168.1.50", 8899)
    apply.assert_awaited_once_with(
        hass,
        entry,
        "ha-1",
        {
            "protocol": "solarman_v5",
            "ip": "192.168.1.50",
            "port": 8899,
            "slave_id": 1,
            "model_id": "deye_sg04lp3",
            "logger_serial": "1234567890",
        },
    )
    assert ack.await_args.kwargs["success"] is True


@pytest.mark.asyncio
async def test_set_read_source_native_probe_fail_rejects_no_apply():
    hass, entry = MagicMock(), MagicMock()
    cmd = {
        "commandId": "c2",
        "command": SET_READ_SOURCE_COMMAND,
        "payload": {
            "inverterId": "ha-1",
            "mode": "native",
            "harvestConfig": {
                "ip": "10.0.0.9",
                "port": 8899,
                "slaveId": 1,
                "modelId": "deye_sg04lp3",
            },
        },
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.probe_modbus_reachable",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.svitgrid.command_poller.apply_read_source_change", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=cmd, **_base_kwargs(hass, entry))

    apply.assert_not_awaited()
    assert ack.await_args.kwargs["success"] is False
    assert ack.await_args.kwargs["reason"] == "probe_failed"


@pytest.mark.asyncio
async def test_set_read_source_relay_applies_clear_no_probe_and_acks_success():
    hass, entry = MagicMock(), MagicMock()
    cmd = {
        "commandId": "c3",
        "command": SET_READ_SOURCE_COMMAND,
        "payload": {
            "inverterId": "ha-1",
            "mode": "relay",
        },
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.probe_modbus_reachable",
            new=AsyncMock(),
        ) as probe,
        patch(
            "custom_components.svitgrid.command_poller.apply_read_source_change", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=cmd, **_base_kwargs(hass, entry))

    probe.assert_not_awaited()
    apply.assert_awaited_once_with(hass, entry, "ha-1", None)
    assert ack.await_args.kwargs["success"] is True
