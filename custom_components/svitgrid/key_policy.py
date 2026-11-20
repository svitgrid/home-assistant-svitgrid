"""The add-on's trusted-key rules, mirrored from the S3 board (svitgrid D-2336, D-2339).

The board's rules live in the svitgrid monorepo, in
`devices/edge-connector-v2/components/command_verifier/key_add_policy.c` and
`key_command_auth.c`. Since S3 2.182.0 a board takes a new key only when a key
it already holds signed it, and an unsigned first key only after a reset at
the board (trust-on-first-use). The add-on follows the same rules, so a
compromised cloud cannot plant a key here either.

The add-on's reset at the board is its Home Assistant admin UI: adding the
Svitgrid integration there (a new config entry) reopens trust-on-first-use
(`SvitgridKeystore.async_open_tofu_for_entry`).

Revokes and clears are not refused here (stage 1 of #1560): the add-on only
reports, on the ack, whether a key it holds signed them (`keyAuth`), in the
shape the board sends.

Pure: nothing here reads or writes the keystore.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from .signing import verify_payload

# A reported key is the first 8 hex characters of its board id
# (`trustedKeyIdPrefixes`, svitgrid #788 item 4).
KEY_ID_PREFIX_LEN = 8

# The API refuses a whole ingest batch, readings included, whose
# `trustedKeyIdPrefixes` lists more than 32 (EdgeTelemetrySchema). A board
# holds at most 5 keys; the add-on has no such cap, so the list is cut here.
MAX_REPORTED_PREFIXES = 32

# The add-on's counterpart of the board's "first_key_needs_local_reset": an
# empty key store that held keys before takes no unsigned first key until the
# integration is added again in Home Assistant. A reason of its own, because
# the remedy differs from the board's button.
FIRST_KEY_NEEDS_LOCAL_PAIRING = "first_key_needs_local_pairing"

_PUBKEY_HEX_LEN = 130  # 0x04 || x || y, 65 bytes

AddOutcome = Literal["proceed", "already_held", "reject"]


@dataclass(frozen=True)
class KeyAddDecision:
    outcome: AddOutcome
    # The board's id for the key: SHA-256 of its 65 bytes, lowercase hex. Set
    # unless the public key itself was refused.
    board_key_id: str | None = None
    reason: str | None = None


def board_key_id(public_key_hex: str) -> str:
    """The id an S3 board gives this key (`trusted_keys_compute_id`)."""
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def _pubkey_reason(public_key_hex: Any) -> str | None:
    if not isinstance(public_key_hex, str) or not public_key_hex:
        return "missing_public_key"
    if len(public_key_hex) != _PUBKEY_HEX_LEN:
        return "invalid_public_key_length"
    try:
        bytes.fromhex(public_key_hex)
    except ValueError:
        return "invalid_public_key_hex"
    return None


def _holds_public_key(store: dict[str, str], public_key_hex: str) -> bool:
    wanted = public_key_hex.lower()
    return any(isinstance(v, str) and v.lower() == wanted for v in store.values())


def _non_empty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def find_signer_key(store: dict[str, str], signer_key_id: str) -> str | None:
    """The public key of `signer_key_id` among the held keys, or None.

    The add-on files a key under the `signingKeyId` the cloud named it by. A
    phone's id is the board id of its key, so a signer named by its board id
    is found too.
    """
    direct = store.get(signer_key_id)
    if isinstance(direct, str) and direct:
        return direct
    for pub in store.values():
        try:
            if isinstance(pub, str) and board_key_id(pub) == signer_key_id:
                return pub
        except ValueError:
            continue
    return None


def _verify(store: dict[str, str], message: Any, signature: str, signer: str) -> str | None:
    """None when `signature` is a valid signature by held key `signer` over
    the canonical JSON of `message`; otherwise the board's refusal reason."""
    signer_pub = find_signer_key(store, signer)
    if signer_pub is None:
        return "unknown_signer_key"
    try:
        base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return "invalid_signature_encoding"
    if not verify_payload(message, signature, signer_pub):
        return "invalid_signature"
    return None


