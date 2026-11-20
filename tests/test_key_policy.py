"""The add-on's trusted-key rules match the S3 board's (svitgrid D-2336, D-2339).

The board's rules: `key_add_policy.c` and `key_command_auth.c` in the svitgrid
monorepo, as shipped in S3 2.182.0.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization

from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.key_policy import (
    FIRST_KEY_NEEDS_LOCAL_PAIRING,
    MAX_REPORTED_PREFIXES,
    decide_key_add,
    key_command_auth,
    key_store_report,
)
from custom_components.svitgrid.keystore import KeystoreState, SvitgridKeystore
from custom_components.svitgrid.signing import compute_key_id, generate_keypair, sign_payload


def _key():
    priv, pub = generate_keypair()
    return priv, pub, compute_key_id(pub)


def _add(pub: str, key_id: str, *, signer=None) -> dict:
    payload = {"publicKeyHex": pub, "signingKeyId": key_id}
    cmd = {"commandId": "c-add", "command": "add_trusted_key", "payload": payload}
    if signer is not None:
        priv, signer_id = signer
        cmd["signature"] = sign_payload(payload, priv)
        cmd["signerKeyId"] = signer_id
    return cmd


# ── decide_key_add ─────────────────────────────────────────────────────────


def test_a_held_key_is_a_success_that_needs_no_signature():
    _, pub, kid = _key()
    d = decide_key_add({kid: pub}, _add(pub, kid), tofu_open=False)
    assert d.outcome == "already_held"
    assert d.board_key_id == hashlib.sha256(bytes.fromhex(pub)).hexdigest()


def test_an_unsigned_new_key_is_refused_once_the_store_holds_one():
    _, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    d = decide_key_add({held_id: held_pub}, _add(new_pub, new_id), tofu_open=True)
    assert (d.outcome, d.reason) == ("reject", "bootstrap_only_when_empty")


def test_a_new_key_signed_by_a_held_key_is_added():
    held_priv, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    d = decide_key_add(
        {held_id: held_pub}, _add(new_pub, new_id, signer=(held_priv, held_id)), tofu_open=False
    )
    assert d.outcome == "proceed"


def test_a_signature_from_a_key_not_held_is_refused():
    _, held_pub, held_id = _key()
    stranger_priv, _, stranger_id = _key()
    _, new_pub, new_id = _key()
    d = decide_key_add(
        {held_id: held_pub},
        _add(new_pub, new_id, signer=(stranger_priv, stranger_id)),
        tofu_open=False,
    )
    assert (d.outcome, d.reason) == ("reject", "unknown_signer_key")


def test_a_signature_over_another_payload_is_refused():
    held_priv, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    cmd = _add(new_pub, new_id)
    cmd["signature"] = sign_payload({"publicKeyHex": "04" + "aa" * 64}, held_priv)
    cmd["signerKeyId"] = held_id
    d = decide_key_add({held_id: held_pub}, cmd, tofu_open=False)
    assert (d.outcome, d.reason) == ("reject", "invalid_signature")


def test_a_signature_that_is_not_base64_is_refused_as_encoding():
    _, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    cmd = _add(new_pub, new_id)
    cmd["signature"] = "not base64 !!"
    cmd["signerKeyId"] = held_id
    d = decide_key_add({held_id: held_pub}, cmd, tofu_open=False)
    assert (d.outcome, d.reason) == ("reject", "invalid_signature_encoding")


def test_a_signer_named_by_its_board_id_is_found():
    """The store files a key under the id the cloud named it by; a phone's id
    is its board id, but an integration's (`ha-…`) is not."""
    held_priv, held_pub, held_board_id = _key()
    _, new_pub, new_id = _key()
    d = decide_key_add(
        {"ha-1234abcd": held_pub},
        _add(new_pub, new_id, signer=(held_priv, held_board_id)),
        tofu_open=False,
    )
    assert d.outcome == "proceed"


def test_an_empty_store_takes_an_unsigned_first_key_only_while_tofu_is_open():
    _, pub, kid = _key()
    assert decide_key_add({}, _add(pub, kid), tofu_open=True).outcome == "proceed"
    closed = decide_key_add({}, _add(pub, kid), tofu_open=False)
    assert (closed.outcome, closed.reason) == ("reject", FIRST_KEY_NEEDS_LOCAL_PAIRING)


