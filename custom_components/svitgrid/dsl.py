"""Safe expression evaluator for preset write-command arg computation.

YAML preset commands look like:

    commands:
      - id: set_battery_charge
        service: modbus.write_register
        args:
          hub: "{{ config.hub_name }}"
          slave: "{{ config.slave_id }}"
          address: 233
          value: "round(payload.chargePowerLimitW / config.battery_voltage / 0.1)"

The strings under `args` are evaluated by this module. The DSL is a
deliberately tiny subset of Python expressions:

  - Numeric literals (int, float)
  - The `payload.<name>` and `config.<name>` namespaces (single-depth
    attribute access only)
  - Arithmetic operators: + - * / // %
  - Unary minus
  - Parentheses
  - Whitelisted functions: round, min, max, abs, int, float

EVERYTHING ELSE IS REJECTED — no string literals, no comparisons, no
list/dict literals, no subscripting, no function calls outside the
whitelist, no attribute chains beyond depth 1, no dunders, no imports,
no lambdas. The intent is: presets are server-controlled data that
ships to many HA installs — a compromised preset should not be able to
execute arbitrary code on the user's HA host.

Implementation uses Python's `ast` module to parse the expression and
walks the tree with an explicit allowlist, refusing nodes outside it.
We never call `eval()` or `compile(..., mode='exec')`.
"""
from __future__ import annotations

import ast
import operator
from typing import Any


class DslEvalError(Exception):
    """Raised when an expression is rejected or fails at runtime.
    Caught by the YamlDispatcher and surfaced as ACK reason='executor_error'."""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_FUNCTIONS = {
    "round": round,
    "min": min,
    "max": max,
    "abs": abs,
    "int": int,
    "float": float,
}


def evaluate(
    expression: Any,
    *,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> Any:
    """Evaluate a DSL expression. Non-string values (int/float/bool) are
    returned as-is so YAML can mix literals freely with expressions."""
    if not isinstance(expression, str):
        return expression
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise DslEvalError(f"DSL parse error: {exc.msg!r} in {expression!r}") from exc
    return _eval_node(tree.body, payload=payload, config=config, expr=expression)


def _eval_node(
    node: ast.AST,
    *,
    payload: dict[str, Any],
    config: dict[str, Any],
    expr: str,
) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise DslEvalError(
            f"DSL rejects literal of type {type(node.value).__name__} in {expr!r}"
        )

    if isinstance(node, ast.Attribute):
        # Only depth-1: payload.x or config.y. The value MUST be a Name.
        if not isinstance(node.value, ast.Name):
            raise DslEvalError(
                f"DSL allows attribute access only on payload/config (depth 1) in {expr!r}"
            )
        ns_name = node.value.id
        if ns_name not in ("payload", "config"):
            raise DslEvalError(
                f"DSL rejects attribute access on {ns_name!r} (use payload/config) in {expr!r}"
            )
        attr = node.attr
        if attr.startswith("_"):
            raise DslEvalError(f"DSL rejects dunder/private attribute {attr!r} in {expr!r}")
        ns = payload if ns_name == "payload" else config
        if attr not in ns:
            raise DslEvalError(f"missing key {ns_name}.{attr} in {expr!r}")
        return ns[attr]

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise DslEvalError(f"DSL rejects operator {op_type.__name__} in {expr!r}")
        left = _eval_node(node.left, payload=payload, config=config, expr=expr)
        right = _eval_node(node.right, payload=payload, config=config, expr=expr)
        try:
            return _BIN_OPS[op_type](left, right)
        except ZeroDivisionError as exc:
            raise DslEvalError(f"division by zero in {expr!r}") from exc

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise DslEvalError(f"DSL rejects unary op {op_type.__name__} in {expr!r}")
        operand = _eval_node(node.operand, payload=payload, config=config, expr=expr)
        return _UNARY_OPS[op_type](operand)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            func_repr = getattr(node.func, "id", type(node.func).__name__)
            raise DslEvalError(f"DSL rejects function call {func_repr!r} in {expr!r}")
        if node.keywords:
            raise DslEvalError(f"DSL rejects keyword arguments in {expr!r}")
        args = [
            _eval_node(a, payload=payload, config=config, expr=expr) for a in node.args
        ]
        return _FUNCTIONS[node.func.id](*args)

    # Bare names (other than payload/config inside Attribute) are rejected.
    if isinstance(node, ast.Name):
        raise DslEvalError(
            f"DSL rejects bare identifier {node.id!r} "
            f"(use payload.<name> or config.<name>) in {expr!r}"
        )

    raise DslEvalError(
        f"DSL rejects node type {type(node).__name__} in {expr!r}"
    )
