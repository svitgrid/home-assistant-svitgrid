"""Tests for the `set_cloud_ingest` command + `enable_island` cloudIngest payload.

Background: `cloud_ingest_enabled` gates the cloud sender (`__init__.py`:
`if cloud_ingest_enabled: ... run_sender_loop`). Before this change the flag was
reachable ONLY at pairing time — `enable_island` hardcoded it to False and
`disable_island` to True, so the "island + cloud" combination could never be
set or unset after pairing. A user who paired island-without-cloud was stranded
with no remote data and no way back short of re-pairing.

Two mechanisms fix that, both covered here:

1. `set_cloud_ingest {enabled: bool}` — flips ONLY the cloud sender, touching
   neither the island key nor island routing. This is the orthogonal control.
2. `enable_island` now honours an optional `cloudIngest` in its payload so
   island can be enabled while cloud upload is kept. Absent → False, which
   preserves the behaviour of every API that predates this change.

Assertions mirror test_command_poller_island.py: the success ACK MUST precede
the entry apply + reload, because the reload cancels the poller task that is
suspended on the ACK's network I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.signing import generate_keypair


def _make_api_client() -> MagicMock:
    c = MagicMock()
    c.ack_command = AsyncMock()
    return c


def _make_keystore() -> MagicMock:
    ks = MagicMock()
    ks.async_set_island_key = AsyncMock()
    ks.async_add_island_key = AsyncMock()
    return ks


def _make_hass_entry(entry_data: dict | None = None):
    hass = MagicMock()
    hass.is_stopping = False
    hass.config_entries = MagicMock()
    hass.async_create_task = MagicMock()

    entry = MagicMock()
    entry.data = entry_data if entry_data is not None else {"cloud_ingest_enabled": True}
    entry.entry_id = "e1"
    return hass, entry


async def _run(command, *, hass, entry, api_client, keystore):
    priv, _pub_hex = generate_keypair()
    await process_command(
        command=command,
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=keystore,
        hass=hass,
        entry=entry,
    )


# ---------------------------------------------------------------------------
# set_cloud_ingest — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_cloud_ingest_true_enables_sender_and_acks_success():
    """The recovery direction: a household stranded island-without-cloud gets
    its sender back WITHOUT re-pairing and without touching the island key."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": False, "island_key": "K"})

    call_order: list[str] = []
    api_client.ack_command.side_effect = lambda *a, **k: call_order.append("ack")
    hass.config_entries.async_update_entry.side_effect = lambda *a, **k: call_order.append("update")
    hass.async_create_task.side_effect = lambda *a, **k: call_order.append("reload")

    await _run(
        {
            "commandId": "c-ci-on",
            "command": "set_cloud_ingest",
            "payload": {"enabled": True},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    hass.config_entries.async_update_entry.assert_called_once()
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is True, (
        f"Expected cloud_ingest_enabled=True, got: {updated}"
    )
    # The island key must survive — this command is orthogonal to island mode.
    assert updated.get("island_key") == "K", (
        f"set_cloud_ingest must not disturb the island key, got: {updated}"
    )
    keystore.async_add_island_key.assert_not_awaited()
    keystore.async_set_island_key.assert_not_awaited()

    hass.async_create_task.assert_called_once()
    api_client.ack_command.assert_awaited_once()
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True
    assert call_order == ["ack", "update", "reload"], (
        f"Success ACK must precede apply+reload, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_set_cloud_ingest_false_disables_sender_and_keeps_island_key():
    """The privacy direction: stop uploading, keep island access intact."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": True, "island_key": "K"})

    await _run(
        {
            "commandId": "c-ci-off",
            "command": "set_cloud_ingest",
            "payload": {"enabled": False},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is False
    assert updated.get("island_key") == "K"
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True


# ---------------------------------------------------------------------------
# set_cloud_ingest — rejections (nothing may half-apply)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_cloud_ingest_rejects_non_bool_enabled_without_mutating_entry():
    """A truthiness coerce here would silently flip a user's data-routing
    choice from a malformed payload, so anything that isn't a real bool is
    rejected before the entry is touched."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": False})

    await _run(
        {
            "commandId": "c-ci-bad",
            "command": "set_cloud_ingest",
            "payload": {"enabled": "yes"},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    hass.config_entries.async_update_entry.assert_not_called()
    hass.async_create_task.assert_not_called()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True


@pytest.mark.asyncio
async def test_set_cloud_ingest_rejects_missing_enabled_without_mutating_entry():
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": False})

    await _run(
        {"commandId": "c-ci-none", "command": "set_cloud_ingest", "payload": {}},
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    hass.config_entries.async_update_entry.assert_not_called()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True


@pytest.mark.asyncio
async def test_set_cloud_ingest_rejects_when_no_config_entry():
    """YAML install (no ConfigEntry) → rejected ACK, no crash."""
    api_client = _make_api_client()
    keystore = _make_keystore()

    await _run(
        {
            "commandId": "c-ci-noentry",
            "command": "set_cloud_ingest",
            "payload": {"enabled": True},
        },
        hass=None,
        entry=None,
        api_client=api_client,
        keystore=keystore,
    )

    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True


# ---------------------------------------------------------------------------
# enable_island — honours the cloudIngest payload flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_island_with_cloud_ingest_true_keeps_sender_running():
    """Regression for the stranding bug: enabling island used to force cloud
    upload OFF regardless of what the payload asked for, so "keep sending to
    the cloud as well" was impossible to express."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": True})

    await _run(
        {
            "commandId": "c-island-cloud",
            "command": "enable_island",
            "payload": {"islandKey": "K", "cloudIngest": True},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    keystore.async_add_island_key.assert_awaited_once()
    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is True, (
        f"enable_island must honour cloudIngest=True, got: {updated}"
    )
    assert api_client.ack_command.await_args.kwargs["body"]["success"] is True


@pytest.mark.asyncio
async def test_enable_island_without_cloud_ingest_defaults_to_false():
    """Back-compat: an API that predates the flag sends no cloudIngest, and
    must keep getting pure island mode."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": True})

    await _run(
        {
            "commandId": "c-island-nocloud",
            "command": "enable_island",
            "payload": {"islandKey": "K"},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is False, (
        f"Absent cloudIngest must default to False, got: {updated}"
    )


@pytest.mark.asyncio
async def test_enable_island_with_non_bool_cloud_ingest_defaults_to_false():
    """Garbage in the flag must fail CLOSED (pure island), never open."""
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": True})

    await _run(
        {
            "commandId": "c-island-badcloud",
            "command": "enable_island",
            "payload": {"islandKey": "K", "cloudIngest": "yes"},
        },
        hass=hass,
        entry=entry,
        api_client=api_client,
        keystore=keystore,
    )

    updated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated.get("cloud_ingest_enabled") is False
