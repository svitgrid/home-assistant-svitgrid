"""Tests for the Svitgrid config flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_user_step_goes_straight_to_pair(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Pairing with the mobile app is the only setup offered, so the user step
    opens the pairing screen directly instead of a one-option menu. The manual
    and direct-harvest steps stay in the code but are not offered here."""
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "secret-abc-def" * 4, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=Exception("don't poll yet"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "pair"
    assert "menu_options" not in result
    mock_client.start.assert_awaited_once()


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

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
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

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
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

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
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

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
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


# ── /finalize's trustedKeyStatus is persisted (2026-08-05) ────────────────
#
# A `pending` signing key is otherwise invisible to the add-on: /finalize's
# `trustedKeys` echoes our own key back whatever the server decided, so
# approval cannot be inferred from it. The server refuses every command ACK
# from an unapproved key while telemetry keeps flowing normally.


async def _run_pair_flow_with_finalize(hass, finalize_payload):
    """Drive the pairing flow to CREATE_ENTRY with a stubbed /finalize and
    return the created entry's data."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        """No-op sleep so the poll loop runs immediately."""

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
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
            return_value={"secret": "secret-1", "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(household_id="h-abc", preset_id=None)
        )
        mock_client.finalize = AsyncMock(return_value=finalize_payload)

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.async_block_till_done()
        # Snapshot INSIDE the patch context: leaving it lets HA re-enter setup
        # with the real store, which has no writable db path under pytest.
        return dict(hass.config_entries.async_entries(DOMAIN)[0].data)


_BASE_FINALIZE = {
    "edgeDeviceId": "ed-1",
    "hardwareId": "ha-xyz",
    "apiKey": "test-key",
    "householdId": "h-abc",
    "presetId": None,
    "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
}


async def test_pair_finalize_persists_pending_trusted_key_status(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    data = await _run_pair_flow_with_finalize(
        hass, {**_BASE_FINALIZE, "trustedKeyStatus": "pending"}
    )
    assert data["trusted_key_status"] == "pending"


async def test_pair_finalize_defaults_trusted_key_status_to_approved(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Fail-OPEN against an API deployment that predates the field: absent must
    mean approved, or every healthy household shows a false approval warning."""
    data = await _run_pair_flow_with_finalize(hass, dict(_BASE_FINALIZE))
    assert data["trusted_key_status"] == "approved"


_REFUSAL = (
    422,
    "no_buildable_inverter",
    "None of the claimed inverters can be created: each needs a preset or a manual spec.",
)


async def _drive_refused_pair_flow(
    hass: HomeAssistant,
    *,
    start_results: list[dict],
    finalize_side_effect: list,
) -> tuple[list[dict], AsyncMock]:
    """Run the pair → poll → finalize sequence with a scripted /start and /finalize.

    Returns every flow result, in order, plus the mocked PairingClient. The
    progress screens are the point of these tests, so the poll task must
    actually suspend: `asyncio.sleep` is replaced by a single event-loop yield
    rather than a no-op, or an eager task finishes before the step can return
    `async_show_progress` and no screen is ever produced. Each round is then
    driven the way the frontend drives it — block until the poll task is done,
    then call `async_configure` again — until the flow stops showing progress.
    """
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed

    fake_priv = ec.generate_private_key(ec.SECP256R1())
    # Bound before the patch: patching `config_flow.asyncio.sleep` replaces the
    # attribute on the real asyncio module, so calling it here would recurse.
    real_sleep = asyncio.sleep

    async def _one_loop_tick(_: float) -> None:
        """Yield to the loop once, so the poll task is pending but not slow."""
        await real_sleep(0)

    manager = hass.config_entries.flow

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_one_loop_tick),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(side_effect=start_results)
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(household_id="h-abc", preset_id=None)
        )
        mock_client.finalize = AsyncMock(side_effect=finalize_side_effect)

        init = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        flow_id = init["flow_id"]
        # The user step opens pairing directly, so init is the first progress screen.
        results = [init]
        # Bounded: one round per code on offer, plus a margin that would catch
        # a restart loop instead of hanging the suite.
        for _ in range(len(start_results) + 2):
            if results[-1]["type"] != FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            results.append(await manager.async_configure(flow_id))
        await hass.async_block_till_done()

    return results, mock_client


