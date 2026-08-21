"""The recommended pairing path must set up an EyBond collector too.

Before this, collector setup lived only behind "Set up direct inverter
connection manually" -- an advanced menu entry. A real user picking
"Pair with the Svitgrid mobile app" and choosing the SmartESS-collector preset
in the app got an integration that paired cleanly and then read nothing: no
listener, no announce, no routing serial.

The app already tells us which inverter this is, and the preset says how it is
read (``protocolId``). So the flow can ask the collector questions itself,
after finalize, instead of making the user find the advanced path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.config_flow import SvitgridConfigFlow
from custom_components.svitgrid.eybond_at.register_map import OutputMode
from custom_components.svitgrid.eybond_at.setup import DiscoveredCollector

EYBOND_PRESET = {
    "id": "anenji-smartess-v1",
    "version": "1",
    "brand": "Anenji",
    "model": "Hybrid (SmartESS collector)",
    "phases": 1,
    "hasBattery": True,
    "pvStrings": 2,
    "protocolId": "home_assistant_eybond",
    "entityMap": {},
    "commands": [],
}
RELAY_PRESET = {**EYBOND_PRESET, "id": "anenji-generic-v1", "protocolId": "home_assistant"}

SERIAL = "99432604107106"


def _make_flow(hass: HomeAssistant, *, preset_id: str | None) -> SvitgridConfigFlow:
    flow = SvitgridConfigFlow()
    flow.hass = hass
    flow._signing_key_id = "ha-sk"
    flow._private_key = ec.generate_private_key(ec.SECP256R1())
    flow._public_key_hex = "04" + "a" * 128
    flow._final_payload = {
        "edgeDeviceId": "ed-h",
        "hardwareId": "ha-78a3a28be0ca",
        "apiKey": "k",
        "householdId": "h",
        "presetId": preset_id,
        "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
        "entityMap": {},
        "brand": "Anenji",
        "model": "Hybrid (SmartESS collector)",
        "phases": 1,
        "hasBattery": False,
        "pvStrings": 1,
        "commands": [],
    }
    return flow


def _mock_preset(preset: dict | None):
    """Patch the client the flow uses to look a preset up by id."""
    inst = MagicMock()
    inst.get_preset = AsyncMock(return_value=preset)
    return patch(
        "custom_components.svitgrid.config_flow.SvitgridApiClient",
        new=MagicMock(return_value=inst),
    ), inst


def _mock_discovery(collectors):
    return patch(
        "custom_components.svitgrid.config_flow.discover_collectors",
        new=AsyncMock(return_value=collectors),
    )


def _on_lan():
    """Pretend Home Assistant is on the LAN, so no network form is needed."""
    return patch(
        "custom_components.svitgrid.config_flow.default_local_ip",
        new=MagicMock(return_value="192.168.1.34"),
    )


@pytest.mark.asyncio
async def test_a_collector_preset_asks_which_inverter(hass: HomeAssistant) -> None:
    flow = _make_flow(hass, preset_id="anenji-smartess-v1")
    preset_patch, client = _mock_preset(EYBOND_PRESET)
    found = [
        DiscoveredCollector(
            serial=SERIAL,
            address="192.168.1.116",
            protocol_number=11,
            firmware="7803_A6260126v1",
            output_mode=OutputMode.SINGLE,
        )
    ]

    with preset_patch, _on_lan(), _mock_discovery(found):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "eybond_collector"
    client.get_preset.assert_awaited_once_with("anenji-smartess-v1")


@pytest.mark.asyncio
async def test_the_picked_collector_reaches_the_created_entry(hass: HomeAssistant) -> None:
    """The whole point: the serial the user picked must survive into the entry.

    It is the hub's ONLY routing key -- lose it and the inverter never
    publishes, with nothing in the log to say why.
    """
    flow = _make_flow(hass, preset_id="anenji-smartess-v1")
    preset_patch, client = _mock_preset(EYBOND_PRESET)
    found = [
        DiscoveredCollector(
            serial=SERIAL,
            address="192.168.1.116",
            protocol_number=11,
            firmware="7803_A6260126v1",
            output_mode=OutputMode.SINGLE,
        )
    ]

    with preset_patch, _on_lan(), _mock_discovery(found):
        await flow.async_step_pair_finalize()
        result = await flow.async_step_eybond_collector({"inverter_serial": SERIAL})

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Finishing re-enters pair_finalize. Nothing may ask a second time: the
    # collected config is what stops it, so a regression here shows up as a
    # repeated question or, worse, unbounded recursion.
    assert client.get_preset.await_count == 1
    inverter = result["data"]["inverters"][0]
    assert inverter["harvest_config"]["protocol"] == "eybond_at"
    assert inverter["harvest_config"]["inverter_serial"] == SERIAL


@pytest.mark.asyncio
async def test_a_relay_preset_still_finishes_without_asking(hass: HomeAssistant) -> None:
    """Regression guard: every other preset must be untouched by this."""
    flow = _make_flow(hass, preset_id="anenji-generic-v1")
    preset_patch, _ = _mock_preset(RELAY_PRESET)

    with preset_patch, _on_lan():
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "harvest_config" not in result["data"]["inverters"][0]


@pytest.mark.asyncio
async def test_a_preset_lookup_that_fails_does_not_break_pairing(hass: HomeAssistant) -> None:
    """Fail OPEN. The pairing itself already succeeded server-side.

    Aborting here would leave the household with an edge device and an
    inverter in the cloud and no integration on this side -- a state the user
    can only escape by deleting things they cannot see.
    """
    flow = _make_flow(hass, preset_id="anenji-smartess-v1")
    preset_patch, _ = _mock_preset(None)

    with preset_patch, _on_lan():
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_no_preset_at_all_is_not_a_lookup(hass: HomeAssistant) -> None:
    """Manual pairings carry presetId None; asking the API for None would 404."""
    flow = _make_flow(hass, preset_id=None)
    preset_patch, client = _mock_preset(EYBOND_PRESET)

    with preset_patch, _on_lan():
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    client.get_preset.assert_not_awaited()
