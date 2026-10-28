"""The integration's version comes from manifest.json, a file read. Home Assistant
logs "Detected blocking call to read_text ... inside the event loop" with a full
stack trace for every read made on the loop (svitgrid#1210): on every start, and
on every `hello` request. These tests fail on any manifest read made on the
event loop's thread, and check the version still reaches each caller."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN

_MANIFEST = (
    Path(__file__).resolve().parent.parent / "custom_components" / "svitgrid" / "manifest.json"
)
_VERSION = json.loads(_MANIFEST.read_text())["version"]


@pytest.fixture
def loop_reads(monkeypatch):
    """Every manifest.json read made with an event loop running in the reading
    thread — exactly what Home Assistant's blocking-call detector reports."""
    from custom_components.svitgrid.api_client import _integration_version

    # A read cached by an earlier test would hide this test's own read.
    _integration_version.cache_clear()
    seen: list[Path] = []
    original = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "manifest.json":
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                seen.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    yield seen
    _integration_version.cache_clear()


class _FakeRequest:
    def __init__(self, hass):
        self.app = {"hass": hass}
        self.query: dict = {}
        self.headers: dict = {}


@pytest.mark.asyncio
async def test_hello_reads_the_manifest_off_the_event_loop(hass, loop_reads):
    from custom_components.svitgrid.http_views import SvitgridHelloView

    resp = await SvitgridHelloView().get(_FakeRequest(hass))

    assert loop_reads == []
    assert json.loads(resp.body)["version"] == _VERSION


@pytest.mark.asyncio
async def test_setup_entry_reads_the_manifest_off_the_event_loop(
    hass, enable_custom_integrations, loop_reads
):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.svitgrid import async_setup_entry
    from custom_components.svitgrid.reading_store import ReadingStore

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": ("-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"),
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-xyz",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
        entry_id="test-entry-id",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock) as cp,
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            ReadingStore,
            "get_lifecycle",
            AsyncMock(return_value={"state": "active", "reason": None, "since": None}),
        ),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
    ):
        assert await async_setup_entry(hass, entry) is True
        await hass.async_block_till_done()

    assert loop_reads == []
    assert cp.call_args.kwargs["integration_version"] == _VERSION
    coordinator = hass.data[DOMAIN][entry.entry_id]["update_coordinator"]
    assert coordinator.installed_version == _VERSION
    # Readings batches carry haVersion from the same cache; setup warmed it,
    # so the push made on the loop reads nothing.
    from custom_components.svitgrid.api_client import _integration_version

    assert _integration_version() == _VERSION
    assert loop_reads == []
