"""Tests for the Svitgrid config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_user_step_shows_menu(hass: HomeAssistant, enable_custom_integrations) -> None:
    """The user step should present the Pair vs Manual menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert "pair" in result["menu_options"]
    assert "manual" in result["menu_options"]


@pytest.mark.asyncio
async def test_pair_step_calls_start_and_shows_code(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Selecting Pair calls /ha-pairing/start and shows the 6-char code."""
    with patch(
        "custom_components.svitgrid.config_flow.PairingClient",
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "secret-abc-def" * 4,
                "code": "7K9PA2",
                "expiresIn": 300,
            }
        )
        # Block status forever so we stay on the waiting screen
        mock_client.get_status = AsyncMock(side_effect=Exception("don't poll yet"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )

        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert "7K9PA2" in str(result.get("description_placeholders", {}))


@pytest.mark.asyncio
async def test_pair_finalize_creates_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    """When polling returns claimed, finalize runs and an entry is created."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        """Replace asyncio.sleep with a no-op so the poll loop runs immediately."""

    with (
        patch(
            "custom_components.svitgrid.config_flow.PairingClient",
        ) as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch(
            "custom_components.svitgrid.config_flow.asyncio.sleep",
            side_effect=_instant_sleep,
        ),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "secret-1",
                "code": "7K9PA2",
                "expiresIn": 300,
            }
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(
                household_id="h-abc",
                preset_id=None,
            )
        )
        mock_client.finalize = AsyncMock(
            return_value={
                "edgeDeviceId": "ed-1",
                "hardwareId": "ha-xyz",
                "apiKey": "test-key",
                "householdId": "h-abc",
                "presetId": None,
                "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
            }
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        # SHOW_PROGRESS → eventually CREATE_ENTRY after the polling loop sees claimed.
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        assert entries[0].data["api_key"] == "test-key"
        assert entries[0].data["household_id"] == "h-abc"


async def test_pair_finalize_persists_preset_fields(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Phase 2: when /finalize returns a preset's entityMap + brand metadata,
    those land in the config entry so async_setup_entry can boot the
    readings publisher with a working mapping."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch(
            "custom_components.svitgrid.config_flow.PairingClient",
        ) as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch(
            "custom_components.svitgrid.config_flow.asyncio.sleep",
            side_effect=_instant_sleep,
        ),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "secret-2",
                "code": "ABCD12",
                "expiresIn": 300,
            }
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(
                household_id="h-deye",
                preset_id="deye-sg04lp3-solarman-v1",
            )
        )
        mock_client.finalize = AsyncMock(
            return_value={
                "edgeDeviceId": "ed-2",
                "hardwareId": "ha-deye-001",
                "apiKey": "deye-key",
                "householdId": "h-deye",
                "presetId": "deye-sg04lp3-solarman-v1",
                "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
                # Phase 2 fields from /finalize response:
                "entityMap": {
                    "batterySoc": "sensor.inverter_battery",
                    "loadPower": "sensor.inverter_load_power",
                },
                "brand": "Deye",
                "model": "SG04LP3",
                "phases": 3,
                "hasBattery": True,
                "pvStrings": 2,
            }
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        data = entries[0].data
        # Existing fields still present
        assert data["api_key"] == "deye-key"
        assert data["preset_id"] == "deye-sg04lp3-solarman-v1"
        # New Phase 2 fields persisted in snake_case (HA convention)
        assert data["entity_map"] == {
            "batterySoc": "sensor.inverter_battery",
            "loadPower": "sensor.inverter_load_power",
        }
        assert data["brand"] == "Deye"
        assert data["model"] == "SG04LP3"
        assert data["phases"] == 3
        assert data["has_battery"] is True
        assert data["pv_strings"] == 2


async def test_pair_finalize_populates_inverters_list(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Regression: a finalized pairing MUST produce an entry whose
    `inverters` list is non-empty, so the readings publisher actually starts.

    The entry is born at VERSION 2, so the v1->v2 migration never runs; if
    finalize only writes the flat entity_map, `_inverters_from_entry` returns
    [] and async_setup_entry logs "no inverters configured; nothing to publish"
    (the cause of forslim@gmail.com's stuck onboarding, 2026-06-03)."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid import _inverters_from_entry
    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch(
            "custom_components.svitgrid.config_flow.PairingClient",
        ) as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch(
            "custom_components.svitgrid.config_flow.asyncio.sleep",
            side_effect=_instant_sleep,
        ),
        patch(
            # We assert the created entry's shape, not its runtime; skip the real
            # setup so background tasks (command poller / mqtt wake) don't fire.
            "custom_components.svitgrid.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "secret-inv",
                "code": "INVLST",
                "expiresIn": 300,
            }
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(
                household_id="h-deye",
                preset_id="deye-sg03lp1-solarman-v1",
            )
        )
        mock_client.finalize = AsyncMock(
            return_value={
                "edgeDeviceId": "ed-9",
                "hardwareId": "ha-9f99",
                "apiKey": "k9",
                "householdId": "h-deye",
                "presetId": "deye-sg03lp1-solarman-v1",
                "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
                "entityMap": {
                    "batterySoc": "sensor.inverter_battery",
                    "loadPower": "sensor.inverter_load_power",
                },
                "brand": "Deye",
                "model": "SG03LP1",
                "phases": 1,
                "hasBattery": True,
                "pvStrings": 2,
                "commands": [],
            }
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        entry = entries[0]

        # The canonical inverters list must exist with the paired inverter.
        invs = entry.data.get("inverters")
        assert invs, "pairing finalize must populate entry.data['inverters']"
        assert len(invs) == 1
        assert invs[0]["inverter_id"] == "ha-9f99"
        assert invs[0]["entity_map"] == {
            "batterySoc": "sensor.inverter_battery",
            "loadPower": "sensor.inverter_load_power",
        }

        # And the helper async_setup_entry uses must see that inverter, so the
        # readings publisher will start (the actual bug being fixed).
        resolved = _inverters_from_entry(entry)
        assert len(resolved) == 1
        assert resolved[0]["inverter_id"] == "ha-9f99"


async def test_pair_finalize_phase_1_compat_when_no_preset(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """When /finalize returns no preset fields (Phase 1 add-on or unknown
    presetId), the entry's new fields default to None/empty so
    async_setup_entry can still load."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch(
            "custom_components.svitgrid.config_flow.PairingClient",
        ) as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch(
            "custom_components.svitgrid.config_flow.asyncio.sleep",
            side_effect=_instant_sleep,
        ),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "s3",
                "code": "BAREPP",
                "expiresIn": 300,
            }
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(
                household_id="h-bare",
                preset_id=None,
            )
        )
        mock_client.finalize = AsyncMock(
            return_value={
                "edgeDeviceId": "ed-3",
                "hardwareId": "ha-bare-001",
                "apiKey": "bare-key",
                "householdId": "h-bare",
                "presetId": None,
                "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
                "entityMap": None,
                "brand": None,
                "model": None,
                "phases": None,
                "hasBattery": None,
                "pvStrings": None,
            }
        )

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        data = entries[0].data
        assert data["entity_map"] == {}  # empty dict, not None — easier for async_setup_entry
        assert data["brand"] is None
        assert data["preset_id"] is None


