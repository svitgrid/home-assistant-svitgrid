"""Setup must start a direct-harvest loop that can load its spec (issue #5).

Add-on 0.22.x stored the cloud's camelCase `harvestConfig` verbatim, so the
entries it created carry `modelId` where setup reads `model_id`. The spec load
raised a `KeyError` behind a fail-open `except`, and the loop idled forever.
Those installs must recover on the next restart, without re-pairing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.inverter_entry import inverters_from_finalize
from custom_components.svitgrid.reading_store import ReadingStore
from tests.test_init_harvest_wiring import _ACTIVE_LIFECYCLE, _MINIMAL_SPEC, _make_entry

_CAMEL = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slaveId": 1,
    "modelId": "deye_sg04lp3",
    "loggerSerial": "1234567890",
}


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


async def _setup(hass, entry):
    from custom_components.svitgrid import async_setup_entry

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        # With `model_id` readable, settings sync finds this inverter eligible
        # and dials the logger. That is the fix working; it is not under test.
        patch("custom_components.svitgrid.run_settings_sync_loop", new_callable=AsyncMock),
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
    return harvest, client


def _assert_loop_has_a_loaded_spec(harvest, client) -> None:
    from custom_components.svitgrid.harvest.register_spec import RegisterSpec

    assert harvest.call_count == 1
    kwargs = harvest.call_args.kwargs
    assert kwargs["cfg"]["model_id"] == "deye_sg04lp3"
    assert kwargs["cfg"]["logger_serial"] == "1234567890"
    assert isinstance(kwargs["spec_holder"].spec, RegisterSpec)
    client.get_register_spec.assert_awaited_once_with("deye_sg04lp3")


@pytest.mark.asyncio
async def test_an_entry_stored_in_camel_case_loads_its_spec(hass, enable_custom_integrations):
    entry = _make_entry(dict(_CAMEL))
    entry.add_to_hass(hass)

    harvest, client = await _setup(hass, entry)

    _assert_loop_has_a_loaded_spec(harvest, client)


@pytest.mark.asyncio
async def test_an_entry_stored_in_camel_case_is_rewritten(hass, enable_custom_integrations):
    """Settings sync and the set_harvest_config arm read `entry.data` directly,
    not the list setup builds. Fixing only the setup copy would leave them
    reading `modelId` on every tick."""
    entry = _make_entry(dict(_CAMEL))
    entry.add_to_hass(hass)

    await _setup(hass, entry)

    stored = entry.data["inverters"][0]["harvest_config"]
    assert stored["model_id"] == "deye_sg04lp3"
    assert stored["slave_id"] == 1
    assert "modelId" not in stored


@pytest.mark.asyncio
async def test_a_finalize_payload_starts_a_loop_with_a_loaded_spec(
    hass, enable_custom_integrations
):
    """From the cloud's own description to a running loop, with nothing between
    them that knows about casing."""
    inverters = inverters_from_finalize(
        {"inverters": [{"inverterId": "ha-xyz", "harvestConfig": dict(_CAMEL)}]},
        fallback_id="unused",
    )
    entry = _make_entry(inverters[0]["harvest_config"])
    entry.add_to_hass(hass)

    harvest, client = await _setup(hass, entry)

    _assert_loop_has_a_loaded_spec(harvest, client)
