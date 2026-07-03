"""TDD for the set_harvest_config command-poller arm: validate ConfigEntry
exists → TCP-probe the new connection → ACK rejected on probe failure, else
ACK success then apply the connection change + reload. Mirrors set_cloud_endpoint
(Arm 1c)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import SET_HARVEST_CONFIG_COMMAND


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
async def test_set_harvest_config_probe_ok_applies_and_acks_success():
    hass, entry = MagicMock(), MagicMock()
    cmd = {
        "commandId": "c1",
        "command": SET_HARVEST_CONFIG_COMMAND,
        "payload": {"ip": "192.168.1.50", "port": 502, "slaveId": 1},
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.probe_modbus_reachable",
            new=AsyncMock(return_value=True),
        ) as probe,
        patch(
            "custom_components.svitgrid.command_poller.apply_harvest_config_change", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=cmd, **_base_kwargs(hass, entry))
    probe.assert_awaited_once_with("192.168.1.50", 502)
    apply.assert_awaited_once()
    assert ack.await_args.kwargs["success"] is True


@pytest.mark.asyncio
async def test_set_harvest_config_probe_fail_rejects_no_apply():
    hass, entry = MagicMock(), MagicMock()
    cmd = {
        "commandId": "c2",
        "command": SET_HARVEST_CONFIG_COMMAND,
        "payload": {"ip": "10.0.0.9", "port": 502, "slaveId": 1},
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.probe_modbus_reachable",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.svitgrid.command_poller.apply_harvest_config_change", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=cmd, **_base_kwargs(hass, entry))
    apply.assert_not_awaited()
    assert ack.await_args.kwargs["success"] is False
    assert ack.await_args.kwargs["reason"] == "probe_failed"


@pytest.mark.asyncio
async def test_set_harvest_config_no_entry_rejects():
    cmd = {
        "commandId": "c3",
        "command": SET_HARVEST_CONFIG_COMMAND,
        "payload": {"ip": "10.0.0.9", "port": 502, "slaveId": 1},
    }
    with patch(
        "custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()
    ) as ack:
        await process_command(command=cmd, **_base_kwargs(None, None))
    assert ack.await_args.kwargs["success"] is False
