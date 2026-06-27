from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import SvitgridApiClient


def _mock(status, body):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body)
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.get = MagicMock(return_value=resp)
    return session


@pytest.mark.asyncio
async def test_get_register_spec_200_returns_body():
    session = _mock(200, {"modelId": "deye_sg04lp3", "version": 1})
    client = SvitgridApiClient(session, api_base="https://api.example")
    spec = await client.get_register_spec("deye_sg04lp3")
    assert spec["modelId"] == "deye_sg04lp3"
    session.get.assert_called_once()
    assert "/api/v1/register-specs/deye_sg04lp3" in session.get.call_args[0][0]


@pytest.mark.asyncio
async def test_get_register_spec_404_returns_none():
    client = SvitgridApiClient(_mock(404, {}), api_base="https://api.example")
    assert await client.get_register_spec("nope") is None