@pytest.mark.asyncio
async def test_pair_finalize_refused_claim_starts_a_new_pairing(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A 422 `no_buildable_inverter` at finalize hands the owner a NEW code
    instead of ending the flow.

    This test used to assert an abort with reason `claim_not_buildable`, which
    is what 0.22.3 shipped. The abort was correct about the cause and useless
    about the cure: the refused code stays `claimed` server-side and /claim
    answers 409 to it forever, so the owner had to find "Add integration"
    again and walk the whole flow a second time to get a code that works. The
    add-on now calls /ha-pairing/start itself and shows the new code on the
    same progress screen, under text that says why there is a new one.
    """
    from custom_components.svitgrid.pairing_client import PairingRefused

    results, mock_client = await _drive_refused_pair_flow(
        hass,
        start_results=[
            {"secret": "secret-1", "code": "7K9PA2", "expiresIn": 300},
            {"secret": "secret-2", "code": "QW3ZR8", "expiresIn": 300},
        ],
        finalize_side_effect=[PairingRefused(*_REFUSAL), PairingRefused(*_REFUSAL)],
    )

    assert mock_client.start.await_count == 2, "the refusal did not start a new pairing"

    progress = [r for r in results if r["type"] == FlowResultType.SHOW_PROGRESS]
    assert len(progress) >= 2, f"expected a second progress screen: {[r['type'] for r in results]}"
    assert progress[0]["progress_action"] == "waiting_for_mobile"
    assert progress[0]["description_placeholders"] == {"code": "7K9PA2"}
    # The second screen carries the new code AND says why it is new.
    assert progress[-1]["progress_action"] == "waiting_for_mobile_after_refusal"
    assert progress[-1]["description_placeholders"] == {"code": "QW3ZR8"}


@pytest.mark.asyncio
async def test_pair_finalize_refused_twice_aborts_claim_not_buildable(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """One automatic restart, not an endless supply of codes.

    If the owner enters the new code from the same un-updated app, the cloud
    refuses again — a third code would refuse too. The second refusal ends the
    flow with `claim_not_buildable`, which names the app as the thing to fix,
    and creates no entry."""
    from custom_components.svitgrid.pairing_client import PairingRefused

    results, mock_client = await _drive_refused_pair_flow(
        hass,
        start_results=[
            {"secret": "secret-1", "code": "7K9PA2", "expiresIn": 300},
            {"secret": "secret-2", "code": "QW3ZR8", "expiresIn": 300},
        ],
        finalize_side_effect=[PairingRefused(*_REFUSAL), PairingRefused(*_REFUSAL)],
    )

    assert mock_client.start.await_count == 2, "a third pairing was started"
    aborts = [r for r in results if r["type"] == FlowResultType.ABORT]
    assert aborts, f"the flow never aborted: {[r['type'] for r in results]}"
    assert aborts[-1]["reason"] == "claim_not_buildable"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_pair_finalize_other_refusal_code_still_aborts(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Only `no_buildable_inverter` is recoverable by a new code. Any other
    refusal still ends the flow as `pairing_refused`, with the cloud's own
    wording, and starts no second pairing."""
    from custom_components.svitgrid.pairing_client import PairingRefused

    results, mock_client = await _drive_refused_pair_flow(
        hass,
        start_results=[{"secret": "secret-1", "code": "7K9PA2", "expiresIn": 300}],
        finalize_side_effect=[PairingRefused(409, "already_claimed", "That code is used.")],
    )

    assert mock_client.start.await_count == 1
    aborts = [r for r in results if r["type"] == FlowResultType.ABORT]
    assert aborts and aborts[-1]["reason"] == "pairing_refused"
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_pair_finalize_plain_failure_aborts_as_pairing_failed(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Any other finalize error still ends the flow, as `pairing_failed`, not
    as an uncaught exception."""
    from cryptography.hazmat.primitives.asymmetric import ec

    from custom_components.svitgrid.pairing_client import PairingClaimed, PairingError

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        """No-op sleep."""

    results: list[dict] = []
    manager = hass.config_entries.flow
    original_configure = manager.async_configure

    async def _recording_configure(*args, **kwargs):
        result = await original_configure(*args, **kwargs)
        results.append(result)
        return result

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
        patch.object(manager, "async_configure", _recording_configure),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "secret-1", "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(
            return_value=PairingClaimed(household_id="h-abc", preset_id=None)
        )
        mock_client.finalize = AsyncMock(side_effect=PairingError("finalize failed: HTTP 503"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        # The user step opens pairing directly; record that first result too.
        results.append(result)
        await hass.async_block_till_done()

    aborts = [r for r in results if r["type"] == FlowResultType.ABORT]
    assert aborts and aborts[-1]["reason"] == "pairing_failed"
    assert hass.config_entries.async_entries(DOMAIN) == []
