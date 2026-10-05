"""`gridPortPower` must be mappable, sent as-is, and kept out of every preset.

Why this exists
---------------
A Deye with an external current transformer (CT) has two grid figures: the
inverter's own grid port (register 169 on 1-phase, `sensor.inverter_grid_power`
in the Solarman profile) and the mains as the clamp sees it (register 172,
`sensor.inverter_external_power`). The presets map `gridPower` to the port, so a
household whose CT reads 2400 W saw import and consumption at the port's 743 W
(svitgrid#843, SG-H99YXC).

The API already accepts `gridPortPower` and publishes
`gridSideLoadPower = gridPower - gridPortPower` from the first frame. What was
missing is a way to map it. A CT household then maps `gridPower` to the external
sensor and `gridPortPower` to the port sensor, in its own options form.

It must NOT go into a preset. Presets reach households by add-only merge, and a
household that keeps `gridPower` on the port would then send two identical
figures. The API reads identical figures as "no CT here" and withholds the
grid-side split, even where a CT is fitted and its statistical verdict says so.
"""

from __future__ import annotations

import glob
import os

import pytest
import yaml

from custom_components.svitgrid.const import (
    ALL_FIELDS,
    CORE_PAYLOAD_FIELDS,
    MAPPABLE_FIELDS,
)
from custom_components.svitgrid.readings_publisher import (
    assemble_payload,
    unresolved_fields,
)

PRESETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "presets")


def test_grid_port_power_is_mappable():
    keys = {field for field, _label in MAPPABLE_FIELDS}
    assert "gridPortPower" in ALL_FIELDS
    assert "gridPortPower" in keys


def test_grid_port_power_is_optional():
    """A household without a CT never maps it, and its readings must still send."""
    assert "gridPortPower" not in CORE_PAYLOAD_FIELDS


def test_assemble_payload_sends_grid_port_power_under_the_api_name():
    """The ingest schema knows `gridPortPower` as-is; no rename applies."""
    payload = assemble_payload(
        inverter_id="inv-1",
        fields={"gridPower": 2400.0, "gridPortPower": 743.0},
    )
    assert payload["gridPower"] == 2400.0
    assert payload["gridPortPower"] == 743.0


def test_negative_grid_port_power_survives():
    """The port exports while the mains imports when the battery feeds the
    grid-side circuits, so a negative port figure is real data."""
    payload = assemble_payload(
        inverter_id="inv-1",
        fields={"gridPower": 7.0, "gridPortPower": -2206.0},
    )
    assert payload["gridPortPower"] == -2206.0


def test_unmapped_grid_port_power_is_not_reported_unresolved():
    entity_map = {"gridPower": "sensor.inverter_grid_power"}
    payload = assemble_payload(inverter_id="i", fields={"gridPower": 100.0})
    assert unresolved_fields(payload, entity_map) == []


@pytest.mark.parametrize(
    "path",
    sorted(glob.glob(os.path.join(PRESETS_DIR, "*.yaml"))),
    ids=lambda p: os.path.basename(p)[:-5],
)
def test_no_preset_maps_grid_port_power(path):
    with open(path) as fh:
        preset = yaml.safe_load(fh)
    assert "gridPortPower" not in preset["entityMap"], (
        "gridPortPower is a per-household CT mapping; in a preset it reaches "
        "households whose gridPower is still the port, and identical figures "
        "make the API withhold a real CT's grid-side split"
    )
