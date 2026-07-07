"""Tests for enable_island / disable_island command handlers (Task 1).

These are "special" internal commands — trusted via the command channel
(no ECDSA admin-signature required), handled in Arm 1d alongside
set_cloud_endpoint (Arm 1c).

Assertions:
- enable_island: keystore.async_set_island_key seeded, entry updated with
  cloud_ingest_enabled=False, reload scheduled, success ACK.
- disable_island: entry updated with cloud_ingest_enabled=True, keystore
  island_key NOT cleared, success ACK.
- enable_island hass/entry None -> rejected ACK, no crash, no keystore write.
- enable_island keystore None -> rejected ACK, entry NOT mutated, no reload.
- enable_island missing/empty islandKey -> rejected ACK.
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
    return ks


def _make_hass_entry(entry_data: dict | None = None):
    """Return (hass_mock, entry_mock) pair suitable for island handler tests."""
    hass = MagicMock()
    hass.is_stopping = False
    hass.config_entries = MagicMock()
    hass.async_create_task = MagicMock()

    entry = MagicMock()
    entry.data = entry_data if entry_data is not None else {"cloud_ingest_enabled": True}
    entry.entry_id = "e1"
    return hass, entry


# ---------------------------------------------------------------------------
# enable_island happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_island_seeds_keystore_updates_entry_and_acks_success():
    """enable_island with a valid islandKey:
    - calls keystore.async_set_island_key('K')
    - ACKs success=True BEFORE applying the config change + reload
    - calls hass.config_entries.async_update_entry with cloud_ingest_enabled=False
    - schedules a reload via hass.async_create_task
    """
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry()

    # Record relative ordering of the ACK vs the entry-apply/reload. The reload
    # tears down the poller task, so the success ACK MUST be sent first.
    call_order: list[str] = []
    api_client.ack_command.side_effect = lambda *a, **k: call_order.append("ack")
    hass.config_entries.async_update_entry.side_effect = lambda *a, **k: call_order.append("update")
    hass.async_create_task.side_effect = lambda *a, **k: call_order.append("reload")

    await process_command(
        command={
            "commandId": "c-enable",
            "command": "enable_island",
            "payload": {"islandKey": "K", "cloudIngest": False},
        },
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

    # Keystore seeded with the island key
    keystore.async_set_island_key.assert_awaited_once_with("K")

    # Entry updated with cloud_ingest_enabled=False
    hass.config_entries.async_update_entry.assert_called_once()
    updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data.get("cloud_ingest_enabled") is False, (
        f"Expected cloud_ingest_enabled=False in update, got: {updated_data}"
    )

    # Reload scheduled
    hass.async_create_task.assert_called_once()

    # Success ACK
    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is True, f"Expected success ACK, got: {body}"

    # ACK must precede the entry apply + reload (reload cancels the poller task).
    assert call_order == ["ack", "update", "reload"], (
        f"Success ACK must be sent BEFORE apply+reload, got: {call_order}"
    )


# ---------------------------------------------------------------------------
# disable_island happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_island_sets_cloud_ingest_true_keeps_key_and_acks():
    """disable_island:
    - does NOT call keystore.async_set_island_key (island key retained)
    - ACKs success=True BEFORE applying the config change + reload
    - calls hass.config_entries.async_update_entry with cloud_ingest_enabled=True
    - schedules a reload
    """
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry({"cloud_ingest_enabled": False, "island_key": "old-key"})

    call_order: list[str] = []
    api_client.ack_command.side_effect = lambda *a, **k: call_order.append("ack")
    hass.config_entries.async_update_entry.side_effect = lambda *a, **k: call_order.append("update")
    hass.async_create_task.side_effect = lambda *a, **k: call_order.append("reload")

    await process_command(
        command={
            "commandId": "c-disable",
            "command": "disable_island",
            "payload": {"cloudIngest": True},
        },
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

    # Keystore island key must NOT be touched
    keystore.async_set_island_key.assert_not_awaited()

    # Entry updated with cloud_ingest_enabled=True
    hass.config_entries.async_update_entry.assert_called_once()
    updated_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert updated_data.get("cloud_ingest_enabled") is True, (
        f"Expected cloud_ingest_enabled=True in update, got: {updated_data}"
    )

    # Reload scheduled
    hass.async_create_task.assert_called_once()

    # Success ACK
    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is True, f"Expected success ACK, got: {body}"

    # ACK must precede the entry apply + reload (reload cancels the poller task).
    assert call_order == ["ack", "update", "reload"], (
        f"Success ACK must be sent BEFORE apply+reload, got: {call_order}"
    )


# ---------------------------------------------------------------------------
# None guard — rejected ACK, no crash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_island_rejects_when_hass_is_none():
    """enable_island with hass=None/entry=None → rejected ACK, no crash,
    keystore must not be mutated."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()

    await process_command(
        command={
            "commandId": "c-no-hass",
            "command": "enable_island",
            "payload": {"islandKey": "K", "cloudIngest": False},
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=keystore,
        hass=None,
        entry=None,
    )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False, "Must not ack success when hass/entry is None"
    assert body["rejected"] is True
    keystore.async_set_island_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_disable_island_rejects_when_hass_is_none():
    """disable_island with hass=None/entry=None → rejected ACK, no crash."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()

    await process_command(
        command={
            "commandId": "c-dis-no-hass",
            "command": "disable_island",
            "payload": {"cloudIngest": True},
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=keystore,
        hass=None,
        entry=None,
    )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True


# ---------------------------------------------------------------------------
# enable_island — missing / empty islandKey
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_island_rejects_when_island_key_missing():
    """enable_island with no islandKey in payload → rejected ACK, keystore untouched."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry()

    await process_command(
        command={
            "commandId": "c-no-key",
            "command": "enable_island",
            "payload": {"cloudIngest": False},  # no islandKey
        },
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

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    keystore.async_set_island_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_enable_island_rejects_when_keystore_none_before_mutating_entry():
    """hass+entry valid but keystore None → rejected ACK; entry must NOT be
    mutated and no reload scheduled (otherwise the add-on comes up with
    cloud-ingest OFF and no island key → every island call 401s)."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    hass, entry = _make_hass_entry()

    await process_command(
        command={
            "commandId": "c-no-keystore",
            "command": "enable_island",
            "payload": {"islandKey": "K", "cloudIngest": False},
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=None,
        hass=hass,
        entry=entry,
    )

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False, "Must not ack success without a keystore"
    assert body["rejected"] is True
    assert body["reason"] == "keystore_unavailable"
    # Entry must be untouched — no half-applied cloud_ingest flip, no reload
    hass.config_entries.async_update_entry.assert_not_called()
    hass.async_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_enable_island_rejects_when_island_key_empty():
    """enable_island with empty string islandKey → rejected ACK."""
    priv, _pub_hex = generate_keypair()
    api_client = _make_api_client()
    keystore = _make_keystore()
    hass, entry = _make_hass_entry()

    await process_command(
        command={
            "commandId": "c-empty-key",
            "command": "enable_island",
            "payload": {"islandKey": "", "cloudIngest": False},
        },
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

    api_client.ack_command.assert_awaited_once()
    body = api_client.ack_command.await_args.kwargs["body"]
    assert body["success"] is False
    assert body["rejected"] is True
    keystore.async_set_island_key.assert_not_awaited()
