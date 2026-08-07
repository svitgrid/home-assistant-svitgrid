"""Regression: the admin trusted-key cache must survive a config-entry reload.

Two independent defects, both reported live on 2026-08-07 as
"Skipping command <id> — signingKeyId <phone key> not in trusted keys
(cache has 0)" on a household whose phone key was demonstrably approved
server-side:

1. `run_loop` seeded its cache from `entry.data["trusted_keys"]` looking for
   `signingKeyId` / `key_id`, but /finalize (and therefore config_flow) stores
   the items as `{"keyId": ..., "publicKeyHex": ...}`.  The seed therefore
   matched nothing and every config-entry install started with an EMPTY cache,
   however many keys the entry actually carried.

2. `async_setup_entry` re-wrote the keystore's `trusted_public_keys_hex` from
   that same pairing-time snapshot on every setup, discarding every key the
   poller had learned since via `add_trusted_key`.

Together: control commands worked only in the window between an
`add_trusted_key` arriving and the next reload (island toggle, cloud-ingest
toggle, add-on update, HA restart) — after which every signed command was
silently skipped, with no ACK and no user-visible error.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import async_setup_entry
from custom_components.svitgrid.command_poller import run_loop as poller_run_loop
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid.signing import generate_keypair, sign_payload

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}


def _pem(priv) -> str:
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _hass_one_iter():
    """A hass double whose `is_stopping` is False once, then True."""
    hass = MagicMock()
    n = {"i": 0}

    def _is_stopping(_self):
        n["i"] += 1
        return n["i"] > 1

    type(hass).is_stopping = property(_is_stopping)
    return hass


# --- Defect 1: the entry_data seed must read the shape /finalize actually sends ---


@pytest.mark.asyncio
async def test_poller_seeds_cache_from_finalize_shaped_trusted_keys(monkeypatch):
    """`entry.data["trusted_keys"]` items are `{keyId, publicKeyHex}` — the
    exact shape config_flow copies out of /finalize's `trustedKeys`.  A signed
    command from such a key must dispatch, not be skipped as untrusted."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-1", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-1",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=None,
        entry_data={
            "api_key": "k",
            "public_key_hex": our_pub_hex,
            "private_key_pem": _pem(our_priv),
            "signing_key_id": "our-key",
            # THE SHAPE THAT SHIPS — see config_flow.async_create_entry.
            "trusted_keys": [{"keyId": "admin-key-id", "publicKeyHex": admin_pub_hex}],
        },
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    executor.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_poller_prefers_keystore_over_pairing_snapshot(monkeypatch):
    """When a keystore is present it is the live trust store — keys learned
    after pairing live only there, so it must win over `entry_data`."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-2", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-2",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "learned-later",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    keystore = AsyncMock()
    keystore.load = AsyncMock(
        return_value=MagicMock(
            api_key="k",
            signing_key_id="our-key",
            trusted_public_keys_hex={"learned-later": admin_pub_hex},
            load_private_key=MagicMock(return_value=our_priv),
        )
    )

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=keystore,
        entry_data={
            "api_key": "k",
            "public_key_hex": our_pub_hex,
            "private_key_pem": _pem(our_priv),
            "signing_key_id": "our-key",
            # Pairing snapshot predates the key above.
            "trusted_keys": [],
        },
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    executor.dispatch.assert_awaited_once()


# --- Self-repair: pull the authoritative key set back from the server ---
#
# The two fixes above stop the cache being LOST, but cannot recover a household
# that has already lost it — the keystore is empty and the pairing snapshot is
# whatever it was. `GET /api/v3/executors/trusted-keys` is the pull half of a
# push-only design, so an install repairs itself with nobody touching prod.


def _entry_data_for(our_priv, our_pub_hex, trusted_keys=None) -> dict:
    return {
        "api_key": "k",
        "public_key_hex": our_pub_hex,
        "private_key_pem": _pem(our_priv),
        "signing_key_id": "our-key",
        "trusted_keys": trusted_keys if trusted_keys is not None else [],
    }


@pytest.mark.asyncio
async def test_poller_resyncs_trusted_keys_from_server_at_startup(monkeypatch):
    """An install with nothing cached anywhere still executes a signed command:
    the poller asks the server for the household's approved keys first."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-3", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-3",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "server-only-key",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    api.get_trusted_keys = AsyncMock(
        return_value=[{"keyId": "server-only-key", "publicKeyHex": admin_pub_hex}]
    )
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    # The reporting household's real state: a keystore that exists (it holds
    # the API key and private key) but whose trusted map was emptied.
    keystore = AsyncMock()
    keystore.load = AsyncMock(
        return_value=MagicMock(
            api_key="k",
            signing_key_id="our-key",
            trusted_public_keys_hex={},
            load_private_key=MagicMock(return_value=our_priv),
        )
    )
    keystore.update_trusted_keys_hex = AsyncMock()

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=keystore,
        entry_data=_entry_data_for(our_priv, our_pub_hex),
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    api.get_trusted_keys.assert_awaited_once()
    executor.dispatch.assert_awaited_once()
    # The recovered set is persisted, so the next start needs no round trip.
    keystore.update_trusted_keys_hex.assert_awaited_with({"server-only-key": admin_pub_hex})


