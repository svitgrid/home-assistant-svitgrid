"""Unit tests for the aiohttp wrapper. Uses aiohttp's built-in ClientSession
mocking via aioresponses pattern — here we directly mock the session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.api_client import (
    BootstrapFailed,
    BootstrapWindowExpired,
    CommandAckFailed,
    DeviceEvicted,
    DeviceNotFound,
    DeviceStopped,
    PublicKeyMismatch,
    RateLimited,
    ReadingRejected,
    SvitgridApiClient,
    SvitgridApiError,
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
                "source": "edge",
            },
        )
        call_args = session.post.call_args
        assert call_args.args[0].endswith("/api/v1/ingest/reading")
        assert call_args.kwargs["headers"]["x-api-key"] == "secret-key"
        body = call_args.kwargs["json"]
        assert body["inverterId"] == "inv-1"
        assert body["source"] == "edge"

    async def test_4xx_raises_reading_rejected_with_status(self):
        """A 400 (e.g. validation error — missing required sensors) is a hard
        client error: raise ReadingRejected so the caller backs off instead of
        re-POSTing the same bad payload every cadence tick."""
        session, _ = _mock_session_with_response(400, {"error": "Validation error", "details": []})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(ReadingRejected) as exc_info:
            await client.push_reading(api_key="k", reading={"inverterId": "i"})
        assert exc_info.value.status == 400

    async def test_5xx_returns_none_for_normal_cadence_retry(self):
        """A 5xx is transient — return None (caller retries at normal cadence),
        NOT ReadingRejected (which would park the publisher at its ceiling)."""
        session, _ = _mock_session_with_response(503, {"error": "unavailable"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        result = await client.push_reading(api_key="k", reading={"inverterId": "i"})
        assert result is None


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
    async def test_410_raises_device_evicted(self):
        session, _ = _mock_session_with_response(410, {"error": "Device key revoked"})
        client = SvitgridApiClient(session, "https://api.example")
        with pytest.raises(DeviceEvicted):
            await client.poll_commands(api_key="revoked-key")

    @pytest.mark.asyncio
    async def test_401_returns_empty_not_evicted(self):
        session, _ = _mock_session_with_response(401, {"error": "Invalid API key"})
        client = SvitgridApiClient(session, "https://api.example")
        resp = await client.poll_commands(api_key="maybe-stale")
        assert resp == {"commands": [], "serverTime": None}

    @pytest.mark.asyncio
    async def test_500_returns_empty_not_evicted(self):
        session, _ = _mock_session_with_response(500, {"error": "oops"})
        client = SvitgridApiClient(session, "https://api.example")
        resp = await client.poll_commands(api_key="k")
        assert resp == {"commands": [], "serverTime": None}


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


# ── Phase 2 T10c: MQTT command-wake token mint ────────────────────────


@pytest.mark.asyncio
class TestGetMqttToken:
    async def test_returns_parsed_response(self):
        session, _ = _mock_session_with_response(
            200,
            {
                "token": "eyJhbGc...jwt",
                "expiresAt": "2026-05-23T12:00:00Z",
                "broker": {
                    "host": "mqtt.svitgrid.app",
                    "port": 8883,
                    "topic": "devices/abc123/wake",
                },
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        result = await client.get_mqtt_token(api_key="secret-key")
        assert result["token"].startswith("eyJ")
        assert result["broker"]["topic"] == "devices/abc123/wake"
        assert result["broker"]["port"] == 8883

    async def test_includes_api_key_header(self):
        session, _ = _mock_session_with_response(
            200,
            {
                "token": "t",
                "expiresAt": "x",
                "broker": {"host": "h", "port": 8883, "topic": "devices/x/wake"},
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.get_mqtt_token(api_key="secret-key")
        call_args = session.post.call_args
        assert call_args.kwargs["headers"]["x-api-key"] == "secret-key"
        assert "/api/v3/edge-devices/" in call_args.args[0]
        assert call_args.args[0].endswith("/mqtt-token")

    async def test_503_when_broker_unconfigured_raises(self):
        from custom_components.svitgrid.api_client import SvitgridApiError

        session, _ = _mock_session_with_response(503, {"error": "mqtt_broker_not_configured"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(SvitgridApiError):
            await client.get_mqtt_token(api_key="secret-key")

    async def test_401_raises(self):
        from custom_components.svitgrid.api_client import SvitgridApiError

        session, _ = _mock_session_with_response(401, {"error": "Unauthorized"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(SvitgridApiError):
            await client.get_mqtt_token(api_key="bad-key")


# ── Graceful stop signal: stopped:true in response body ───────────────────


@pytest.mark.asyncio
class TestDeviceStopped:
    async def test_poll_commands_raises_device_stopped_on_signal(self):
        """`poll_commands` raises DeviceStopped when server body has stopped: true."""
        session, _ = _mock_session_with_response(
            200,
            {
                "commands": [],
                "stopped": True,
                "stoppedReason": "manual eviction",
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(DeviceStopped) as exc_info:
            await client.poll_commands(api_key="secret")
        assert exc_info.value.reason == "manual eviction"

    async def test_push_reading_raises_device_stopped_on_signal(self):
        """`push_reading` raises DeviceStopped when server body has stopped: true."""
        session, _ = _mock_session_with_response(
            200,
            {
                "stopped": True,
                "stoppedReason": "zombie poll cost",
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(DeviceStopped) as exc_info:
            await client.push_reading(
                api_key="secret",
                reading={"inverterId": "inv-1", "timestamp": "t", "source": "edge"},
            )
        assert exc_info.value.reason == "zombie poll cost"

    async def test_poll_commands_does_not_stop_when_stopped_falsey(self):
        """`stopped` must be exactly `True` — `false`/missing/truthy do not stop.

        Locks in the `is True` semantics so a future refactor to a truthy
        check (`if data.get("stopped")`) is caught by tests.
        """
        for stopped_value in (False, 0, 1, "true", None):
            body = {"commands": [], "serverTime": None}
            if stopped_value is not None:
                body["stopped"] = stopped_value
            session, _ = _mock_session_with_response(200, body)
            client = SvitgridApiClient(session, api_base="https://api.example")
            # Must NOT raise; returns the normal body.
            data = await client.poll_commands(api_key="secret")
            assert data["commands"] == []


# ── Multi-inverter: add_inverter() ───────────────────────────────────────────


@pytest.mark.asyncio
class TestAddInverter:
    async def test_preset_posts_to_correct_url_and_returns_body(self):
        session, _ = _mock_session_with_response(
            200,
            {
                "inverterId": "ha-abc123",
                "brand": "Deye",
                "entityMap": {"batterySoc": "sensor.soc"},
                "commands": [],
            },
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        body = await client.add_inverter(api_key="my-api-key", preset_id="deye-sg04lp3")

        assert body["inverterId"] == "ha-abc123"
        call_args = session.post.call_args
        assert call_args.args[0].endswith("/api/v1/ha/inverters")
        assert call_args.kwargs["headers"]["x-api-key"] == "my-api-key"
        assert call_args.kwargs["json"] == {"presetId": "deye-sg04lp3"}

    async def test_manual_spec_posts_inverter_key(self):
        spec = {
            "brand": "Foo",
            "model": "Bar",
            "phases": 1,
            "hasBattery": False,
            "pvStrings": 1,
            "entityMap": {"pv1Power": "sensor.pv"},
            "commands": [],
        }
        session, _ = _mock_session_with_response(200, {"inverterId": "ha-x"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.add_inverter(api_key="my-api-key", inverter=spec)

        call_args = session.post.call_args
        assert call_args.args[0].endswith("/api/v1/ha/inverters")
        assert call_args.kwargs["headers"]["x-api-key"] == "my-api-key"
        assert call_args.kwargs["json"] == {"inverter": spec}

    async def test_error_response_raises_svitgrid_api_error(self):
        session, _ = _mock_session_with_response(409, {"error": "cap"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(SvitgridApiError):
            await client.add_inverter(api_key="k", preset_id="p")

    async def test_neither_preset_nor_inverter_raises_before_request(self):
        session, _ = _mock_session_with_response(200, {})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(SvitgridApiError):
            await client.add_inverter(api_key="k")
        session.post.assert_not_called()

    async def test_both_preset_and_inverter_raises_before_request(self):
        session, _ = _mock_session_with_response(200, {})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(SvitgridApiError):
            await client.add_inverter(
                api_key="k",
                preset_id="deye-sg04lp3",
                inverter={"brand": "Foo"},
            )
        session.post.assert_not_called()


@pytest.mark.asyncio
class TestGetPreset:
    async def test_200_returns_parsed_dict(self):
        payload = {
            "id": "deye-sg04lp3-solarman-v1",
            "version": "6",
            "entityMap": {"pv1Power": "sensor.x"},
        }
        session, _ = _mock_session_with_response(200, payload)
        client = SvitgridApiClient(session, api_base="https://api.example")
        result = await client.get_preset("deye-sg04lp3-solarman-v1")
        assert result == payload
        call_args = session.get.call_args
        assert call_args.args[0].endswith("/api/v1/ha-presets/deye-sg04lp3-solarman-v1")

    async def test_404_returns_none(self):
        session, _ = _mock_session_with_response(404, {"error": "Not found"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        result = await client.get_preset("unknown-preset")
        assert result is None


@pytest.mark.asyncio
class TestPushReadingsBatch:
    async def test_posts_readings_array_and_returns_body(self):
        session, _ = _mock_session_with_response(
            200, {"results": [{"ok": True, "inverterId": "inv-1"}], "ingestIntervalMs": 60000}
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        body = await client.push_readings_batch(
            api_key="k" * 64,
            readings=[{"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"}],
        )
        assert body["ingestIntervalMs"] == 60000
        call = session.post.call_args
        assert call.args[0].endswith("/api/v1/ingest/readings")
        assert call.kwargs["json"] == {
            "readings": [{"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"}],
            "haVersion": "0.16.0",  # tracks manifest.json version; census rides every batch
        }

    async def test_5xx_returns_none(self):
        session, _ = _mock_session_with_response(503, {})
        client = SvitgridApiClient(session, api_base="https://api.example")
        assert await client.push_readings_batch(api_key="k" * 64, readings=[]) is None

    async def test_4xx_raises_reading_rejected(self):
        session, _ = _mock_session_with_response(400, {"error": "bad"})
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(ReadingRejected):
            await client.push_readings_batch(api_key="k" * 64, readings=[{"x": 1}])

    async def test_push_readings_batch_410_raises_device_evicted(self):
        session, _ = _mock_session_with_response(
            410, {"error": "Device key revoked (owner household removed)"}
        )
        client = SvitgridApiClient(session, api_base="https://api.example")
        with pytest.raises(DeviceEvicted):
            await client.push_readings_batch(
                api_key="k" * 64,
                readings=[{"inverterId": "i", "timestamp": "2026-06-25T10:00:00Z"}],
            )


class TestBatchHaVersion:
    """v0.15.3: push_readings_batch reports the integration version so the
    cloud can census HA installs (server writes edgeDevices.version on change)."""

    async def test_batch_body_includes_ha_version(self, monkeypatch):
        import custom_components.svitgrid.api_client as mod

        monkeypatch.setattr(mod, "_integration_version", lambda: "0.16.0")
        session, _ = _mock_session_with_response(200, {"results": []})
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.push_readings_batch(
            api_key="k" * 64,
            readings=[{"inverterId": "inv-1", "timestamp": "2026-07-17T10:00:00Z"}],
        )
        assert session.post.call_args.kwargs["json"]["haVersion"] == "0.16.0"

    async def test_batch_body_omits_ha_version_when_unknown(self, monkeypatch):
        import custom_components.svitgrid.api_client as mod

        monkeypatch.setattr(mod, "_integration_version", lambda: None)
        session, _ = _mock_session_with_response(200, {"results": []})
        client = SvitgridApiClient(session, api_base="https://api.example")
        await client.push_readings_batch(api_key="k" * 64, readings=[])
        assert "haVersion" not in session.post.call_args.kwargs["json"]

    def test_integration_version_reads_manifest(self):
        from custom_components.svitgrid.api_client import _integration_version

        _integration_version.cache_clear()
        assert _integration_version() == "0.16.0"