def test_manual_fields_derive_from_mappable_source():
    """The manual pairing field list must be exactly MAPPABLE_FIELDS — no
    separate hardcoded copy that can drift."""
    from custom_components.svitgrid.config_flow import _MANUAL_FIELDS
    from custom_components.svitgrid.const import MAPPABLE_FIELDS

    assert list(_MANUAL_FIELDS) == list(MAPPABLE_FIELDS)


@pytest.mark.asyncio
async def test_options_flow_shows_init_form(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Clicking Configure now renders the add/edit/remove menu (not a flat form)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"entity_map": {"batterySoc": "sensor.soc"}},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) >= {"add_inverter", "edit_inverter", "remove_inverter"}


@pytest.mark.asyncio
async def test_options_flow_saves_and_drops_blanks(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """edit_inverter step writes the cleaned entity_map (blank selectors dropped)
    directly into entry.data["inverters"] for the selected inverter.

    HA's EntitySelector rejects literal "" at schema-validation time, so we
    omit the field entirely rather than passing "" — that is exactly how HA
    delivers cleared optional selectors in real usage.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    inv_id = "ha-aaa"
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "e1",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": inv_id,
                    "entity_map": {"batterySoc": "sensor.old_soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "X",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
    )
    entry.add_to_hass(hass)

    # Go through: init (menu) → edit_inverter (pick inverter) → edit_inverter (remap)
    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "menu"

    # Step 1: pick inverter
    result = await flow.async_step_edit_inverter({"inverter_id": inv_id})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_inverter"

    # Step 2: submit remapped sensors (loadPower absent → dropped)
    result = await flow.async_step_edit_inverter(
        {
            "batterySoc": "sensor.new_soc",
            "gridPower": "sensor.grid",
            # loadPower absent → dropped by cleaned = {k: v for k, v in … if v}
        }
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    updated_inv = next(i for i in entry.data["inverters"] if i["inverter_id"] == inv_id)
    assert updated_inv["entity_map"] == {
        "batterySoc": "sensor.new_soc",
        "gridPower": "sensor.grid",
    }
    assert "loadPower" not in updated_inv["entity_map"]


@pytest.mark.asyncio
async def test_options_flow_rejects_empty_map(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """edit_inverter re-shows the form with an error when no entities are selected
    and leaves the inverter's entity_map untouched.

    We submit an empty dict rather than passing "" values because HA's
    EntitySelector rejects blank strings at schema-validation time; an empty
    user_input dict is what HA delivers when every optional selector is cleared.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    inv_id = "ha-aaa"
    original_map = {"batterySoc": "sensor.soc"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "e1",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": inv_id,
                    "entity_map": original_map,
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "X",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
    )
    entry.add_to_hass(hass)

    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass

    # Navigate: menu → edit_inverter picker → edit_inverter remap (pick inverter first)
    await flow.async_step_init()
    await flow.async_step_edit_inverter({"inverter_id": inv_id})

    # Empty dict = no fields submitted (all optional selectors cleared).
    result = await flow.async_step_edit_inverter({})

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_inverter"
    assert result["errors"] == {"base": "no_entities_selected"}
    # entity_map is unchanged
    updated_inv = next(i for i in entry.data["inverters"] if i["inverter_id"] == inv_id)
    assert updated_inv["entity_map"] == original_map
