"""Tests for the expression DSL used to compute write-command args in
preset YAMLs.

The DSL is a tiny safe subset of Python expressions — no statements, no
attribute access, no calls except whitelisted helpers. Two namespaces
exposed: `payload` (the command's payload dict) and `config` (preset
config from /finalize, e.g. battery_voltage, hub_name, slave_id)."""
from __future__ import annotations

import pytest

from custom_components.svitgrid.dsl import (
    DslEvalError,
    evaluate,
)


# ── Happy path: arithmetic + namespace refs ───────────────────────────


def test_literal_int():
    assert evaluate("42", payload={}, config={}) == 42


def test_literal_float():
    assert evaluate("3.14", payload={}, config={}) == 3.14


def test_payload_ref():
    assert evaluate("payload.chargePowerLimitW", payload={"chargePowerLimitW": 2000}, config={}) == 2000


def test_config_ref():
    assert evaluate("config.battery_voltage", payload={}, config={"battery_voltage": 52.8}) == 52.8


def test_division():
    assert evaluate(
        "payload.chargePowerLimitW / config.battery_voltage",
        payload={"chargePowerLimitW": 2640},
        config={"battery_voltage": 52.8},
    ) == 50.0


def test_round_call():
    assert evaluate(
        "round(payload.chargePowerLimitW / config.battery_voltage / 0.1)",
        payload={"chargePowerLimitW": 2640},
        config={"battery_voltage": 52.8},
    ) == 500


def test_min_max_abs():
    assert evaluate("min(10, 20)", payload={}, config={}) == 10
    assert evaluate("max(10, 20)", payload={}, config={}) == 20
    assert evaluate("abs(-7)", payload={}, config={}) == 7


def test_int_float_coercion():
    assert evaluate("int(3.9)", payload={}, config={}) == 3
    assert evaluate("float(3)", payload={}, config={}) == 3.0


def test_nested_arithmetic():
    assert evaluate(
        "(payload.a + payload.b) * config.scale",
        payload={"a": 10, "b": 5},
        config={"scale": 2},
    ) == 30


def test_unary_minus():
    assert evaluate("-payload.x", payload={"x": 42}, config={}) == -42


# ── Rejected: anything not on the whitelist ───────────────────────────


def test_rejects_attribute_chain_beyond_top_level():
    """payload.foo.bar — only one level of attribute access allowed."""
    with pytest.raises(DslEvalError):
        evaluate("payload.foo.bar", payload={"foo": {"bar": 1}}, config={})


def test_rejects_unknown_function():
    with pytest.raises(DslEvalError):
        evaluate("len(payload.x)", payload={"x": [1, 2, 3]}, config={})


def test_rejects_dunder_access():
    with pytest.raises(DslEvalError):
        evaluate("payload.__class__", payload={}, config={})


def test_rejects_subscript():
    with pytest.raises(DslEvalError):
        evaluate("payload[0]", payload={"x": 1}, config={})


def test_rejects_lambda():
    with pytest.raises(DslEvalError):
        evaluate("lambda: 42", payload={}, config={})


def test_rejects_import():
    with pytest.raises(DslEvalError):
        evaluate("__import__('os')", payload={}, config={})


def test_rejects_bare_name_outside_payload_config():
    """A bare identifier (not 'payload' / 'config') is rejected."""
    with pytest.raises(DslEvalError):
        evaluate("undefined_var", payload={}, config={})


def test_rejects_string_literal():
    """Strings could be load-bearing if we ever allow eval on values;
    forbid them now while the DSL is purely numeric. Easy to relax later."""
    with pytest.raises(DslEvalError):
        evaluate("'evil'", payload={}, config={})


def test_rejects_comparison():
    """No booleans / control flow at all."""
    with pytest.raises(DslEvalError):
        evaluate("payload.x > 5", payload={"x": 10}, config={})


def test_missing_payload_key_raises_with_clear_message():
    with pytest.raises(DslEvalError, match="payload\\.missing"):
        evaluate("payload.missing", payload={}, config={})


def test_missing_config_key_raises_with_clear_message():
    with pytest.raises(DslEvalError, match="config\\.missing"):
        evaluate("config.missing", payload={}, config={})


def test_division_by_zero_raises_dsl_error():
    """ZeroDivisionError wrapped in DslEvalError so the executor can ACK with
    a clean executor_error reason instead of a Python traceback."""
    with pytest.raises(DslEvalError, match="zero"):
        evaluate("payload.x / 0", payload={"x": 1}, config={})


# ── Literal passthrough for non-string args ───────────────────────────


def test_literal_int_passthrough():
    """When the YAML has a plain int/float/bool, evaluate returns it as-is."""
    assert evaluate(42, payload={}, config={}) == 42
    assert evaluate(3.14, payload={}, config={}) == 3.14
    assert evaluate(True, payload={}, config={}) is True