def test_this_integrations_own_key_does_not_count_as_a_held_key():
    """Its own key lets nobody else in, so the household's first phone still
    arrives as an unsigned first key, and the own key itself needs no
    signature."""
    _, own_pub, _ = _key()
    _, phone_pub, phone_id = _key()
    first_phone = decide_key_add(
        {"ha-me": own_pub}, _add(phone_pub, phone_id), tofu_open=True, own_public_key_hex=own_pub
    )
    assert first_phone.outcome == "proceed"
    own = decide_key_add(
        {phone_id: phone_pub}, _add(own_pub, "ha-me"), tofu_open=False, own_public_key_hex=own_pub
    )
    assert own.outcome == "proceed"


@pytest.mark.parametrize(
    ("public_key_hex", "reason"),
    [
        (None, "missing_public_key"),
        ("04" + "aa" * 10, "invalid_public_key_length"),
        ("zz" * 65, "invalid_public_key_hex"),
    ],
)
def test_a_malformed_public_key_is_refused_with_the_boards_reason(public_key_hex, reason):
    cmd = {"command": "add_trusted_key", "payload": {"publicKeyHex": public_key_hex}}
    d = decide_key_add({}, cmd, tofu_open=True)
    assert (d.outcome, d.reason) == ("reject", reason)


# ── key_command_auth ───────────────────────────────────────────────────────


def test_key_auth_names_the_held_key_that_signed_a_revoke():
    priv, pub, kid = _key()
    payload = {"keyIdToRevoke": "other"}
    cmd = {"payload": payload, "signature": sign_payload(payload, priv), "signerKeyId": kid}
    assert key_command_auth({kid: pub}, cmd) == {"signed": True, "signerKeyId": kid}


def test_key_auth_checks_a_clear_with_no_payload_as_an_empty_object():
    priv, pub, kid = _key()
    cmd = {"signature": sign_payload({}, priv), "signerKeyId": kid}
    assert key_command_auth({kid: pub}, cmd) == {"signed": True, "signerKeyId": kid}


@pytest.mark.parametrize("variant", ["unsigned", "unknown_signer", "wrong_payload"])
def test_key_auth_reads_unsigned_otherwise(variant):
    priv, pub, kid = _key()
    stranger_priv, _, stranger_id = _key()
    payload = {"keyIdToRevoke": "other"}
    cmd: dict = {"payload": payload}
    if variant == "unknown_signer":
        cmd |= {"signature": sign_payload(payload, stranger_priv), "signerKeyId": stranger_id}
    elif variant == "wrong_payload":
        cmd |= {"signature": sign_payload({"keyIdToRevoke": "x"}, priv), "signerKeyId": kid}
    assert key_command_auth({kid: pub}, cmd) == {"signed": False}


# ── key_store_report ───────────────────────────────────────────────────────


def test_key_store_report_has_the_boards_shape():
    _, pub_a, _ = _key()
    _, pub_b, _ = _key()
    report = key_store_report({"ha-aaaa": pub_a, "phone": pub_b})
    assert report["trustedKeyCount"] == 2
    assert report["trustedKeyIdPrefixes"] == sorted(
        hashlib.sha256(bytes.fromhex(p)).hexdigest()[:8] for p in (pub_a, pub_b)
    )
    assert key_store_report({}) == {"trustedKeyCount": 0}


def test_key_store_report_never_lists_more_prefixes_than_the_api_takes():
    store = {f"k{i}": generate_keypair()[1] for i in range(MAX_REPORTED_PREFIXES + 3)}
    report = key_store_report(store)
    assert report["trustedKeyCount"] == MAX_REPORTED_PREFIXES + 3
    assert len(report["trustedKeyIdPrefixes"]) == MAX_REPORTED_PREFIXES


# ── process_command ────────────────────────────────────────────────────────


