"""Tests for the pairing API client."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.pairing_client import (
    PairingClaimed,
    PairingClient,
    PairingExpired,
    PairingNotFound,
    PairingPending,
)


@pytest.fixture
def mock_session():
    session = MagicMock()
    # aiohttp uses async context manager — the .get/.post return a CM whose
    # __aenter__ yields the response.
    return session


def _mock_response(status: int, json_body: dict | None = None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_body or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_start_returns_secret_code_expires(mock_session):
    mock_session.post.return_value = _mock_response(200, {
        "secret": "abc123" * 6, "code": "7K9PA2", "expiresIn": 300,
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    result = await client.start(
        public_key_hex="04" + "a" * 128,
        signing_key_id="ha-home-01",
    )
    assert result["secret"] == "abc123" * 6
    assert result["code"] == "7K9PA2"
    assert result["expiresIn"] == 300


@pytest.mark.asyncio
async def test_get_status_pending(mock_session):
    mock_session.get.return_value = _mock_response(200, {"status": "pending"})
    client = PairingClient(mock_session, api_base="https://api.example.com")
    status = await client.get_status("secret-abc")
    assert isinstance(status, PairingPending)


@pytest.mark.asyncio
async def test_get_status_claimed(mock_session):
    mock_session.get.return_value = _mock_response(200, {
        "status": "claimed",
        "householdId": "h-abc",
        "presetId": None,
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    status = await client.get_status("secret-abc")
    assert isinstance(status, PairingClaimed)
    assert status.household_id == "h-abc"
    assert status.preset_id is None


@pytest.mark.asyncio
async def test_get_status_expired_raises(mock_session):
    mock_session.get.return_value = _mock_response(410)
    client = PairingClient(mock_session, api_base="https://api.example.com")
    with pytest.raises(PairingExpired):
        await client.get_status("secret-abc")


@pytest.mark.asyncio
async def test_get_status_not_found_raises(mock_session):
    mock_session.get.return_value = _mock_response(404)
    client = PairingClient(mock_session, api_base="https://api.example.com")
    with pytest.raises(PairingNotFound):
        await client.get_status("secret-abc")


@pytest.mark.asyncio
async def test_get_status_claimed_with_island_key(mock_session):
    """get_status parses islandKey from the claimed-status body."""
    mock_session.get.return_value = _mock_response(200, {
        "status": "claimed",
        "householdId": "h-island",
        "presetId": None,
        "island": True,
        "cloudIngest": False,
        "islandKey": "sk-app-generated-key",
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    status = await client.get_status("secret-abc")
    assert isinstance(status, PairingClaimed)
    assert status.island is True
    assert status.cloud_ingest is False
    assert status.island_key == "sk-app-generated-key"


@pytest.mark.asyncio
async def test_get_status_claimed_without_island_key(mock_session):
    """get_status returns island_key=None when islandKey absent (non-island or app omitted it)."""
    mock_session.get.return_value = _mock_response(200, {
        "status": "claimed",
        "householdId": "h-std",
        "presetId": None,
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    status = await client.get_status("secret-abc")
    assert isinstance(status, PairingClaimed)
    assert status.island_key is None


@pytest.mark.asyncio
async def test_finalize_returns_apikey(mock_session):
    mock_session.post.return_value = _mock_response(200, {
        "edgeDeviceId": "ed-1",
        "hardwareId": "ha-abc123",
        "apiKey": "test-key-1234567890",
        "householdId": "h-abc",
        "presetId": None,
        "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    result = await client.finalize(
        secret="secret-abc",
        public_key_hex="04" + "a" * 128,
        signing_key_id="ha-home-01",
    )
    assert result["apiKey"] == "test-key-1234567890"
    assert result["edgeDeviceId"] == "ed-1"


@pytest.mark.asyncio
async def test_finalize_post_body_excludes_island_key_and_cloud_ingest(mock_session):
    """finalize POST body must NOT include islandKey or cloudIngestEnabled —
    the cloud no longer reads them from the finalize call (pivot: app owns the key)."""
    mock_session.post.return_value = _mock_response(200, {
        "edgeDeviceId": "ed-1", "hardwareId": "h",
        "apiKey": "k", "householdId": "h",
        "presetId": None, "trustedKeys": [],
    })
    client = PairingClient(mock_session, api_base="https://api.example.com")
    await client.finalize(
        secret="secret-abc",
        public_key_hex="04" + "a" * 128,
        signing_key_id="ha-home-01",
    )
    assert mock_session.post.called
    posted_json = mock_session.post.call_args.kwargs.get("json", {})
    assert "islandKey" not in posted_json
    assert "cloudIngestEnabled" not in posted_json
