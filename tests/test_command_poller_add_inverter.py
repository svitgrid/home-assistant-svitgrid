"""The `add_inverter` command arm.

An inverter added to an already-paired Home Assistant station is created in the
cloud by the app, and the add-on has to learn about it. Its inverter list lives
in the config entry and is written once, at pairing — nothing re-reads it from
the cloud — so without this arm the new inverter exists in Firestore, shows on
the dashboard, and is never polled by anything. No error anywhere.

`add_inverter` is already in the API's ALLOWED_EDGE_COMMANDS (it is what the
ESP32 firmware answers), so the command reaches the add-on today and is ACKed
as unsupported. This makes it mean something here too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import ADD_INVERTER_COMMAND


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


def _entry(inverters: list[dict] | None = None):
    entry = MagicMock()
    entry.data = {
        "api_key": "k",
        "inverters": inverters if inverters is not None else [{"inverter_id": "ha-first"}],
    }
    entry.entry_id = "entry-1"
    return entry


def _command(payload: dict, cmd_id: str = "c1") -> dict:
    return {"commandId": cmd_id, "command": ADD_INVERTER_COMMAND, "payload": payload}


NEW_INVERTER = {
    "inverterId": "ha-second",
    "presetId": "deye-b",
    "entityMap": {"batterySoc": "sensor.b_soc"},
    "brand": "Deye",
    "model": "SG01LP1",
    "phases": 1,
    "hasBattery": True,
    "pvStrings": 2,
    "commands": [],
}


@pytest.mark.asyncio
async def test_appends_the_inverter_and_reloads():
    hass, entry = MagicMock(), _entry()
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=_command(NEW_INVERTER), **_base_kwargs(hass, entry))

    apply.assert_awaited_once()
    assert apply.await_args.args[2]["inverter_id"] == "ha-second"
    assert ack.await_args.kwargs["success"] is True


@pytest.mark.asyncio
async def test_a_payload_with_no_inverter_id_is_rejected():
    """Appending an inverter with no id gives the setup loop an entry it cannot
    route a reading to, and nothing would ever remove it."""
    hass, entry = MagicMock(), _entry()
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=_command({"brand": "Deye"}), **_base_kwargs(hass, entry))

    apply.assert_not_awaited()
    assert ack.await_args.kwargs["success"] is False
    assert ack.await_args.kwargs["reason"] == "missing_inverter_id"


@pytest.mark.asyncio
async def test_an_inverter_already_present_succeeds_without_adding_it_twice():
    """A retried command must not duplicate the inverter. The cloud retries on
    any ACK it did not see, and a duplicate would be polled twice and publish
    each reading twice."""
    hass, entry = MagicMock(), _entry([{"inverter_id": "ha-second"}])
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=_command(NEW_INVERTER), **_base_kwargs(hass, entry))

    apply.assert_not_awaited()
    # Success, NOT rejection: the requested state is the state we are in, and
    # reporting failure would show the owner an error for a thing that worked.
    assert ack.await_args.kwargs["success"] is True


@pytest.mark.asyncio
async def test_refuses_to_grow_past_the_declared_maximum():
    """The same cap /api/svitgrid/hello promises the app. Accepting more than
    we advertise is how the extra inverter ends up stored and never polled."""
    from custom_components.svitgrid.const import MAX_INVERTERS

    hass = MagicMock()
    entry = _entry([{"inverter_id": f"ha-{i}"} for i in range(MAX_INVERTERS)])
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=_command(NEW_INVERTER), **_base_kwargs(hass, entry))

    apply.assert_not_awaited()
    assert ack.await_args.kwargs["success"] is False
    assert ack.await_args.kwargs["reason"] == "too_many_inverters"


@pytest.mark.asyncio
async def test_no_config_entry_is_rejected_rather_than_crashing():
    """A YAML install has no entry to append to — the same fence every other
    entry-writing arm carries."""
    kwargs = _base_kwargs(None, None)
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()) as ack,
    ):
        await process_command(command=_command(NEW_INVERTER), **kwargs)

    apply.assert_not_awaited()
    assert ack.await_args.kwargs["success"] is False
    assert ack.await_args.kwargs["reason"] == "yaml_config_no_entry"


@pytest.mark.asyncio
async def test_a_direct_modbus_inverter_carries_its_address_through():
    hass, entry = MagicMock(), _entry()
    payload = {
        **NEW_INVERTER,
        "harvestConfig": {
            "protocol": "solarman_v5",
            "ip": "192.168.1.11",
            "port": 8899,
            "slaveId": 1,
            "modelId": "deye_sg04lp3",
            "loggerSerial": "222",
        },
    }
    with (
        patch(
            "custom_components.svitgrid.command_poller.apply_add_inverter", new=AsyncMock()
        ) as apply,
        patch("custom_components.svitgrid.command_poller._send_signed_ack", new=AsyncMock()),
    ):
        await process_command(command=_command(payload), **_base_kwargs(hass, entry))

    assert apply.await_args.args[2]["harvest_config"]["ip"] == "192.168.1.11"
