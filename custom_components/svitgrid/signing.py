"""Canonical JSON + ECDSA P-256 signing helpers.

Mirror of services/api/src/signing/canonical-json.ts and verify-signature.ts
in the svitgrid monorepo. Byte-level compatibility is required — mismatched
output here makes every signed ACK fail on the server.

Canonical JSON rules (from TS):
  - Keys sorted alphabetically (ASCII-only) at every nesting level.
  - Arrays preserve source order (not sorted).
  - Numbers: integer-valued doubles emit as ints ("1", not "1.0"). Non-integer
    doubles emit as their Python repr (matches JS toString for sanely-sized nums).
  - NaN / Infinity / -Infinity raise ValueError.
  - undefined (missing keys) are omitted by the caller; None IS preserved
    as JSON null (the TS side distinguishes between them — Python does not,
    so for symmetry with the contract, sign_ack_payload in commands must
    avoid placing None in required fields).
"""

from __future__ import annotations

import json
import math


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
