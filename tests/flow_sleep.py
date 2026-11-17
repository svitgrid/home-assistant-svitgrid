"""Make the config flow's pairing poll run without waiting, and nothing else.

``patch("custom_components.svitgrid.config_flow.asyncio.sleep", ...)`` looks
local but patches ``sleep`` on the real asyncio module, for every coroutine in
the process. When a pairing finalizes, the entry is set up and the readings loop
starts as an eager task. With a no-op sleep that loop never yields: it spun on
"Skipping ingest — incomplete reading" forever and the CI job hung until it was
cancelled (svitgrid D-1850). This helper gives the config flow its own copy of
the asyncio module with ``sleep`` replaced, so the rest of the integration keeps
the real one.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Awaitable, Callable
from unittest.mock import patch


def patch_flow_sleep(sleep: Callable[[float], Awaitable[None]]):
    """Patch ``asyncio.sleep`` as the config flow sees it, and only there."""
    flow_asyncio = types.ModuleType("asyncio")
    flow_asyncio.__dict__.update(vars(asyncio))
    flow_asyncio.sleep = sleep
    return patch("custom_components.svitgrid.config_flow.asyncio", flow_asyncio)
