"""Where a direct-harvest inverter's register spec comes from.

Setup used to fetch the spec from the cloud on every start and keep it only in
memory. A Home Assistant that started without the cloud had no spec, and the
harvest loop idled. The order now:

1. The cloud, unless the entry is local-only. Every spec it returns is saved.
2. The spec saved from an earlier cloud answer (Home Assistant storage).
3. The copy bundled with the component (`register_specs/`), refreshed from
   the monorepo by `scripts/sync-register-specs.sh`.

A saved copy always wins over the bundle. Versions cannot settle it: maps are
fixed without a version bump, and the saved copy is the more recent cloud
answer.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

BUNDLED_SPECS_DIR = Path(__file__).resolve().parent.parent / "register_specs"

SPEC_STORAGE_KEY = "svitgrid_register_specs"
SPEC_STORAGE_VERSION = 1

# Same shape as the cloud's modelId. It also keeps a caller from naming a file
# outside the directory.
_MODEL_ID = re.compile(r"^[a-z0-9_]+$")

Fetch = Callable[[str], Awaitable[dict | None]]


def load_bundled_spec(model_id: str) -> dict[str, Any] | None:
    """The bundled spec document for `model_id`, or None. Reads the disk."""
    if not isinstance(model_id, str) or not _MODEL_ID.match(model_id):
        return None
    path = BUNDLED_SPECS_DIR / f"{model_id}.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOGGER.exception("bundled register spec %s could not be read", model_id)
        return None
    return doc if isinstance(doc, dict) else None


class RegisterSpecStore:
    """Every spec the cloud has returned, keyed by model id, in HA storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, SPEC_STORAGE_VERSION, SPEC_STORAGE_KEY)

    async def _load(self) -> dict[str, Any]:
        data = await self._store.async_load()
        return data if isinstance(data, dict) else {}

    async def async_get(self, model_id: str) -> dict[str, Any] | None:
        spec = (await self._load()).get(model_id)
        return spec if isinstance(spec, dict) else None

    async def async_put(self, model_id: str, spec: dict[str, Any]) -> None:
        data = await self._load()
        data[model_id] = spec
        await self._store.async_save(data)


async def resolve_register_spec(
    hass: HomeAssistant,
    model_id: str,
    *,
    fetch: Fetch | None,
    store: RegisterSpecStore,
) -> dict[str, Any] | None:
    """The spec to run `model_id` with, or None when no source has one.

    `fetch` is the cloud lookup; pass None for a local-only entry, which must
    not call the cloud. A spec the cloud returns is used and saved as it is,
    with no version comparison: maps are fixed in place without a version
    bump, so a version gate would freeze an install on its first copy.

    Never raises: a storage failure falls through to the next source, because
    a spec from anywhere beats an idle harvest loop.
    """
    if fetch is not None:
        try:
            fetched = await fetch(model_id)
        except Exception:  # noqa: BLE001 — the cloud is unreachable; fall back
            _LOGGER.warning(
                "harvest: register spec fetch failed for %s; using a local copy",
                model_id,
                exc_info=True,
            )
            fetched = None
        if fetched:
            try:
                await store.async_put(model_id, fetched)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("could not save register spec for %s", model_id)
            return fetched

    try:
        saved = await store.async_get(model_id)
    except Exception:  # noqa: BLE001 — storage is a cache, never a blocker
        _LOGGER.exception("could not read saved register spec for %s", model_id)
        saved = None
    if saved is not None:
        return saved
    bundled = await hass.async_add_executor_job(load_bundled_spec, model_id)
    if bundled is not None:
        _LOGGER.info("harvest: using the bundled register spec for %s", model_id)
    return bundled
