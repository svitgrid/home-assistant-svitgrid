"""Protocol 1 shares protocol 11's telemetry layout.

MEASURED 2026-08-29 from a customer's ANJ-5KW (device type 0x3501, serial
99432507102679). Register 184 reports 1; the block at 200 decodes on the same
layout as the bench unit's protocol 11. Captured by the mobile app's
unknown-protocol register sweep, which exists so a user who cannot use the app
can still supply the evidence needed to support their device.
"""

import pytest

from custom_components.svitgrid.eybond_at.register_map import (
    PLATFORMS,
    SMG_II_PROTOCOL_11,
)

# The real frame, registers 200-233, exactly as captured.
CAPTURE = [
    0x8480, 0x0002, 0x07D5, 0x138A, 0x0055, 0x07CD, 0x0005, 0x1388,
    0xFFD3, 0x0037, 0x07CD, 0x0005, 0x1388, 0x0027, 0x0063, 0x0120,
    0xFFF2, 0x0000, 0x1195, 0x0146, 0x0000, 0x0000, 0x0000, 0x0000,
    0x0000, 0x0002, 0x0018, 0x001C, 0x0017, 0x0064, 0x0013, 0x0154,
    0x0000, 0x0013,
]


def test_protocol_1_and_11_select_the_same_map():
    assert PLATFORMS[1] is SMG_II_PROTOCOL_11
    assert PLATFORMS[11] is SMG_II_PROTOCOL_11, "11 must keep working"


def test_unmeasured_protocols_are_still_refused():
    # Widening to 1 must not widen to everything: the 8/11 kW map for
    # protocols 3-6 was refuted against this very frame.
    for protocol in (0, 3, 6, 12):
        assert protocol not in PLATFORMS


@pytest.mark.parametrize(
    "field,expected",
    [
        ("gridVoltageL1", 200.5),
        ("gridFrequency", 50.02),
        ("gridPower", 85),
        ("loadVoltageL1", 199.7),
        ("loadPower", 39),
        ("batteryVoltage", 28.8),
        ("batteryCurrent", -1.4),
        ("batteryPower", 0),
        ("pv1Voltage", 32.6),
        ("pv1Current", 0),
        ("pv1Power", 0),
        ("inverterTemperature", 28),
        ("batterySoc", 100),
    ],
)
def test_decodes_the_customer_capture(field, expected):
    values = PLATFORMS[1].decode_block(200, CAPTURE)
    assert values[field] == pytest.approx(expected, abs=0.01)
