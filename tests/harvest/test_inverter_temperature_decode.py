"""Direct harvest reads inverter temperature (svitgrid#1328).

The bundled register specs are the monorepo's generated ones
(`scripts/sync-register-specs.sh`). Since svitgrid#1325 they read
`inverterTemperature` with the Deye-family bias (x0.1 - 100) and keep it
inside the `battery_temp_clamp` window, so raw 0 (-100 degC) is absent, not
a cold inverter. The values match the Dart golden vectors.
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.harvest.decoder import decode, sanitize
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.spec_source import load_bundled_spec

# (model, address of the inverter temperature register)
_DEYE_FAMILY = [
    ("deye_sg01hp3_30k", 541),
    ("deye_sg02hp3_80k", 541),
    ("sunsynk_3phase", 541),
    ("solark_12k", 91),
]


def _spec(model_id: str) -> RegisterSpec:
    doc = load_bundled_spec(model_id)
    assert doc is not None, model_id
    return RegisterSpec.from_dict(doc)


def _read_of(spec: RegisterSpec, field: str):
    return next((r for r in spec.reads if r.field == field), None)


@pytest.mark.parametrize(("model_id", "address"), _DEYE_FAMILY)
def test_the_bundled_spec_reads_inverter_temperature(model_id, address):
    read = _read_of(_spec(model_id), "inverterTemperature")
    assert read is not None, f"{model_id} has no inverterTemperature read"
    assert read.address == address
    assert read.scale == pytest.approx(0.1)
    assert read.offset == pytest.approx(-100.0)


@pytest.mark.parametrize(("model_id", "address"), _DEYE_FAMILY)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1450, 45.0),  # 45 degC
        (950, -5.0),  # sub-zero is a real reading
        (0, None),  # raw 0 is the "no sensor" sentinel, -100 degC
    ],
)
def test_the_bundled_spec_decodes_inverter_temperature(model_id, address, raw, expected):
    spec = _spec(model_id)
    unit = _read_of(spec, "inverterTemperature").unit_id
    out = sanitize(decode(spec, {unit: {address: raw}}), spec)
    if expected is None:
        assert out.get("inverterTemperature") is None
    else:
        assert out["inverterTemperature"] == pytest.approx(expected)
