"""Protocol 4 is the SMG II 8/11 kW "phase-block" layout, not protocol 11's.

A customer's ANJ-11KW on an EyBond collector reports 184 = 4 and was refused
with "unrecognised protocol number 4" (svitgrid D-1781), so it never published
a reading. The monorepo already decodes it (`smgIiProtocol4` in
packages/inverter_protocol/.../eybond_at/register_map.dart); these vectors
mirror its `protocol_4_test.dart` so the two decoders stay in parity.

CAPTURED 2026-10-25 by the unknown-protocol sweep from an `anenji_anj_11kw`
(device type 0x8401, serial 92B32604100658), off grid and idle. Registers
200-263 land where the per-protocol table in gagara/easun-smg-ii-ha-config
puts them. The battery, L1 and PV blocks were outside the capture window, so
the values used for them below are synthetic and distinguishable.
"""

import pytest

from custom_components.svitgrid.eybond_at.harvest import poll_once
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
from custom_components.svitgrid.eybond_at.reader import EybondAtReader
from custom_components.svitgrid.eybond_at.register_map import (
    PLATFORMS,
    SMG_II_PROTOCOL_11,
    Confidence,
    OutputMode,
)

BASE = 200
CAPTURED_WINDOW = [
    0xB010, 0x0003, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0100,  # 200
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,  # 208
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,  # 216
    0x0000, 0x0000, 0x0000, 0x1388, 0x0000, 0x0000, 0x0000, 0x0024,  # 224
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,  # 232
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,  # 240
    0x0000, 0x0000, 0x0000, 0x0000, 0x0003, 0x1389, 0xFFFF, 0x0044,  # 248
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,  # 256
]  # fmt: skip

SYNTHETIC = {
    277: 532,  # battery 53.2 V
    278: 0xFF9C,  # battery -10.0 A (discharging)
    279: 0xFDEC,  # battery -532 W
    280: 77,  # SOC 77 %
    338: 2291,  # L1 grid 229.1 V
    340: 0xFF06,  # L1 grid -250 W
    346: 2302,  # L1 output 230.2 V
    351: 3105,  # PV1 310.5 V
    352: 42,  # PV1 4.2 A
    353: 1304,  # PV1 1304 W
    389: 2950,  # PV2 295.0 V
    390: 31,  # PV2 3.1 A
    391: 915,  # PV2 915 W
    600: 0,  # output mode: single
}


class Protocol4Link:
    """Every register the protocol-4 map reads; anything else reads zero."""

    def __init__(self):
        self.registers = {REG_PROTOCOL: 4, REG_DEVICE_TYPE: 0x8401}
        self.registers.update({BASE + i: w for i, w in enumerate(CAPTURED_WINDOW)})
        self.registers.update(SYNTHETIC)
        self.reads: list[tuple[int, int]] = []

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0):
        self.reads.append((address, count))
        if address == REG_SERIAL:
            serial = b"92B32604100658".ljust(count * 2, b"\0")
            return [(serial[i] << 8) | serial[i + 1] for i in range(0, count * 2, 2)]
        if address == REG_FIRMWARE:
            return [0] * count
        return [self.registers.get(address + i, 0) for i in range(count)]


class MemoryStore:
    def __init__(self):
        self.appended: list[dict] = []

    async def append(self, payload):
        self.appended.append(payload)


def _identity(protocol: int) -> DeviceIdentity:
    return DeviceIdentity(
        protocol_number=protocol, device_type=0x8401, serial="92B32604100658", firmware=""
    )


def test_protocol_4_resolves_to_its_own_map_not_protocol_11():
    register_map = resolve_map(_identity(4))
    assert register_map.protocol_numbers == (4,)
    assert register_map is not SMG_II_PROTOCOL_11


def test_the_captured_window_decodes_to_the_idle_off_grid_unit():
    values = PLATFORMS[4].decode_block(BASE, CAPTURED_WINDOW)
    assert values["runningState"] == 3, "Off-Grid mode"
    assert values["gridFrequency"] == 0, "no mains connected"
    assert values["inverterTemperature"] == 36
    assert values["loadPower"] == -1, "ffff is signed: -1 W at idle"


def test_nothing_is_read_where_protocol_11_keeps_its_telemetry():
    # 202 is grid CURRENT on this layout; decoding it as protocol 11 would put
    # 238 A on a dashboard as grid voltage.
    addresses = {spec.address for spec in PLATFORMS[4].fields}
    for p11_only in (202, 204, 210, 213, 215, 216, 217, 219, 220, 223, 229):
        assert p11_only not in addresses, f"register {p11_only}"


def test_keeps_its_output_mode_at_600_not_300():
    assert PLATFORMS[4].topology_register == 600


def test_every_field_is_identified_until_a_live_frame_confirms_it():
    assert all(spec.confidence is Confidence.IDENTIFIED for spec in PLATFORMS[4].fields)


def test_protocols_3_5_and_6_are_still_refused():
    for protocol in (3, 5, 6):
        with pytest.raises(UnknownPlatform):
            resolve_map(_identity(protocol))


async def test_identify_reads_the_output_mode_from_600():
    link = Protocol4Link()
    identity = await identify(link)
    assert identity.protocol_number == 4
    assert identity.output_mode is OutputMode.SINGLE
    assert (600, 1) in link.reads
    assert (300, 1) not in link.reads


async def test_a_full_poll_publishes_a_reading_with_both_pv_strings():
    store = MemoryStore()
    payload = await poll_once(
        reader=EybondAtReader(Protocol4Link()), inverter_id="ha-test", store=store
    )

    assert payload is not None, "a protocol 4 reading must not be refused or gated"
    assert store.appended == [payload]
    expected = {
        "gridVoltageL1": 229.1,
        "gridPower": -250,
        "gridFrequency": 0,
        "loadVoltageL1": 230.2,
        "loadPower": -1,
        "batteryVoltage": 53.2,
        "batteryCurrent": -10.0,
        "batteryPower": -532,
        "batterySoc": 77,
        "pvVoltage1": 310.5,
        "pvCurrent1": 4.2,
        "pvPower1": 1304,
        "pvVoltage2": 295.0,
        "pvCurrent2": 3.1,
        "pvPower2": 915,
        "pvPower": 2219,
        "inverterTemperature": 36,
        "runningState": 3,
    }
    for field, value in expected.items():
        assert payload[field] == pytest.approx(value, abs=0.001), field