@pytest.mark.asyncio
async def test_resync_failure_leaves_the_existing_cache_intact(monkeypatch):
    """Old API (404) or a flaky network must not disarm a working install."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-4", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-4",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    api.get_trusted_keys = AsyncMock(side_effect=RuntimeError("404 Not Found"))
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=None,
        entry_data=_entry_data_for(
            our_priv,
            our_pub_hex,
            [{"keyId": "admin-key-id", "publicKeyHex": admin_pub_hex}],
        ),
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    executor.dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_resync_drops_a_key_missing_from_a_non_empty_server_set(monkeypatch):
    """A non-empty response is authoritative and complete, so the resync also
    propagates revocations — it is a replace, not a merge."""
    revoked_priv, revoked_pub_hex = generate_keypair()
    _, survivor_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-5", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-5",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, revoked_priv),
        "signingKeyId": "revoked-key",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    api.get_trusted_keys = AsyncMock(
        return_value=[{"keyId": "survivor-key", "publicKeyHex": survivor_pub_hex}]
    )
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    keystore = AsyncMock()
    keystore.load = AsyncMock(
        return_value=MagicMock(
            api_key="k",
            signing_key_id="our-key",
            trusted_public_keys_hex={"revoked-key": revoked_pub_hex},
            load_private_key=MagicMock(return_value=our_priv),
        )
    )
    keystore.update_trusted_keys_hex = AsyncMock()

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=keystore,
        entry_data=_entry_data_for(our_priv, our_pub_hex),
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    executor.dispatch.assert_not_awaited()
    keystore.update_trusted_keys_hex.assert_awaited_with({"survivor-key": survivor_pub_hex})


@pytest.mark.asyncio
async def test_an_empty_server_set_never_empties_a_populated_cache(monkeypatch):
    """The one response that could disarm control fleet-wide if the endpoint
    ever got it wrong. Revocation has its own push path (`revoke_trusted_key`),
    so refusing to act on an empty set costs nothing."""
    admin_priv, admin_pub_hex = generate_keypair()
    our_priv, our_pub_hex = generate_keypair()

    signed_event_data = {"commandId": "cmd-6", "command": "set_battery_charge"}
    command = {
        "commandId": "cmd-6",
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin_priv),
        "signingKeyId": "admin-key-id",
        "signedEventData": signed_event_data,
    }

    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": [command]})
    api.ack_command = AsyncMock()
    api.get_trusted_keys = AsyncMock(return_value=[])
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})

    keystore = AsyncMock()
    keystore.load = AsyncMock(
        return_value=MagicMock(
            api_key="k",
            signing_key_id="our-key",
            trusted_public_keys_hex={"admin-key-id": admin_pub_hex},
            load_private_key=MagicMock(return_value=our_priv),
        )
    )
    keystore.update_trusted_keys_hex = AsyncMock()

    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await poller_run_loop(
        hass=_hass_one_iter(),
        api_client=api,
        keystore=keystore,
        entry_data=_entry_data_for(our_priv, our_pub_hex),
        executors_by_inverter={"ha-aaa": executor},
        wake_event=None,
    )

    executor.dispatch.assert_awaited_once()
    keystore.update_trusted_keys_hex.assert_not_awaited()


# --- Defect 2: setup must not discard keys the poller learned after pairing ---


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "edge1",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            # Paired BEFORE the phone registered its key — the real ordering on
            # the reporting household (integration 20:58:05, phone 21:03:14).
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-aaa",
                    "entity_map": {"batterySoc": "sensor.a"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "X",
                    "phases": 1,
                    "has_battery": True,
                    "pv_strings": 1,
                    "preset_id": None,
                }
            ],
        },
    )


@pytest.mark.asyncio
async def test_setup_entry_keeps_keys_learned_via_add_trusted_key(hass):
    """A key added by `add_trusted_key` lives only in the keystore.  Setting the
    entry up again (reload / restart) must not roll it back to the pairing-time
    snapshot in entry.data."""
    keystore = SvitgridKeystore(hass)
    await keystore.save(
        api_key="k",
        public_key_hex="pub",
        private_key_pem="pem",
        signing_key_id="sk",
        trusted_key_ids=["phone-key"],
        trusted_public_keys_hex={"phone-key": "04" + "ab" * 64},
    )

    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch("custom_components.svitgrid.run_readings_loop", return_value=None),
        patch("custom_components.svitgrid.run_command_loop", return_value=None),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", return_value=None),
        patch("custom_components.svitgrid.run_sender_loop", return_value=None),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=True),
    ):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    state = await SvitgridKeystore(hass).load()
    assert state is not None
    assert state.trusted_public_keys_hex == {"phone-key": "04" + "ab" * 64}
