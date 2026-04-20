"""Unit tests for the aiohttp wrapper. Uses aiohttp's built-in ClientSession
mocking via aioresponses pattern — here we directly mock the session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import (
    BootstrapFailed,
    BootstrapWindowExpired,
    CommandAckFailed,
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


@pytest.mark.asyncio
class TestReadingsPush:
    async def test_posts_reading_with_api_key_header(self):
        session, resp = _mock_session_with_response(200, {"ok": True})
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.push_reading(
            api_key="secret-key",
            reading={
                "inverterId": "inv-1",
                "timestamp": "2026-04-19T12:00:00Z",
                "batterySoc": 80,
                "batteryPower": -1000,
                "pvPower": 2500,
                "gridPower": -500,
                "loadPower": 3000,
                "source": "edge-device",
            },
        )
        call_args = session.post.call_args
        assert call_args.args[0].endswith("/api/v1/ingest/reading")
        assert call_args.kwargs["headers"]["x-api-key"] == "secret-key"
        body = call_args.kwargs["json"]
        assert body["inverterId"] == "inv-1"
        assert body["source"] == "edge-device"


@pytest.mark.asyncio
class TestPollCommands:
    async def test_returns_commands_list(self):
        session, _ = _mock_session_with_response(
            200,
            {
                "commands": [
                    {
                        "commandId": "c1",
                        "command": "set_battery_charge",
                        "signature": "sig",
                        "signingKeyId": "k",
                    }
                ],
                "serverTime": "2026-04-19T12:00:00Z",
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        resp = await client.poll_commands(api_key="secret")
        assert len(resp["commands"]) == 1
        assert resp["commands"][0]["commandId"] == "c1"

    async def test_empty_list_ok(self):
        session, _ = _mock_session_with_response(200, {"commands": [], "serverTime": "t"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        resp = await client.poll_commands(api_key="secret")
        assert resp["commands"] == []

    async def test_aliases_server_id_field_to_commandId(self):
        # Server response uses `id` as the doc ID (per
        # services/api/src/routes/v3/executor-commands.ts:284). Downstream
        # command_poller.process_command expects `commandId`. The client
        # normalizes the wire format at the boundary.
        session, _ = _mock_session_with_response(
            200,
            {
                "commands": [
                    {"id": "cmd-abc", "command": "set_battery_charge"},
                ],
                "serverTime": "t",
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        resp = await client.poll_commands(api_key="secret")
        assert resp["commands"][0]["commandId"] == "cmd-abc"
        # Original `id` still preserved for anyone who needs the raw wire form.
        assert resp["commands"][0]["id"] == "cmd-abc"


@pytest.mark.asyncio
class TestAckCommand:
    async def test_posts_signed_ack_body(self):
        session, _ = _mock_session_with_response(200, {"ok": True})
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.ack_command(
            api_key="secret",
            command_id="c1",
            body={
                "success": False,
                "rejected": True,
                "reason": "unsupported",
                "executorTime": "2026-04-19T12:00:00Z",
                "executorVersion": "0.1.0",
                "signature": "sigbase64",
                "signingKeyId": "our-key",
            },
        )
        call_args = session.post.call_args
        assert call_args.args[0].endswith("/api/v3/executors/commands/c1/ack")
        assert call_args.kwargs["headers"]["x-api-key"] == "secret"
        assert call_args.kwargs["json"]["signature"] == "sigbase64"

    async def test_401_raises_command_ack_failed(self):
        session, _ = _mock_session_with_response(401, {"error": "invalid signature"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(CommandAckFailed):
            await client.ack_command(
                api_key="secret",
                command_id="c1",
                body={"success": False, "signature": "bad", "signingKeyId": "k"},
            )
