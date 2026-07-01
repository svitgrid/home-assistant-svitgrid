"""poll_commands sends the running integration version as a header."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import SvitgridApiClient


def _mock_session(status=200, json_body=None):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {"commands": []})
    resp.__aenter__.return_value = resp
    resp.__aexit__.return_value = None
    session.get = MagicMock(return_value=resp)
    return session


@pytest.mark.asyncio
async def test_poll_commands_sends_integration_version_header():
    session = _mock_session()
    client = SvitgridApiClient(session, api_base="https://api.example")
    await client.poll_commands(api_key="k" * 10, integration_version="0.11.0")
    _, kwargs = session.get.call_args
    assert kwargs["headers"]["x-integration-version"] == "0.11.0"
    assert kwargs["headers"]["x-api-key"] == "k" * 10


@pytest.mark.asyncio
async def test_poll_commands_omits_header_when_version_none():
    session = _mock_session()
    client = SvitgridApiClient(session, api_base="https://api.example")
    await client.poll_commands(api_key="k" * 10)
    _, kwargs = session.get.call_args
    assert "x-integration-version" not in kwargs["headers"]
