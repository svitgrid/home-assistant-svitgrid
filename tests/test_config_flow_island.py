"""Tests for island-mode wiring in async_step_pair_finalize (SP2 Task 2).

When the pairing is island (PairingClaimed.island=True):
  - generate_island_key() is called → key is stored via async_set_island_key
  - cloud_ingest_enabled is written to entry.data
  - the finalize POST body includes islandKey + cloudIngestEnabled

When the pairing is NOT island:
  - no island key is generated
  - no islandKey in the finalize POST body (regression guard)
  - cloud_ingest_enabled not set (or False) in entry.data
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.config_flow import SvitgridConfigFlow
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.pairing_client import PairingClaimed
from custom_components.svitgrid.signing import generate_keypair, serialize_private_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINALIZE_RESPONSE_BASE: dict = {
    "edgeDeviceId": "ed-island",
    "hardwareId": "ha-island-001",
    "apiKey": "island-api-key",
    "householdId": "h-island",
    "presetId": None,
    "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
    "entityMap": {"batterySoc": "sensor.soc"},
    "brand": "Deye",
    "model": "SG04LP3",
    "phases": 3,
    "hasBattery": True,
    "pvStrings": 2,
    "commands": [],
}


def _make_flow(
    hass: HomeAssistant,
    *,
    claimed_status: PairingClaimed,
    mock_finalize_return: dict,
) -> tuple[SvitgridConfigFlow, MagicMock]:
    """Create a flow with _claimed_status pre-set and a mocked _pairing_client.

    Returns (flow, mock_pairing_client) so callers can assert on finalize calls.
    The flow has _final_payload=None so async_step_pair_finalize will call finalize.
    """
    flow = SvitgridConfigFlow()
    flow.hass = hass
    priv_key, pub_hex = generate_keypair()
    flow._private_key = priv_key
    flow._public_key_hex = pub_hex
    flow._signing_key_id = "ha-test-sk"
    flow._secret = "test-secret"
    flow._manual_inverter = None
    flow._claimed_status = claimed_status

    mock_client = MagicMock()
    mock_client.finalize = AsyncMock(return_value=mock_finalize_return)
    flow._pairing_client = mock_client

    return flow, mock_client


async def _prime_keystore(hass: HomeAssistant, flow: SvitgridConfigFlow) -> None:
    """Pre-populate the keystore so async_set_island_key is not a no-op."""
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key=flow._final_payload["apiKey"] if flow._final_payload else "placeholder",
        public_key_hex=flow._public_key_hex,
        private_key_pem=serialize_private_key(flow._private_key),
        signing_key_id=flow._signing_key_id,
        trusted_key_ids=["ha-home-01"],
    )


# ---------------------------------------------------------------------------
# Island finalize — direct (async_step_pair_finalize called directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_island_finalize_generates_and_stores_key(hass: HomeAssistant) -> None:
    """Island pairing: generate_island_key called, stored via async_set_island_key,
    async_get_island_key() returns the key, entry.data has cloud_ingest_enabled=True."""
    finalize_resp = {**_FINALIZE_RESPONSE_BASE, "island": True, "cloudIngest": True}
    claimed = PairingClaimed(household_id="h-island", preset_id=None, island=True, cloud_ingest=True)
    flow, mock_client = _make_flow(hass, claimed_status=claimed, mock_finalize_return=finalize_resp)

    # Prime keystore so async_set_island_key is not a no-op.
    ks = SvitgridKeystore(hass)
    priv, pub = generate_keypair()
    await ks.save(
        api_key="placeholder",
        public_key_hex=flow._public_key_hex,
        private_key_pem=serialize_private_key(flow._private_key),
        signing_key_id=flow._signing_key_id,
        trusted_key_ids=["ha-home-01"],
    )

    fake_key = "deterministic-island-key-for-test"
    with patch("custom_components.svitgrid.config_flow.generate_island_key", return_value=fake_key):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY

    # island key stored in keystore
    stored_key = await ks.async_get_island_key()
    assert stored_key == fake_key

    # cloud_ingest_enabled in entry data
    assert result["data"].get("cloud_ingest_enabled") is True


@pytest.mark.asyncio
async def test_island_finalize_post_body_includes_island_key(hass: HomeAssistant) -> None:
    """finalize POST body must include islandKey and cloudIngestEnabled when island."""
    finalize_resp = {**_FINALIZE_RESPONSE_BASE, "island": True, "cloudIngest": True}
    claimed = PairingClaimed(household_id="h-island", preset_id=None, island=True, cloud_ingest=True)
    flow, mock_client = _make_flow(hass, claimed_status=claimed, mock_finalize_return=finalize_resp)

    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="placeholder",
        public_key_hex=flow._public_key_hex,
        private_key_pem=serialize_private_key(flow._private_key),
        signing_key_id=flow._signing_key_id,
        trusted_key_ids=["ha-home-01"],
    )

    fake_key = "island-key-abc123"
    with patch("custom_components.svitgrid.config_flow.generate_island_key", return_value=fake_key):
        await flow.async_step_pair_finalize()

    mock_client.finalize.assert_awaited_once()
    kwargs = mock_client.finalize.call_args.kwargs
    assert kwargs.get("island_key") == fake_key
    assert kwargs.get("cloud_ingest_enabled") is True


@pytest.mark.asyncio
async def test_non_island_finalize_no_island_key(hass: HomeAssistant) -> None:
    """Non-island pairing: no island key generated, no islandKey in finalize body."""
    finalize_resp = {**_FINALIZE_RESPONSE_BASE}
    claimed = PairingClaimed(household_id="h-std", preset_id=None, island=False, cloud_ingest=True)
    flow, mock_client = _make_flow(hass, claimed_status=claimed, mock_finalize_return=finalize_resp)

    gen_island_key_mock = MagicMock(return_value="should-not-be-called")
    with patch("custom_components.svitgrid.config_flow.generate_island_key", new=gen_island_key_mock):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY

    # generate_island_key must NOT have been called
    gen_island_key_mock.assert_not_called()

    # finalize POST body must NOT include islandKey
    kwargs = mock_client.finalize.call_args.kwargs
    assert "island_key" not in kwargs or kwargs.get("island_key") is None

    # cloud_ingest_enabled not set (or False) for non-island
    assert not result["data"].get("cloud_ingest_enabled")


@pytest.mark.asyncio
async def test_non_island_finalize_keystore_has_no_island_key(hass: HomeAssistant) -> None:
    """Non-island pairing: keystore must not acquire an island key."""
    finalize_resp = {**_FINALIZE_RESPONSE_BASE}
    claimed = PairingClaimed(household_id="h-std2", preset_id=None, island=False, cloud_ingest=True)
    flow, mock_client = _make_flow(hass, claimed_status=claimed, mock_finalize_return=finalize_resp)

    # Prime keystore (island key should stay None after non-island finalize).
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="placeholder",
        public_key_hex=flow._public_key_hex,
        private_key_pem=serialize_private_key(flow._private_key),
        signing_key_id=flow._signing_key_id,
        trusted_key_ids=["ha-home-01"],
    )

    await flow.async_step_pair_finalize()

    stored_key = await ks.async_get_island_key()
    assert stored_key is None


# ---------------------------------------------------------------------------
# Island finalize — full flow (through HA flow manager, _poll_for_claim runs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_island_full_flow_entry_has_cloud_ingest_enabled(
    hass: HomeAssistant,
    enable_custom_integrations,
) -> None:
    """Full end-to-end: island claim → entry.data['cloud_ingest_enabled'] = True."""
    from custom_components.svitgrid.const import DOMAIN

    fake_priv = ec.generate_private_key(ec.SECP256R1())
    finalize_resp = {**_FINALIZE_RESPONSE_BASE, "island": True, "cloudIngest": True}

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
        patch(
            "custom_components.svitgrid.config_flow.generate_island_key",
            return_value="full-flow-island-key",
        ),
        patch(
            "custom_components.svitgrid.config_flow.SvitgridKeystore",
            autospec=True,
        ) as mock_ks_cls,
    ):
        mock_ks = mock_ks_cls.return_value
        mock_ks.async_set_island_key = AsyncMock()

        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(return_value={
            "secret": "sec-island", "code": "ISLAND", "expiresIn": 300,
        })
        mock_client.get_status = AsyncMock(return_value=PairingClaimed(
            household_id="h-island-full",
            preset_id=None,
            island=True,
            cloud_ingest=True,
        ))
        mock_client.finalize = AsyncMock(return_value=finalize_resp)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data.get("cloud_ingest_enabled") is True


@pytest.mark.asyncio
async def test_island_full_flow_finalize_body_has_island_key(
    hass: HomeAssistant,
    enable_custom_integrations,
) -> None:
    """Full end-to-end: island claim → finalize POST body includes islandKey."""
    from custom_components.svitgrid.const import DOMAIN

    fake_priv = ec.generate_private_key(ec.SECP256R1())
    finalize_resp = {**_FINALIZE_RESPONSE_BASE, "island": True, "cloudIngest": True}
    captured_kwargs: dict = {}

    async def _instant_sleep(_: float) -> None:
        pass

    async def _capture_finalize(**kwargs):
        captured_kwargs.update(kwargs)
        return finalize_resp

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch(
            "custom_components.svitgrid.config_flow.generate_keypair",
            return_value=(fake_priv, "04" + "a" * 128),
        ),
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
        patch(
            "custom_components.svitgrid.config_flow.generate_island_key",
            return_value="captured-island-key",
        ),
        patch("custom_components.svitgrid.config_flow.SvitgridKeystore", autospec=True) as mock_ks_cls,
    ):
        mock_ks = mock_ks_cls.return_value
        mock_ks.async_set_island_key = AsyncMock()

        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(return_value={
            "secret": "sec-cap", "code": "CAPTR", "expiresIn": 300,
        })
        mock_client.get_status = AsyncMock(return_value=PairingClaimed(
            household_id="h-cap",
            preset_id=None,
            island=True,
            cloud_ingest=True,
        ))
        mock_client.finalize = AsyncMock(side_effect=_capture_finalize)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

    assert captured_kwargs.get("island_key") == "captured-island-key"
    assert captured_kwargs.get("cloud_ingest_enabled") is True
