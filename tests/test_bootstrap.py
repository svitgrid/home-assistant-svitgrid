"""bootstrap.run_first_time(): generates keypair, calls api_client.bootstrap,
saves state via keystore. Returns the saved KeystoreState."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.api_client import DeviceNotFound
from custom_components.svitgrid.bootstrap import run_first_time
from custom_components.svitgrid.keystore import SvitgridKeystore


@pytest.mark.asyncio
async def test_happy_path_saves_state(hass):
    api_client = AsyncMock()
    api_client.bootstrap.return_value = {
        "apiKey": "a" * 64,
        "trustedKeyIds": ["key-1"],
        "inverters": [],
    }
    keystore = SvitgridKeystore(hass)

    state = await run_first_time(
        api_client=api_client,
        keystore=keystore,
        device_id="dev-1",
        signing_key_id="key-1",
    )

    assert state.api_key == "a" * 64
    assert state.signing_key_id == "key-1"
    assert state.trusted_key_ids == ["key-1"]

    # Re-loading from keystore gives the same state (survives restart)
    reloaded = await keystore.load()
    assert reloaded is not None
    assert reloaded.api_key == "a" * 64


@pytest.mark.asyncio
async def test_propagates_device_not_found(hass):
    api_client = AsyncMock()
    api_client.bootstrap.side_effect = DeviceNotFound("not found")
    keystore = SvitgridKeystore(hass)

    with pytest.raises(DeviceNotFound):
        await run_first_time(
            api_client=api_client,
            keystore=keystore,
            device_id="dev-missing",
            signing_key_id="k",
        )

    # Keystore stays empty on failure — caller can retry cleanly
    assert await keystore.load() is None
