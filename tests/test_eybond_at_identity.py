"""Device identification and register-map dispatch.

The rule this module enforces: **choose the register map from the DEVICE, never
from a brand or a model name the user picked during onboarding.**

Anenji already spans two register maps. `srne_anenji_12k` is hardware-proven on
the SRNE ASF-HF map, while the SmartESS family clones EASUN/ISolar SMG II. And
inside SMG II the map is versioned by a number the device reports about itself
at register 184: our bench unit reads 11, and the most complete published map
documents protocols 3 to 6 and disagrees with our hardware on the very first
telemetry register.

So an unrecognised protocol number publishes nothing. A wrong map produces
plausible numbers, which is the failure mode nobody notices.
"""

import pytest

from custom_components.svitgrid.eybond_at.identity import (
    REG_DEVICE_TYPE,
    REG_FIRMWARE,
    REG_PROTOCOL,
    REG_SERIAL,
    DeviceIdentity,
    UnknownPlatform,
    identify,
    resolve_map,
)
from custom_components.svitgrid.eybond_at.register_map import (
    SMG_II_PROTOCOL_11,
    Confidence,
)

# Captured 2026-08-20 from collector I20000282044487591.
CAPTURED_TYPE = 0x7803
CAPTURED_PROTOCOL = 11
CAPTURED_SERIAL_WORDS = [
    0x3939, 0x3433, 0x3236, 0x3034, 0x3130, 0x3731,
    0x3036, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
]  # fmt: skip
CAPTURED_FIRMWARE_WORDS = [
    0x3738, 0x3033, 0x5F41, 0x3632, 0x3630, 0x3132, 0x3676, 0x3100,
]  # fmt: skip
# Registers 201..215, verbatim from the capture.
CAPTURED_TELEMETRY = [
    0x0004, 0x08EC, 0x1387, 0x0000, 0x08FA, 0x0000, 0x1387, 0xFFFE,
    0x0000, 0x08FA, 0x0002, 0x1386, 0xFFFE, 0x002D, 0x0000,
]  # fmt: skip


class FakeLink:
    """Answers register reads from a canned table."""

    def __init__(self, table: dict[int, list[int]]):
        self.table = table
        self.reads: list[tuple[int, int]] = []

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0):
        self.reads.append((address, count))
        if address not in self.table:
            raise KeyError(f"unexpected read at {address}")
        return self.table[address][:count]


def captured_link(overrides: dict[int, list[int]] | None = None) -> FakeLink:
    table = {
        REG_DEVICE_TYPE: [CAPTURED_TYPE],
        REG_PROTOCOL: [CAPTURED_PROTOCOL],
        REG_SERIAL: CAPTURED_SERIAL_WORDS,
        REG_FIRMWARE: CAPTURED_FIRMWARE_WORDS,
    }
    table.update(overrides or {})
    return FakeLink(table)


class TestIdentify:
    async def test_reads_the_identity_block_from_the_device(self):
        link = captured_link()
        identity = await identify(link)
        assert identity == DeviceIdentity(
            protocol_number=11,
            device_type=0x7803,
            serial="99432604107106",
            firmware="7803_A6260126v1",
        )

    async def test_the_device_type_matches_the_firmware_prefix(self):
        """Register 171 is the firmware prefix as packed binary-coded decimal.

        `0x7803` renders as the four digits that open `7803_A6260126v1`. That
        correspondence is what makes the register trustworthy as an identifier.
        """
        identity = await identify(captured_link())
        assert f"{identity.device_type:04x}" == identity.firmware[:4]

    async def test_a_missing_firmware_string_is_not_fatal(self):
        # Identity must still resolve without it: the protocol number is what
        # selects the map, and the firmware string is corroboration.
        link = captured_link({REG_FIRMWARE: [0] * 8})
        identity = await identify(link)
        assert identity.protocol_number == 11
        assert identity.firmware == ""


