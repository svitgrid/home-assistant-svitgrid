"""Tests for the shared verify_signed_command helper (command_auth.py).

TDD: tests written before implementation so they will FAIL until
command_auth.py exists with the correct logic.
"""
from custom_components.svitgrid import signing
from custom_components.svitgrid.command_auth import verify_signed_command


def test_accepts_trusted_valid_signature():
    """Trusted key + valid signature over signed_event_data → True."""
    priv, pub = signing.generate_keypair()
    data = {"command": "set_work_mode", "payload": {"inverterId": "ha-1", "workMode": 1}}
    sig = signing.sign_payload(data, priv)
    assert verify_signed_command({"kid-1": pub}, "kid-1", data, sig) is True


def test_rejects_untrusted_key():
    """Key ID absent from trusted dict → False (even with a valid sig)."""
    priv, pub = signing.generate_keypair()
    data = {"x": 1}
    sig = signing.sign_payload(data, priv)
    assert verify_signed_command({}, "kid-1", data, sig) is False


def test_rejects_tampered_data():
    """Sig was created over different data → False."""
    priv, pub = signing.generate_keypair()
    sig = signing.sign_payload({"a": 1}, priv)
    assert verify_signed_command({"kid-1": pub}, "kid-1", {"a": 2}, sig) is False


def test_rejects_missing_signing_key_id():
    """signing_key_id is None → False."""
    assert verify_signed_command({"kid-1": "04ab"}, None, {}, "sig") is False


def test_rejects_missing_signature():
    """signature is None → False."""
    assert verify_signed_command({"kid-1": "04ab"}, "kid-1", {}, None) is False


def test_rejects_empty_signing_key_id():
    """signing_key_id is empty string → False."""
    priv, pub = signing.generate_keypair()
    data = {"x": 1}
    sig = signing.sign_payload(data, priv)
    assert verify_signed_command({"kid-1": pub}, "", data, sig) is False


def test_rejects_empty_signature():
    """signature is empty string → False."""
    priv, pub = signing.generate_keypair()
    data = {"x": 1}
    assert verify_signed_command({"kid-1": pub}, "kid-1", data, "") is False


def test_never_raises_on_garbage_inputs():
    """Garbage public key hex → False, no exception."""
    result = verify_signed_command(
        {"kid-1": "not-a-valid-hex-pubkey"},
        "kid-1",
        {"x": 1},
        "badsig==",
    )
    assert result is False