def _state(priv, pub, trusted: dict, *, tofu_closed: bool) -> KeystoreState:
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return KeystoreState(
        api_key="k",
        public_key_hex=pub,
        private_key_pem=pem,
        signing_key_id="ha-me",
        trusted_key_ids=list(trusted),
        trusted_public_keys_hex=dict(trusted),
        tofu_closed=tofu_closed,
    )


async def _run(command, cache, state):
    our_priv = state.load_private_key()
    api = MagicMock()
    api.ack_command = AsyncMock()
    keystore = AsyncMock()
    keystore.load = AsyncMock(return_value=state)
    await process_command(
        command=command,
        api_client=api,
        api_key="k",
        trusted_public_keys_hex=cache,
        our_private_key=our_priv,
        our_signing_key_id="ha-me",
        executor_version="0.35.0",
        keystore=keystore,
    )
    return api.ack_command.call_args.kwargs["body"], keystore


async def test_unsigned_add_on_a_store_with_keys_is_refused_and_nothing_is_written():
    our_priv, our_pub = generate_keypair()
    _, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    cache = {held_id: held_pub}
    body, keystore = await _run(
        _add(new_pub, new_id), cache, _state(our_priv, our_pub, cache, tofu_closed=True)
    )
    assert body["success"] is False
    assert body["rejected"] is True
    assert body["reason"] == "bootstrap_only_when_empty"
    assert cache == {held_id: held_pub}
    keystore.update_trusted_keys_hex.assert_not_awaited()


async def test_signed_add_is_trusted_and_acked_in_the_boards_shape():
    our_priv, our_pub = generate_keypair()
    held_priv, held_pub, held_id = _key()
    _, new_pub, new_id = _key()
    cache = {held_id: held_pub}
    body, keystore = await _run(
        _add(new_pub, new_id, signer=(held_priv, held_id)),
        cache,
        _state(our_priv, our_pub, cache, tofu_closed=True),
    )
    assert body["success"] is True
    assert body["result"] == {"addedKeyId": new_id, "keyAdded": True, "keyId": new_id}
    assert cache == {held_id: held_pub, new_id: new_pub}
    keystore.update_trusted_keys_hex.assert_awaited_once()


async def test_a_held_key_re_pushed_unsigned_acks_already_held():
    our_priv, our_pub = generate_keypair()
    _, held_pub, held_id = _key()
    cache = {held_id: held_pub}
    body, keystore = await _run(
        _add(held_pub, held_id), cache, _state(our_priv, our_pub, cache, tofu_closed=True)
    )
    assert body["success"] is True
    assert body["result"]["keyAdded"] is False
    assert body["result"]["alreadyHeld"] is True
    keystore.update_trusted_keys_hex.assert_not_awaited()


async def test_an_emptied_store_refuses_an_unsigned_first_key():
    our_priv, our_pub = generate_keypair()
    _, new_pub, new_id = _key()
    cache: dict[str, str] = {}
    body, _ = await _run(
        _add(new_pub, new_id), cache, _state(our_priv, our_pub, cache, tofu_closed=True)
    )
    assert body["reason"] == FIRST_KEY_NEEDS_LOCAL_PAIRING
    assert cache == {}


async def test_a_store_that_never_held_a_key_takes_an_unsigned_first_key():
    our_priv, our_pub = generate_keypair()
    _, new_pub, new_id = _key()
    cache: dict[str, str] = {}
    body, _ = await _run(
        _add(new_pub, new_id), cache, _state(our_priv, our_pub, cache, tofu_closed=False)
    )
    assert body["success"] is True
    assert cache == {new_id: new_pub}


async def test_a_signed_revoke_reports_its_signer_on_the_ack():
    our_priv, our_pub = generate_keypair()
    priv_a, pub_a, id_a = _key()
    _, pub_b, id_b = _key()
    payload = {"keyIdToRevoke": id_b}
    cmd = {
        "commandId": "c-rev",
        "command": "revoke_trusted_key",
        "payload": payload,
        "signature": sign_payload(payload, priv_a),
        "signerKeyId": id_a,
    }
    cache = {id_a: pub_a, id_b: pub_b}
    body, _ = await _run(cmd, cache, _state(our_priv, our_pub, cache, tofu_closed=True))
    assert body["success"] is True
    assert body["keyAuth"] == {"signed": True, "signerKeyId": id_a}
    assert cache == {id_a: pub_a}


