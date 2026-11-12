"""Guard: tests must not turn the global ``asyncio.sleep`` into a no-op.

``patch("custom_components.svitgrid.config_flow.asyncio.sleep", ...)`` patches
the attribute on the real asyncio module, so every coroutine in the process
sees it, not only the config flow. Once a pairing finalizes, the entry is set up
and the readings loop starts as an eager task. A no-op sleep never yields, so
that loop spun forever on "Skipping ingest — incomplete reading" and the suite
hung with no timeout (svitgrid D-1850). Speed the poll up with
``tests.flow_sleep.patch_flow_sleep``, which replaces sleep for the config
flow alone.
"""

from __future__ import annotations

import re
from pathlib import Path

_TESTS = Path(__file__).parent
# These two only name the bad pattern in their docstrings.
_SKIP = {Path(__file__).name, "flow_sleep.py"}
# Any patch target ending in ``asyncio.sleep`` reaches the real asyncio module.
_NOOP_SLEEP_PATCH = re.compile(r"""patch\(\s*["'][\w.]*\basyncio\.sleep["']""")


def test_no_test_patches_the_global_asyncio_sleep() -> None:
    offenders = [
        f"{path.relative_to(_TESTS)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_TESTS.rglob("*.py"))
        if path.name not in _SKIP
        for text in [path.read_text(encoding="utf-8")]
        for m in _NOOP_SLEEP_PATCH.finditer(text)
    ]
    assert offenders == [], (
        "asyncio.sleep is patched process-wide (with a no-op, the readings loop "
        "started by entry setup never yields and the suite hangs); use "
        "tests.flow_sleep.patch_flow_sleep instead: " + ", ".join(offenders)
    )
