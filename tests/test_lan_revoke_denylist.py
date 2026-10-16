"""A key revoked over the LAN stays revoked (security fix, 2026-10-15).

Two holes let a revoked phone key keep signing commands:

1. The command poller copied the trusted keys into memory once, at start.
   `POST /api/svitgrid/keys/revoke` wrote only the keystore, so while the cloud
   copy was on, the poller kept accepting commands signed with the revoked key
   until the entry reloaded.
2. Every poller start replaced the trusted keys with the server's approved
   list. A LAN revoke never reaches the server, so that list still held the
   key, and a reload trusted it again.

The keystore now tells the running poller about every change to the trusted
keys, and it keeps the ids revoked over the LAN in `.storage/svitgrid`. No
resync, from startup or from an unknown key, trusts a revoked id again. Only a
deliberate step does: the owner approving the phone in a LAN pairing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import async_remove_entry
from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.command_poller import run_loop as poller_run_loop
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.http_views import (
    SvitgridTrustKeyDetailView,
    SvitgridTrustKeyView,
)
from custom_components.svitgrid.island_lan import SvitgridKeysRevokeView
from custom_components.svitgrid.keystore import (
    ENTRY_ISLAND_DEVICE_IDS,
    ENTRY_TRUSTED_KEY_IDS,
    SvitgridKeystore,
)
from custom_components.svitgrid.local_pairing import ROLE_CONTROL, ROLE_VIEW, LanClaim, add_phone
from custom_components.svitgrid.signing import generate_keypair, sign_payload
from tests.test_island_lan import ISLAND_KEY, NOW_MS, OTHER_ISLAND_KEY, _Admin, _body, _Req


@pytest.fixture(autouse=True)
def _pinned_clock():
    with patch("custom_components.svitgrid.island_lan._now_ms", return_value=NOW_MS):
        yield


def _pem(priv) -> str:
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


async def _station(hass, *admins: _Admin) -> tuple[SvitgridKeystore, MockConfigEntry, object]:
    """A cloud-paired station with the cloud copy on: the poller runs."""
    our_priv, our_pub = generate_keypair()
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="cloud-api-key",
        public_key_hex=our_pub,
        private_key_pem=_pem(our_priv),
        signing_key_id="ha-1",
        trusted_key_ids=[a.key_id for a in admins],
        trusted_public_keys_hex={a.key_id: a.pub for a in admins},
    )
    await ks.async_add_island_key("phone-1", ISLAND_KEY, label="Pixel 8")
    hass.data.setdefault(DOMAIN, {})["keystore"] = ks
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"api_key": "k", "edge_device_id": "ed-1", "cloud_ingest_enabled": True},
    )
    entry.add_to_hass(hass)
    return ks, entry, our_priv


def _signed_command(admin: _Admin, cmd_id: str) -> dict:
    signed_event_data = {"commandId": cmd_id, "command": "set_battery_charge"}
    return {
        "commandId": cmd_id,
        "command": "set_battery_charge",
        "payload": {"inverterId": "ha-aaa", "enabled": True},
        "signature": sign_payload(signed_event_data, admin.priv),
        "signingKeyId": admin.key_id,
        "signedEventData": signed_event_data,
    }


def _executor():
    executor = MagicMock()
    executor.dispatch = AsyncMock(return_value={"ok": True})
    return executor


async def _revoke(hass, signer: _Admin, key_id: str):
    return await SvitgridKeysRevokeView().post(
        _Req(hass, signer.request("revoke_key", keyId=key_id))
    )


async def _run_one_poll(hass, ks, api, executor, *, before_commands=None) -> None:
    """Start the poller, let it poll once, then stop it.

    `before_commands` runs inside that poll, after the poller has started and
    loaded its trusted keys, and before it judges the commands.
    """
    lifecycle = SimpleNamespace(active=True)
    wake = asyncio.Event()
    commands = api.poll_commands.return_value

    async def _poll(**_kwargs):
        lifecycle.active = False
        wake.set()
        if before_commands is not None:
            await before_commands()
        return commands

    api.poll_commands = AsyncMock(side_effect=_poll)
    await asyncio.wait_for(
        poller_run_loop(
            hass=hass,
            api_client=api,
            keystore=ks,
            entry_data={"api_key": "k"},
            executors_by_inverter={"ha-aaa": executor},
            wake_event=wake,
            lifecycle=lifecycle,
        ),
        timeout=5,
    )


def _api(server_keys: list[_Admin], commands: list[dict]) -> MagicMock:
    api = MagicMock()
    api.poll_commands = AsyncMock(return_value={"commands": commands})
    api.ack_command = AsyncMock()
    api.get_trusted_keys = AsyncMock(
        return_value=[{"keyId": a.key_id, "publicKeyHex": a.pub} for a in server_keys]
    )
    return api


# ── the running poller ──────────────────────────────────────────────────────


async def test_a_lan_revoke_reaches_the_running_poller_without_a_reload(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    api = _api([owner, phone], [_signed_command(phone, "cmd-after-revoke")])
    executor = _executor()

    async def _revoke_mid_run():
        resp = await _revoke(hass, owner, phone.key_id)
        assert resp.status == 200, _body(resp)

    await _run_one_poll(hass, ks, api, executor, before_commands=_revoke_mid_run)

    executor.dispatch.assert_not_awaited()


async def test_the_poller_still_runs_commands_of_a_key_that_was_not_revoked(
    hass: HomeAssistant,
):
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    api = _api([owner, phone], [_signed_command(owner, "cmd-owner")])
    executor = _executor()

    async def _revoke_mid_run():
        await _revoke(hass, owner, phone.key_id)

    await _run_one_poll(hass, ks, api, executor, before_commands=_revoke_mid_run)

    executor.dispatch.assert_awaited_once()


async def test_a_startup_resync_does_not_trust_a_revoked_key_again(hass: HomeAssistant):
    """The server never heard of the LAN revoke and still lists the key."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    assert (await _revoke(hass, owner, phone.key_id)).status == 200

    api = _api([owner, phone], [_signed_command(phone, "cmd-after-restart")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    state = await ks.load()
    assert phone.key_id not in state.trusted_public_keys_hex
    assert owner.key_id in state.trusted_public_keys_hex


async def test_an_unknown_key_resync_does_not_trust_a_revoked_key_again(hass: HomeAssistant):
    """A command from the revoked key triggers the unknown-key resync, which
    must not hand the key back either."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    assert (await _revoke(hass, owner, phone.key_id)).status == 200

    api = _api([owner, phone], [_signed_command(phone, "cmd-unknown")])
    executor = _executor()
    with patch("custom_components.svitgrid.command_poller.TRUSTED_KEY_RESYNC_MIN_INTERVAL_S", 0):
        await _run_one_poll(hass, ks, api, executor)

    assert api.get_trusted_keys.await_count == 2
    executor.dispatch.assert_not_awaited()
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


# ── the denylist ────────────────────────────────────────────────────────────


async def test_the_revoked_id_is_persisted(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)

    # A fresh instance reads `.storage/svitgrid`, as after a restart.
    assert phone.key_id in (await SvitgridKeystore(hass).load()).revoked_key_ids


async def test_a_refused_revoke_records_nothing(hass: HomeAssistant):
    owner = _Admin()
    ks, _entry, _ = await _station(hass, owner)
    assert (await _revoke(hass, owner, owner.key_id)).status == 409
    assert (await ks.load()).revoked_key_ids == []


async def test_the_keystore_never_stores_a_revoked_key_as_trusted(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)

    await ks.update_trusted_keys_hex({owner.key_id: owner.pub, phone.key_id: phone.pub})

    assert set((await ks.load()).trusted_public_keys_hex) == {owner.key_id}


async def test_a_cloud_add_trusted_key_does_not_clear_a_lan_revoke(hass: HomeAssistant):
    """The cloud cannot tell an owner's approval from the app's automatic
    re-push of an approved key, so its `add_trusted_key` is refused for a key
    revoked here, and the refusal is acknowledged as a failure."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, our_priv = await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)
    trusted = dict((await ks.load()).trusted_public_keys_hex)
    api = _api([], [])

    await process_command(
        command={
            "commandId": "add-1",
            "command": "add_trusted_key",
            "payload": {"signingKeyId": phone.key_id, "publicKeyHex": phone.pub},
        },
        api_client=api,
        api_key="k",
        trusted_public_keys_hex=trusted,
        our_private_key=our_priv,
        our_signing_key_id="ha-1",
        executor_version="t",
        keystore=ks,
    )

    assert phone.key_id not in trusted
    state = await ks.load()
    assert phone.key_id not in state.trusted_public_keys_hex
    assert phone.key_id in state.revoked_key_ids
    ack = api.ack_command.await_args.kwargs["body"]
    assert ack["success"] is False
    assert ack["reason"] == "key_revoked_locally"


# ── re-trusting takes a deliberate step ─────────────────────────────────────


def _claim_for(phone: _Admin, device_id: str = "phone-2") -> LanClaim:
    return LanClaim(
        island_key=OTHER_ISLAND_KEY,
        device_id=device_id,
        device_label="iPhone",
        signing_key_id=phone.key_id,
        public_key_hex=phone.pub,
        station_name=None,
        preset_id=None,
        inverters=[],
        paired_at="2026-10-15T10:00:00Z",
    )


async def test_a_lan_pairing_of_the_same_key_trusts_it_again(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)

    role = await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)

    assert role == ROLE_CONTROL
    state = await ks.load()
    assert phone.key_id in state.trusted_public_keys_hex
    assert phone.key_id not in state.revoked_key_ids


async def test_a_re_paired_key_survives_a_startup_resync(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)
    await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)

    api = _api([owner, phone], [_signed_command(phone, "cmd-re-paired")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_awaited_once()


async def test_a_lan_pairing_reaches_the_running_poller(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner)
    api = _api([owner], [_signed_command(phone, "cmd-new-phone")])
    executor = _executor()

    async def _pair_mid_run():
        await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)

    await _run_one_poll(hass, ks, api, executor, before_commands=_pair_mid_run)

    executor.dispatch.assert_awaited_once()


# ── the island-key trust endpoints ──────────────────────────────────────────


async def test_deleting_a_trusted_key_over_the_lan_also_revokes_it(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)

    req = _Req(hass, owner.request("revoke_key", keyId=phone.key_id))
    resp = await SvitgridTrustKeyDetailView().delete(req, phone.key_id)

    assert resp.status == 200
    assert phone.key_id in (await ks.load()).revoked_key_ids


async def test_trust_key_refuses_a_revoked_key(hass: HomeAssistant):
    """Even a trusted admin key cannot trust a key revoked over the LAN
    through `/trust-key`. That takes the owner approving a LAN pairing."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    await _revoke(hass, owner, phone.key_id)

    body = owner.request("trust_key", keyId=phone.key_id, publicKeyHex=phone.pub)
    resp = await SvitgridTrustKeyView().post(_Req(hass, body))

    assert resp.status == 409
    assert _body(resp) == {"error": "key_revoked"}
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


# ── removing an entry ───────────────────────────────────────────────────────


async def test_removing_an_entry_untrusts_the_keys_linked_to_its_phones(hass: HomeAssistant):
    """A key linked to one of the entry's phones goes with the entry, even when
    the pairing did not record it in `trusted_key_ids_granted`."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    await ks.async_add_island_key("phone-2", OTHER_ISLAND_KEY)
    await ks.async_link_signing_key(phone.key_id, "phone-2")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"local_only": True, ENTRY_ISLAND_DEVICE_IDS: ["phone-2"]},
    )
    entry.add_to_hass(hass)

    await async_remove_entry(hass, entry)

    state = await ks.load()
    assert phone.key_id not in state.trusted_public_keys_hex
    assert phone.key_id not in state.key_devices
    assert owner.key_id in state.trusted_public_keys_hex


