"""Cross-language canonical-JSON tests — vectors copied verbatim from
services/api/src/__tests__/signing/canonical-json.test.ts in the
svitgrid monorepo. A mismatch here means signed ACKs will fail
verification on the server; these tests are the contract gate.
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.signing import (
    canonical_json_bytes,
    canonical_json_encode,
)


class TestCanonicalJsonEncode:
    def test_sorts_top_level_keys(self):
        assert canonical_json_encode({"z": 1, "a": 2, "m": 3}) == '{"a":2,"m":3,"z":1}'

    def test_sorts_nested_keys_recursively(self):
        assert canonical_json_encode({"outer": {"z": 1, "a": 2}}) == '{"outer":{"a":2,"z":1}}'

    def test_arrays_preserve_order(self):
        assert canonical_json_encode({"items": [3, 1, 2]}) == '{"items":[3,1,2]}'

    def test_nested_arrays_of_objects(self):
        assert (
            canonical_json_encode({"list": [{"z": 1, "a": 2}, {"m": 3}]})
            == '{"list":[{"a":2,"z":1},{"m":3}]}'
        )

    def test_strings_with_special_chars(self):
        assert canonical_json_encode({"key": 'hello "world"'}) == '{"key":"hello \\"world\\""}'

    def test_null_values_preserved(self):
        # None values are PRESERVED (distinct from missing keys).
        # Matches TS: { a: null, b: 1 } -> {"a":null,"b":1}
        assert canonical_json_encode({"a": None, "b": 1}) == '{"a":null,"b":1}'

    def test_booleans(self):
        assert (
            canonical_json_encode({"flag": True, "other": False}) == '{"flag":true,"other":false}'
        )

    def test_doubles(self):
        assert canonical_json_encode({"val": 55.2}) == '{"val":55.2}'

    def test_integer_valued_doubles_as_integers(self):
        # Critical: TS emits "1" not "1.0" for x == floor(x).
        assert canonical_json_encode({"val": 1.0, "other": 55.0}) == '{"other":55,"val":1}'

    def test_empty_map(self):
        assert canonical_json_encode({}) == "{}"

    def test_empty_array(self):
        assert canonical_json_encode({"items": []}) == '{"items":[]}'

    def test_produces_utf8_bytes(self):
        assert canonical_json_bytes({"a": 1}) == b'{"a":1}'

    def test_raises_for_nan(self):
        with pytest.raises(ValueError):
            canonical_json_encode({"val": float("nan")})

    def test_raises_for_infinity(self):
        with pytest.raises(ValueError):
            canonical_json_encode({"val": float("inf")})

    def test_raises_for_negative_infinity(self):
        with pytest.raises(ValueError):
            canonical_json_encode({"val": float("-inf")})

    def test_cross_platform_vector(self):
        # Direct copy of TS "matches cross-platform test vector" test.
        payload = {
            "scenarioId": "evt-456",
            "inverterId": "inv-123",
            "mode": "battery_charge",
            "authorizedCommands": ["set_battery_charge", "restore_battery_charge"],
            "schedule": {
                "startTime": "10:00",
                "endTime": "16:00",
                "recurrence": "daily",
            },
            "config": {"targetSoc": 90, "chargeVoltage": 55.2},
            "commandPayload": {
                "chargeVoltage": 55.2,
                "gridChargeEnable": 1,
                "gridChargeSoc": 90,
                "slotEnd": 960,
                "slotStart": 600,
            },
            "validUntil": "2026-12-31T23:59:59Z",
        }
        expected = (
            '{"authorizedCommands":["set_battery_charge","restore_battery_charge"],'
            '"commandPayload":{"chargeVoltage":55.2,"gridChargeEnable":1,"gridChargeSoc":90,"slotEnd":960,"slotStart":600},'
            '"config":{"chargeVoltage":55.2,"targetSoc":90},'
            '"inverterId":"inv-123",'
            '"mode":"battery_charge",'
            '"scenarioId":"evt-456",'
            '"schedule":{"endTime":"16:00","recurrence":"daily","startTime":"10:00"},'
            '"validUntil":"2026-12-31T23:59:59Z"}'
        )
        assert canonical_json_encode(payload) == expected

    def test_tuple_raises_type_error(self):
        # Tuples are NOT valid — callers must use lists. This pins the
        # current strict behavior: surprise coercion would be worse than
        # a clear error.
        with pytest.raises(TypeError):
            canonical_json_encode({"items": ("a", "b")})

    def test_bytes_raises_type_error(self):
        # bytes aren't JSON-serializable — caller must decode to str first.
        with pytest.raises(TypeError):
            canonical_json_encode({"blob": b"data"})

    def test_float_precision_edge_case(self):
        # Both Python and JS use shortest-round-trip float formatting.
        # 0.1 + 0.2 == 0.30000000000000004 in both. This test pins
        # the cross-language parity for float precision edge cases.
        assert canonical_json_encode({"val": 0.1 + 0.2}) == '{"val":0.30000000000000004}'