def holds_another_key(store: dict[str, str], own_public_key_hex: str | None) -> bool:
    """Whether the store holds a key other than this integration's own.

    The add-on trusts its own key once the household approves it. Only this
    install holds that key's private half, so it lets nobody else in, and it
    does not count as the store holding a key: the household's first phone
    still arrives as an unsigned first key, as on a board.
    """
    own = own_public_key_hex.lower() if isinstance(own_public_key_hex, str) else None
    return any(isinstance(v, str) and v.lower() != own for v in store.values())


def decide_key_add(
    store: dict[str, str],
    command: dict[str, Any],
    *,
    tofu_open: bool,
    own_public_key_hex: str | None = None,
) -> KeyAddDecision:
    """Decide an `add_trusted_key`, as `key_add_decide` and
    `key_add_first_key_allowed` do on the board, signature included.

    In the board's order: the public key must be well formed; a key already
    held is a success that changes nothing, signed or not (#788 item 2); a
    new key on a store that holds any needs a valid signature from a held
    key; a first key on an empty store is taken unsigned only while
    trust-on-first-use is open.

    This integration's own key (`own_public_key_hex`) is the one addition
    the board has no counterpart for: it is taken unsigned and does not count
    as a held key (`holds_another_key`).
    """
    payload = command.get("payload")
    if not isinstance(payload, dict):
        return KeyAddDecision("reject", reason="missing_payload")
    public_key_hex = payload.get("publicKeyHex")
    bad = _pubkey_reason(public_key_hex)
    if bad is not None:
        return KeyAddDecision("reject", reason=bad)
    key_id = board_key_id(public_key_hex)
    if _holds_public_key(store, public_key_hex):
        return KeyAddDecision("already_held", board_key_id=key_id)
    if isinstance(own_public_key_hex, str) and public_key_hex.lower() == own_public_key_hex.lower():
        return KeyAddDecision("proceed", board_key_id=key_id)
    if not holds_another_key(store, own_public_key_hex):
        if tofu_open:
            return KeyAddDecision("proceed", board_key_id=key_id)
        return KeyAddDecision("reject", board_key_id=key_id, reason=FIRST_KEY_NEEDS_LOCAL_PAIRING)
    signature = _non_empty_str(command.get("signature"))
    signer = _non_empty_str(command.get("signerKeyId"))
    if signature is None or signer is None:
        return KeyAddDecision("reject", board_key_id=key_id, reason="bootstrap_only_when_empty")
    refused = _verify(store, payload, signature, signer)
    if refused is not None:
        return KeyAddDecision("reject", board_key_id=key_id, reason=refused)
    return KeyAddDecision("proceed", board_key_id=key_id)


def key_command_auth(store: dict[str, str], command: dict[str, Any]) -> dict[str, Any]:
    """The ack's `keyAuth` for a clear or revoke, as `key_command_auth_check`
    builds it on the board: `{"signed": True, "signerKeyId": …}` when a held
    key signed the command's payload (`{}` when it has none), otherwise
    `{"signed": False}`. Call it before the store changes: a phone revoking
    its own key signs with that key."""
    signature = _non_empty_str(command.get("signature"))
    signer = _non_empty_str(command.get("signerKeyId"))
    if signature is None or signer is None:
        return {"signed": False}
    payload = command.get("payload")
    message = payload if isinstance(payload, dict) else {}
    if _verify(store, message, signature, signer) is not None:
        return {"signed": False}
    return {"signed": True, "signerKeyId": signer}


def key_store_report(store: dict[str, str]) -> dict[str, Any]:
    """The key store as the board reports it on its ingest batches:
    `trustedKeyCount`, and `trustedKeyIdPrefixes` (the first 8 hex characters
    of each key's board id, sorted) when it holds any. An empty store is a
    count of 0 with no list, as on the board."""
    prefixes: set[str] = set()
    for pub in store.values():
        if not isinstance(pub, str) or _pubkey_reason(pub) is not None:
            continue
        prefixes.add(board_key_id(pub)[:KEY_ID_PREFIX_LEN])
    report: dict[str, Any] = {"trustedKeyCount": len(store)}
    if store:
        report["trustedKeyIdPrefixes"] = sorted(prefixes)[:MAX_REPORTED_PREFIXES]
    return report
