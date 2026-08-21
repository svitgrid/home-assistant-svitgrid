"""The wiring must match the signature it wires to.

Twice in one evening a keyword was added to a call site and removed from the
callee (or the reverse), and the whole test suite stayed green -- because no
test drives `async_setup_entry`, and every unit test calls the loops directly
with its own arguments. The failure only appears at runtime, as
`TypeError: run_sender_loop() got an unexpected keyword argument 'activity'`,
and it kills the background task that uploads readings. Nothing else reports
it: the task dies, the store fills, and the user sees stale data.

This binds the keywords each setup call site actually passes against the real
signature of the function it calls, so a mismatch fails here instead of on a
user's install.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import custom_components.svitgrid as svitgrid_init
from custom_components.svitgrid.command_poller import run_loop as run_command_loop
from custom_components.svitgrid.reading_sender import run_sender_loop
from custom_components.svitgrid.readings_publisher import run_loop as run_readings_loop

WIRED = {
    "run_sender_loop": run_sender_loop,
    "run_readings_loop": run_readings_loop,
    "run_command_loop": run_command_loop,
}


def _calls_in_setup() -> list[tuple[str, list[str], int]]:
    """Every call to a wired loop in __init__.py: (name, keywords, lineno)."""
    src = pathlib.Path(inspect.getsourcefile(svitgrid_init)).read_text()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in WIRED:
            kwargs = [k.arg for k in node.keywords if k.arg is not None]
            found.append((name, kwargs, node.lineno))
    return found


def test_there_is_something_to_check():
    # Guards the guard: if the AST walk stops finding call sites (a rename, a
    # refactor to a dict of partials), this test must fail rather than pass
    # vacuously while checking nothing.
    calls = _calls_in_setup()
    assert calls, "found no wired loop calls in __init__.py — the check is vacuous"


@pytest.mark.parametrize("name,kwargs,lineno", _calls_in_setup())
def test_setup_passes_only_keywords_the_loop_accepts(name, kwargs, lineno):
    sig = inspect.signature(WIRED[name])
    accepted = set(sig.parameters)
    takes_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if takes_var_kw:
        pytest.skip(f"{name} accepts **kwargs; nothing to mismatch")
    unknown = [k for k in kwargs if k not in accepted]
    assert not unknown, (
        f"__init__.py:{lineno} passes {unknown} to {name}(), which does not accept "
        f"it. This raises TypeError at runtime and kills the background task."
    )


@pytest.mark.parametrize("name,kwargs,lineno", _calls_in_setup())
def test_setup_supplies_every_required_keyword(name, kwargs, lineno):
    sig = inspect.signature(WIRED[name])
    required = [
        p.name
        for p in sig.parameters.values()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    missing = [r for r in required if r not in kwargs]
    assert not missing, f"__init__.py:{lineno} omits required keyword(s) {missing} for {name}()."
