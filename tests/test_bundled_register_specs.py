"""Register specs ship inside the component, so direct Modbus harvest starts
without the cloud.

The bundle is a copy of the monorepo's `packages/inverter_protocol/
register-specs/`, refreshed by `scripts/sync-register-specs.sh`. The cloud's
`GET /api/v1/register-specs/{modelId}` returns the same documents (it adds
`updatedAt`), so `build_spec` must accept every bundled file as it is. A file
it refuses would pair, look configured, and read nothing.
"""

from __future__ import annotations

import json

import pytest

from custom_components.svitgrid.harvest.spec_health import build_spec
from custom_components.svitgrid.harvest.spec_source import (
    BUNDLED_SPECS_DIR,
    load_bundled_spec,
)

_FILES = sorted(BUNDLED_SPECS_DIR.glob("*.json"))


def test_the_bundle_is_not_empty():
    assert len(_FILES) > 10


@pytest.mark.parametrize("path", _FILES, ids=[p.stem for p in _FILES])
def test_every_bundled_spec_builds(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["modelId"] == path.stem, "file name must be the modelId"
    assert build_spec(doc, model_id=doc["modelId"]) is not None


def test_a_spec_loads_by_model_id():
    spec = load_bundled_spec("deye_sg04lp3")
    assert spec["modelId"] == "deye_sg04lp3"
    assert spec["reads"]


def test_an_unknown_model_is_none():
    assert load_bundled_spec("no_such_model") is None


def test_a_model_id_that_leaves_the_directory_is_refused():
    assert load_bundled_spec("../manifest") is None
    assert load_bundled_spec("") is None
