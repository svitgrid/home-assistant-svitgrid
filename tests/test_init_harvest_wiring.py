"""Task 11 wiring: an inverter carrying `harvest_config` spawns the direct
Modbus harvest loop (run_direct_harvest_loop) instead of the HA-entity
run_readings_loop. Inverters without harvest_config keep the entity loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

_MINIMAL_SPEC = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "reads": [],
    "derivations": [],
}


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _make_entry(harvest_config):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (harvest)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-harvest",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-xyz",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                    "harvest_config": harvest_config,
                }
            ],
        },
        entry_id="entry-harvest",
    )


@pytest.mark.asyncio
async def test_harvest_config_spawns_direct_harvest_loop_not_entity_loop(
    hass, enable_custom_integrations
):
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry(
        {"model_id": "deye_sg04lp3", "host": "10.0.0.5", "port": 8899, "slave_id": 1}
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock) as rp,
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    # The entity readings loop must NOT have been spawned for this inverter.
    assert rp.call_count == 0
    # The direct harvest loop WAS spawned exactly once.
    assert harvest.call_count == 1
    hk = harvest.call_args.kwargs
    assert hk["inverter_id"] == "ha-xyz"
    assert hk["cfg"]["model_id"] == "deye_sg04lp3"
    assert hk["spec_holder"] is not None
    # The spec was loaded + parsed into a RegisterSpec on the holder.
    from custom_components.svitgrid.harvest.register_spec import RegisterSpec

    assert isinstance(hk["spec_holder"].spec, RegisterSpec)
    assert hk["spec_holder"].spec.model_id == "deye_sg04lp3"
    client.get_register_spec.assert_awaited_once_with("deye_sg04lp3")

    # The task is registered under readings_tasks (so shutdown cancels it) with
    # the harvest-specific name.
    entry_state = hass.data[DOMAIN][entry.entry_id]
    task = entry_state["readings_tasks"]["ha-xyz"]
    assert task is not None
    assert task.get_name() == "svitgrid_harvest_ha-xyz"


@pytest.mark.asyncio
async def test_spec_load_failure_does_not_crash_setup(hass, enable_custom_integrations):
    """load_spec is fail-open: a failed spec fetch leaves spec_holder.spec=None
    and setup still completes (the loop idles until a spec exists)."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry({"model_id": "deye_sg04lp3", "host": "10.0.0.5"})
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(side_effect=RuntimeError("network"))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    assert harvest.call_count == 1
    # Fail-open: holder.spec stays None, loop tolerates it.
    assert harvest.call_args.kwargs["spec_holder"].spec is None
