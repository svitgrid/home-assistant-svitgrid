"""Presets bundled with the component, readable with no cloud.

HACS installs only `custom_components/svitgrid/`, so the repository-root
`presets/` never reaches an install. `scripts/sync-bundled-presets.sh` copies
them into `presets/` beside this module; the root copy stays the source,
because the monorepo's preset sync reads it from there.

The cloud (`/api/v1/ha-presets`) stays the source of updates. These copies
are the fallback for a Home Assistant that pairs over the LAN, or that cannot
reach the cloud at all.

Every function here reads the disk, so call it from an executor job.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

BUNDLED_PRESETS_DIR = Path(__file__).parent / "presets"

# Same rule as the cloud's HaPresetSchema `id`. It also keeps a caller from
# naming a file outside the directory.
_PRESET_ID = re.compile(r"^[a-z0-9-]+$")


def _normalise(doc: dict[str, Any]) -> dict[str, Any]:
    """Apply the cloud schema's coercions and defaults.

    HaPresetSchema stores `version` as a numeric string and defaults
    `commands` to [] and `pvStrings` to 2. A YAML file may leave either out,
    or write the version unquoted.
    """
    preset = dict(doc)
    preset["version"] = str(preset.get("version", "1"))
    preset["commands"] = list(preset.get("commands") or [])
    preset["pvStrings"] = int(preset.get("pvStrings") or 2)
    preset["entityMap"] = dict(preset.get("entityMap") or {})
    return preset


def load_bundled_preset(preset_id: str) -> dict[str, Any] | None:
    """The full preset document, as the cloud's `GET /ha-presets/{id}` returns
    it, or None when no bundled preset has that id."""
    if not isinstance(preset_id, str) or not _PRESET_ID.match(preset_id):
        return None
    path = BUNDLED_PRESETS_DIR / f"{preset_id}.yaml"
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        _LOGGER.exception("bundled preset %s could not be read", preset_id)
        return None
    if not isinstance(doc, dict) or doc.get("id") != preset_id:
        _LOGGER.error("bundled preset %s is malformed or names another id", preset_id)
        return None
    return _normalise(doc)


def list_bundled_presets() -> list[dict[str, Any]]:
    """Every bundled preset in the shape of the cloud's list item.

    The keys and the brand-then-model order follow `GET /api/v1/ha-presets`,
    because the app parses both with `HaPreset.fromJson`. A file that does not
    parse is skipped, as the cloud skips a malformed document.
    """
    items: list[dict[str, Any]] = []
    for path in sorted(BUNDLED_PRESETS_DIR.glob("*.yaml")):
        preset = load_bundled_preset(path.stem)
        if preset is None:
            continue
        try:
            items.append(
                {
                    "id": preset["id"],
                    "version": preset["version"],
                    "brand": str(preset["brand"]),
                    "model": str(preset["model"]),
                    "phases": int(preset["phases"]),
                    "hasBattery": bool(preset["hasBattery"]),
                    "commandsCount": len(preset["commands"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            _LOGGER.error("bundled preset %s lacks a required field; skipped", path.stem)
    items.sort(key=lambda p: f"{p['brand']} {p['model']}".casefold())
    return items