class TestDispatch:
    async def test_resolves_the_measured_platform(self):
        identity = await identify(captured_link())
        assert resolve_map(identity) is SMG_II_PROTOCOL_11

    def test_refuses_an_unrecognised_protocol_number(self):
        # Protocols 3-6 are documented publicly but disagree with our hardware
        # at register 202, so they are NOT in the allowlist until measured.
        for protocol in (3, 4, 5, 6, 0, 99):
            identity = DeviceIdentity(
                protocol_number=protocol, device_type=0x7803, serial="x", firmware=""
            )
            with pytest.raises(UnknownPlatform):
                resolve_map(identity)

    def test_the_error_names_the_protocol_number(self):
        identity = DeviceIdentity(protocol_number=4, device_type=0x3501, serial="x", firmware="")
        with pytest.raises(UnknownPlatform, match="4"):
            resolve_map(identity)


class TestDecoding:
    def test_decodes_the_captured_telemetry_block(self):
        values = SMG_II_PROTOCOL_11.decode_block(201, CAPTURED_TELEMETRY)
        assert values["gridVoltageL1"] == pytest.approx(228.4)
        assert values["gridFrequency"] == pytest.approx(49.99)
        assert values["loadVoltageL1"] == pytest.approx(229.8)

    def test_applies_the_signed_convention_where_the_field_needs_it(self):
        # Register 213 is output active power and arrives as 0xFFFE. Unsigned
        # it reads 65534 W, which is nonsense for an idle inverter.
        values = SMG_II_PROTOCOL_11.decode_block(201, CAPTURED_TELEMETRY)
        assert values["loadPower"] == -2

    def test_leaves_out_addresses_the_block_does_not_cover(self):
        values = SMG_II_PROTOCOL_11.decode_block(201, CAPTURED_TELEMETRY[:3])
        assert "gridVoltageL1" in values
        assert "batterySoc" not in values

    def test_every_field_maps_to_a_canonical_svitgrid_name(self):
        # A typo here publishes a field the API silently strips, and the app
        # shows nothing with no error anywhere.
        canonical = {
            "batteryCurrent", "batteryPower", "batterySoc", "batteryVoltage",
            "gridFrequency", "gridPower", "gridVoltageL1", "inverterTemperature",
            "loadPower", "loadVoltageL1", "pv1Current", "pv1Power", "pv1Voltage",
            "runningState",
        }  # fmt: skip
        for spec in SMG_II_PROTOCOL_11.fields:
            assert spec.field in canonical, f"{spec.field} is not a canonical field"


class TestConfidence:
    def test_the_ac_fields_measured_on_hardware_are_marked_confirmed(self):
        by_field = {s.field: s for s in SMG_II_PROTOCOL_11.fields}
        assert by_field["gridVoltageL1"].confidence is Confidence.CONFIRMED
        assert by_field["gridFrequency"].confidence is Confidence.CONFIRMED
        assert by_field["inverterTemperature"].confidence is Confidence.CONFIRMED

    def test_the_battery_and_pv_fields_are_not_claimed_as_confirmed(self):
        """The bench unit has no battery and no panels, so these read zero.

        Zero is undecidable, not evidence. Marking them confirmed would be the
        exact overclaim this project keeps getting caught by.
        """
        by_field = {s.field: s for s in SMG_II_PROTOCOL_11.fields}
        for field in ("batterySoc", "batteryVoltage", "pv1Voltage", "pv1Power"):
            assert by_field[field].confidence is Confidence.IDENTIFIED


class TestReadPlan:
    def test_covers_every_field_address(self):
        covered = set()
        for address, count in SMG_II_PROTOCOL_11.read_plan():
            covered.update(range(address, address + count))
        for spec in SMG_II_PROTOCOL_11.fields:
            assert spec.address in covered, f"{spec.field} is not polled"

    def test_no_block_exceeds_the_collector_read_limit(self):
        for _address, count in SMG_II_PROTOCOL_11.read_plan():
            assert 1 <= count <= 32  # the collector caps a read at 32 registers
