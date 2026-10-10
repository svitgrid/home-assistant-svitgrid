"""Presets ship inside the component, so a Home Assistant with no cloud has them.

HACS installs only `custom_components/svitgrid/`, so the repository-root
`presets/` never reaches an install. The root copy stays the source: the
monorepo's `sync-ha-presets.mts` and `seed-from-yaml.cjs` read it from there.
`scripts/sync-bundled-presets.sh` copies it into the component, and the first
test here fails when the two drift.
"""

from __future__ import annotations

from pathlib import Path

from custom_components.svitgrid.bundled_presets import (
    BUNDLED_PRESETS_DIR,
    list_bundled_presets,
    load_bundled_preset,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_PRESETS = REPO_ROOT / "presets"

# The cloud's GET /api/v1/ha-presets list item (services/api ha-presets.ts).
# The app parses both lists with HaPreset.fromJson, so the keys must match.
LIST_ITEM_KEYS = {"id", "version", "brand", "model", "phases", "hasBattery", "commandsCount"}


def test_the_bundled_copy_matches_the_repository_presets_byte_for_byte():
    root = {p.name: p.read_bytes() for p in ROOT_PRESETS.glob("*.yaml")}
    bundled = {p.name: p.read_bytes() for p in BUNDLED_PRESETS_DIR.glob("*.yaml")}
    assert root, "no presets at the repository root"
    assert sorted(bundled) == sorted(root), (
        "custom_components/svitgrid/presets differs from presets/; "
        "run scripts/sync-bundled-presets.sh"
    )
    for name, content in root.items():
        assert bundled[name] == content, f"{name} differs; run scripts/sync-bundled-presets.sh"


def test_the_list_has_the_cloud_list_item_shape():
    presets = list_bundled_presets()
    assert len(presets) == len(list(BUNDLED_PRESETS_DIR.glob("*.yaml")))
    for item in presets:
        assert set(item) == LIST_ITEM_KEYS
        assert isinstance(item["version"], str)
        assert isinstance(item["phases"], int)
        assert isinstance(item["hasBattery"], bool)
        assert isinstance(item["commandsCount"], int)


def test_the_list_is_sorted_by_brand_and_model_like_the_cloud():
    presets = list_bundled_presets()
    keys = [f"{p['brand']} {p['model']}".casefold() for p in presets]
    assert keys == sorted(keys)


def test_commands_count_is_the_number_of_commands():
    item = next(p for p in list_bundled_presets() if p["id"] == "deye-sg04lp3-solarman-v1")
    full = load_bundled_preset("deye-sg04lp3-solarman-v1")
    assert item["commandsCount"] == len(full["commands"])


def test_a_preset_loads_by_id_with_its_entity_map():
    preset = load_bundled_preset("deye-sg04lp3-solarman-v1")
    assert preset["id"] == "deye-sg04lp3-solarman-v1"
    assert preset["brand"] == "Deye"
    assert preset["entityMap"]["batterySoc"]
    assert isinstance(preset["version"], str)


def test_defaults_match_the_cloud_schema():
    """HaPresetSchema defaults `commands` to [] and `pvStrings` to 2."""
    for item in list_bundled_presets():
        preset = load_bundled_preset(item["id"])
        assert isinstance(preset["commands"], list)
        assert isinstance(preset["pvStrings"], int)


def test_an_unknown_id_is_none():
    assert load_bundled_preset("no-such-preset-v1") is None


def test_an_id_that_leaves_the_directory_is_refused():
    assert load_bundled_preset("../manifest") is None
    assert load_bundled_preset("..") is None
    assert load_bundled_preset("") is None