async def test_a_phone_revoking_its_own_key_is_reported_signed():
    """The signature is checked before the key goes."""
    our_priv, our_pub = generate_keypair()
    priv_a, pub_a, id_a = _key()
    _, pub_b, id_b = _key()
    payload = {"keyIdToRevoke": id_a}
    cmd = {
        "commandId": "c-rev",
        "command": "revoke_trusted_key",
        "payload": payload,
        "signature": sign_payload(payload, priv_a),
        "signerKeyId": id_a,
    }
    cache = {id_a: pub_a, id_b: pub_b}
    body, _ = await _run(cmd, cache, _state(our_priv, our_pub, cache, tofu_closed=True))
    assert body["keyAuth"] == {"signed": True, "signerKeyId": id_a}


async def test_a_clear_is_refused_as_before_and_reports_whether_it_was_signed():
    our_priv, our_pub = generate_keypair()
    priv_a, pub_a, id_a = _key()
    cmd = {
        "commandId": "c-clr",
        "command": "clear_trusted_keys",
        "signature": sign_payload({}, priv_a),
        "signerKeyId": id_a,
    }
    cache = {id_a: pub_a}
    body, _ = await _run(cmd, cache, _state(our_priv, our_pub, cache, tofu_closed=True))
    assert body["success"] is False
    assert body["reason"] == "unsupported"
    assert body["keyAuth"] == {"signed": True, "signerKeyId": id_a}
    assert cache == {id_a: pub_a}


# ── keystore: trust-on-first-use ───────────────────────────────────────────


class _MemStore:
    def __init__(self) -> None:
        self.data: dict | None = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


def _keystore() -> SvitgridKeystore:
    ks = SvitgridKeystore.__new__(SvitgridKeystore)
    ks._store = _MemStore()
    ks._trust_listeners = []
    return ks


async def _save(ks: SvitgridKeystore, trusted: dict[str, str]) -> None:
    await ks.save(
        api_key="k",
        public_key_hex="04" + "00" * 64,
        private_key_pem="pem",
        signing_key_id="ha-me",
        trusted_key_ids=list(trusted),
        trusted_public_keys_hex=dict(trusted),
    )


async def test_tofu_is_open_until_the_store_holds_a_key_and_stays_closed_after():
    ks = _keystore()
    assert await ks.async_tofu_open() is True
    await _save(ks, {})
    assert await ks.async_tofu_open() is True
    await ks.update_trusted_keys_hex({"a": "04" + "aa" * 64})
    assert await ks.async_tofu_open() is False
    await ks.update_trusted_keys_hex({})
    assert await ks.async_tofu_open() is False


async def test_an_upgrade_records_every_entry_and_closes_tofu_on_a_store_with_keys():
    ks = _keystore()
    await _save(ks, {"a": "04" + "aa" * 64})
    ks._store.data["tofu_closed"] = False  # a blob written before 0.35
    ks._store.data.pop("paired_entry_ids", None)
    await ks.async_note_entry_pairing("e1", ["e1", "e2"])
    state = await ks.load()
    assert state.paired_entry_ids == ["e1", "e2"]
    assert state.tofu_closed is True
    assert state.is_new_pairing("e2") is False


async def test_the_integrations_own_key_alone_leaves_tofu_open():
    ks = _keystore()
    await _save(ks, {})
    await ks.update_trusted_keys_hex({"ha-me": "04" + "00" * 64})
    assert await ks.async_tofu_open() is True


async def test_a_new_pairing_that_leaves_the_store_empty_reopens_tofu():
    ks = _keystore()
    await _save(ks, {"a": "04" + "aa" * 64})
    await ks.async_note_entry_pairing("e1", ["e1"])
    await ks.update_trusted_keys_hex({})
    assert await ks.async_tofu_open() is False
    state = await ks.load()
    assert state.is_new_pairing("e2") is True
    await ks.async_note_entry_pairing("e2", ["e2"])
    assert await ks.async_tofu_open() is True
