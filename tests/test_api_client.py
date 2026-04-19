"""Unit tests for the aiohttp wrapper. Uses aiohttp's built-in ClientSession
mocking via aioresponses pattern — here we directly mock the session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import (
    BootstrapFailed,
    BootstrapWindowExpired,
    DeviceNotFound,
    PublicKeyMismatch,
    RateLimited,
    SvitgridApiClient,
)


def _mock_session_with_response(status: int, json_body: dict):
    """Build a mocked aiohttp session that returns the given status + JSON
    for the next POST/GET call."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.post = MagicMock(return_value=resp)
    session.get = MagicMock(return_value=resp)
    return session, resp


@pytest.mark.asyncio
class TestBootstrap:
    async def test_happy_path_returns_parsed_response(self):
        session, _ = _mock_session_with_response(
            200,
            {
                "apiKey": "a" * 64,
                "cloudEndpoint": "https://api.example",
                "inverters": [{"inverterId": "inv-1"}],
                "pollingInterval": 5,
                "reportingInterval": 60,
                "trustedKeyIds": ["key-a"],
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")

        resp = await client.bootstrap(
            device_id="dev-1",
            public_key_hex="04" + "aa" * 64,
            signing_key_id="key-a",
        )

        assert resp["apiKey"] == "a" * 64
        assert resp["trustedKeyIds"] == ["key-a"]
        assert resp["inverters"][0]["inverterId"] == "inv-1"

    async def test_404_maps_to_device_not_found(self):
        session, _ = _mock_session_with_response(404, {"error": "Device not found"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(DeviceNotFound):
            await client.bootstrap(
                device_id="dev-missing", public_key_hex="04" + "aa" * 64, signing_key_id="k"
            )

    async def test_409_maps_to_public_key_mismatch(self):
        session, _ = _mock_session_with_response(409, {"error": "..."})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(PublicKeyMismatch):
            await client.bootstrap(
                device_id="dev-1", public_key_hex="04" + "bb" * 64, signing_key_id="k"
            )

    async def test_410_maps_to_bootstrap_window_expired(self):
        session, _ = _mock_session_with_response(410, {"error": "..."})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(BootstrapWindowExpired):
            await client.bootstrap(
                device_id="dev-1", public_key_hex="04" + "aa" * 64, signing_key_id="k"
            )

    async def test_429_maps_to_rate_limited(self):
        session, _ = _mock_session_with_response(429, {"error": "..."})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(RateLimited):
            await client.bootstrap(
                device_id="dev-1", public_key_hex="04" + "aa" * 64, signing_key_id="k"
            )

    async def test_500_maps_to_generic_bootstrap_failed(self):
        session, _ = _mock_session_with_response(500, {"error": "oops"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(BootstrapFailed):
            await client.bootstrap(
                device_id="dev-1", public_key_hex="04" + "aa" * 64, signing_key_id="k"
            )
