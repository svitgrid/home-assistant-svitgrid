"""Shared signed-command verifier used by command_poller and the island
local-command endpoint (island-mode SP1).

Factored out of command_poller.py's inline admin-signature gate so that
both the cloud poller path and the LAN island path share a single,
tested verification function.
"""

from __future__ import annotations

import logging

from .signing import verify_payload

_LOGGER = logging.getLogger(__name__)


def verify_signed_command(
    trusted_public_keys_hex: dict[str, str],
    signing_key_id: str | None,
    signed_event_data: object,
    signature: str | None,
) -> bool:
    """Return True iff the command carries a valid admin signature.

    Conditions for True (all must hold):
    - ``signing_key_id`` is truthy (non-None, non-empty).
    - ``signing_key_id`` is present in ``trusted_public_keys_hex``.
    - ``signature`` is truthy (non-None, non-empty).
    - ``verify_payload(signed_event_data, signature, pub_hex)`` returns True.

    Any exception from the cryptography layer is caught and treated as False
    so callers never have to guard against unexpected raises.
    """
    if not signing_key_id:
        return False
    pub_hex = trusted_public_keys_hex.get(signing_key_id)
    if not pub_hex:
        return False
    if not signature:
        return False
    try:
        return verify_payload(signed_event_data, signature, pub_hex)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "verify_signed_command: unexpected exception for key_id=%s",
            signing_key_id,
            exc_info=True,
        )
        return False