async def test_removing_an_entry_keeps_the_last_admin_key_of_another_entry(
    hass: HomeAssistant,
):
    """The trusted keys belong to the install. When the removed entry's key is
    the only one, the station that remains still needs it to sign anything."""
    phone = _Admin()
    ks, _cloud_entry, _ = await _station(hass, phone)
    await ks.async_link_signing_key(phone.key_id, "phone-1")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "local_only": True,
            ENTRY_ISLAND_DEVICE_IDS: ["phone-1"],
            ENTRY_TRUSTED_KEY_IDS: [phone.key_id],
        },
    )
    entry.add_to_hass(hass)

    await async_remove_entry(hass, entry)

    assert phone.key_id in (await ks.load()).trusted_public_keys_hex


async def test_the_keystore_notifies_and_unsubscribes(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    seen: list[set[str]] = []
    unsubscribe = ks.subscribe_trust(lambda keys: seen.append(set(keys)))

    await _revoke(hass, owner, phone.key_id)
    unsubscribe()
    await ks.update_trusted_keys_hex({owner.key_id: owner.pub})

    assert seen == [{owner.key_id}]


# ── a revoke from the cloud ─────────────────────────────────────────────────


def _cloud_revoke(cmd_id: str, key_id: str) -> dict:
    """As the API writes it: `routes/v2/key-management.ts` and
    `services/trusted-device-revoke.ts` put the key in `keyIdToRevoke`."""
    return {
        "commandId": cmd_id,
        "command": "revoke_trusted_key",
        "payload": {"keyIdToRevoke": key_id},
    }


async def test_a_cloud_revoke_goes_on_the_denylist_and_is_acknowledged(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, _entry, our_priv = await _station(hass, owner, phone)
    trusted = dict((await ks.load()).trusted_public_keys_hex)
    api = _api([], [])

    await process_command(
        command=_cloud_revoke("revoke-1", phone.key_id),
        api_client=api,
        api_key="k",
        trusted_public_keys_hex=trusted,
        our_private_key=our_priv,
        our_signing_key_id="ha-1",
        executor_version="t",
        keystore=ks,
    )

    assert phone.key_id not in trusted
    # A fresh instance reads `.storage/svitgrid`, as after a restart.
    state = await SvitgridKeystore(hass).load()
    assert phone.key_id not in state.trusted_public_keys_hex
    assert phone.key_id in state.revoked_key_ids
    assert owner.key_id in state.trusted_public_keys_hex
    ack = api.ack_command.await_args.kwargs["body"]
    assert ack["success"] is True
    assert ack["result"] == {"revokedKeyId": phone.key_id}


async def test_a_cloud_revoke_reaches_the_running_poller_at_once(hass: HomeAssistant):
    """The revoke and a command signed by the revoked key arrive in one poll.
    The command must not run."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, phone)
    api = _api(
        [owner, phone],
        [_cloud_revoke("revoke-1", phone.key_id), _signed_command(phone, "cmd-after")],
    )
    executor = _executor()

    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    acks = [c.kwargs["body"] for c in api.ack_command.await_args_list]
    assert {"success": True, "result": {"revokedKeyId": phone.key_id}}.items() <= acks[0].items()


async def test_a_resync_does_not_trust_a_key_the_cloud_revoked(hass: HomeAssistant):
    """The server's approved list can lag the revoke it sent."""
    owner, phone = _Admin(), _Admin()
    ks, _entry, our_priv = await _station(hass, owner, phone)
    await process_command(
        command=_cloud_revoke("revoke-1", phone.key_id),
        api_client=_api([], []),
        api_key="k",
        trusted_public_keys_hex=dict((await ks.load()).trusted_public_keys_hex),
        our_private_key=our_priv,
        our_signing_key_id="ha-1",
        executor_version="t",
        keystore=ks,
    )

    api = _api([owner, phone], [_signed_command(phone, "cmd-after-restart")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex


# ── a resync keeps the phones paired over the LAN ──────────────────────────


async def test_a_startup_resync_keeps_a_phone_paired_over_the_lan(hass: HomeAssistant):
    """The cloud never hears of a LAN pairing, so its approved list lacks the
    phone's key. Replacing the trusted keys with that list took control away
    from the phone at every restart with the cloud copy on."""
    owner, phone = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner)
    await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)

    api = _api([owner], [_signed_command(phone, "cmd-lan-phone")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_awaited_once()
    state = await ks.load()
    assert state.trusted_public_keys_hex[phone.key_id] == phone.pub
    assert owner.key_id in state.trusted_public_keys_hex


async def test_an_unknown_key_resync_keeps_a_phone_paired_over_the_lan(hass: HomeAssistant):
    owner, phone, newcomer = _Admin(), _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner)
    await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)

    api = _api(
        [owner],
        [_signed_command(newcomer, "cmd-new"), _signed_command(phone, "cmd-lan-phone")],
    )
    # The owner approves the newcomer in the cloud after the poller started.
    api.get_trusted_keys.side_effect = [
        [{"keyId": owner.key_id, "publicKeyHex": owner.pub}],
        [{"keyId": a.key_id, "publicKeyHex": a.pub} for a in (owner, newcomer)],
    ]
    executor = _executor()
    with patch("custom_components.svitgrid.command_poller.TRUSTED_KEY_RESYNC_MIN_INTERVAL_S", 0):
        await _run_one_poll(hass, ks, api, executor)

    assert api.get_trusted_keys.await_count == 2
    assert executor.dispatch.await_count == 2
    assert {owner.key_id, newcomer.key_id, phone.key_id} <= set(
        (await ks.load()).trusted_public_keys_hex
    )


async def test_a_resync_does_not_trust_a_view_only_phone(hass: HomeAssistant):
    """A view-only phone's key is linked to its roster row and was never
    trusted. Keeping LAN keys must not promote it."""
    owner, viewer = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner)
    await add_phone(hass, entry, _claim_for(viewer), ROLE_VIEW)

    api = _api([owner], [_signed_command(viewer, "cmd-viewer")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    assert viewer.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_a_resync_drops_a_key_trusted_with_no_phone(hass: HomeAssistant):
    """Only LAN-paired keys are kept. A key the cloud trusted and no longer
    lists goes, as before."""
    owner, cloud_key = _Admin(), _Admin()
    ks, _entry, _ = await _station(hass, owner, cloud_key)

    api = _api([owner], [_signed_command(cloud_key, "cmd-cloud-key")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    assert cloud_key.key_id not in (await ks.load()).trusted_public_keys_hex


async def test_a_resync_drops_a_lan_phone_that_was_revoked(hass: HomeAssistant):
    owner, phone = _Admin(), _Admin()
    ks, entry, _ = await _station(hass, owner)
    await add_phone(hass, entry, _claim_for(phone), ROLE_CONTROL)
    assert (await _revoke(hass, owner, phone.key_id)).status == 200

    api = _api([owner], [_signed_command(phone, "cmd-revoked-phone")])
    executor = _executor()
    await _run_one_poll(hass, ks, api, executor)

    executor.dispatch.assert_not_awaited()
    assert phone.key_id not in (await ks.load()).trusted_public_keys_hex
