"""Canonical JSON + ECDSA P-256 signing helpers.

Mirror of services/api/src/signing/canonical-json.ts and verify-signature.ts
in the svitgrid monorepo. Byte-level compatibility is required — mismatched
output here makes every signed ACK fail on the server.

Canonical JSON rules (from TS):
  - Keys sorted by Unicode codepoint at every nesting level. This matches
    JavaScript's default `Array.prototype.sort()` ordering. Non-ASCII keys
    are supported but SHOULD be avoided in the signed ACK payload — the
    entire cloud API uses ASCII-only keys, and a non-ASCII key on the add-on
    side is almost certainly a bug.
  - Arrays preserve source order (not sorted).
  - Numbers: integer-valued doubles emit as ints ("1", not "1.0"). Non-integer
    doubles emit as their Python repr (matches JS toString for sanely-sized nums).
  - Number-size boundary: JavaScript's `number` loses precision above 2**53
    (Number.MAX_SAFE_INTEGER). Python ints do not, so integer-valued payloads
    with values > 2^53 will serialize DIFFERENTLY across languages. Callers
    MUST use strings (not numbers) for any identifier or counter that could
    exceed the JS safe-integer range.
  - NaN / Infinity / -Infinity raise ValueError.
  - undefined (missing keys) are omitted by the caller; None IS preserved
    as JSON null (the TS side distinguishes between them — Python does not,
    so for symmetry with the contract, sign_ack_payload in commands must
    avoid placing None in required fields).
"""

from __future__ import annotations

import base64
import json
import math

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def canonical_json_encode(obj: object) -> str:
    """Encode obj as canonical JSON string."""
    return _encode(obj)


def canonical_json_bytes(obj: object) -> bytes:
    """Encode obj as canonical JSON UTF-8 bytes."""
    return canonical_json_encode(obj).encode("utf-8")


def _encode(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"Cannot encode non-finite number: {value}")
        # Match TS: integer-valued doubles emit as ints.
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # json.dumps for string escaping — matches JSON.stringify.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_encode(v) for v in value) + "]"
    if isinstance(value, dict):
        # Sorted keys, recursive encode.
        parts = [
            f"{json.dumps(str(k), ensure_ascii=False)}:{_encode(v)}"
            for k, v in sorted(value.items())
        ]
        return "{" + ",".join(parts) + "}"
    raise TypeError(f"Unsupported type: {type(value).__name__}")


def generate_keypair() -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Generate a fresh ECDSA P-256 keypair. Returns (private_key, public_key_hex)
    where public_key_hex is the uncompressed EC point (04 + x + y, 130 hex chars)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, public_key_to_hex(private_key.public_key())


def public_key_to_hex(public_key: ec.EllipticCurvePublicKey) -> str:
    """Serialize a P-256 public key as uncompressed EC point hex."""
    nums = public_key.public_numbers()
    x = nums.x.to_bytes(32, "big")
    y = nums.y.to_bytes(32, "big")
    return "04" + x.hex() + y.hex()


def public_key_from_hex(hex_str: str) -> ec.EllipticCurvePublicKey:
    """Parse an uncompressed EC point (04 || x || y) into a P-256 public key."""
    if not hex_str.startswith("04") or len(hex_str) != 130:
        raise ValueError("publicKeyHex must be uncompressed EC point (04 + 128 hex chars)")
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError as e:
        raise ValueError(f"publicKeyHex is not valid hex: {e}") from e
    x = int.from_bytes(raw[1:33], "big")
    y = int.from_bytes(raw[33:65], "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def sign_payload(payload: object, private_key: ec.EllipticCurvePrivateKey) -> str:
    """Sign canonical-JSON bytes of payload with ECDSA P-256 + SHA-256.
    Returns base64-encoded DER signature (what the server expects)."""
    der = private_key.sign(canonical_json_bytes(payload), ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(der).decode("ascii")


def verify_payload(payload: object, signature_b64: str, public_key_hex: str) -> bool:
    """Verify a base64 DER signature over canonical-JSON bytes of payload
    against a public key given as uncompressed EC point hex."""
    try:
        public_key = public_key_from_hex(public_key_hex)
        der = base64.b64decode(signature_b64)
        public_key.verify(der, canonical_json_bytes(payload), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False
