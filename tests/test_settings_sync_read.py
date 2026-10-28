"""Tests for settings_sync.read_config_registers: chunked read + stitch + guard.

Uses a fake transport (monkeypatched onto harvest.transport) so no real
socket I/O happens. Mirrors the `_FakeHass` convention used in
tests/harvest/test_transport_write.py.
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.harvest import transport
from custom_components.svitgrid.settings_sync import read_config_registers


class _FakeHass:
    """Minimal hass stub that runs executor jobs synchronously."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _cfg(protocol: str = "solarman_v5") -> dict:
    return {
        "ip": "192.168.1.50",
        "logger_serial": "12345",
        "port": "8899" if protocol == "solarman_v5" else "502",
        "slave_id": "1",
        "protocol": protocol,
    }


async def test_stitches_three_chunks_in_order(monkeypatch):
    """25+25+25+2 = 77 registers (widened 2026-07-28 for peak-shaving regs
    178/191), stitched back in address order.

    Item 6 (2026-07-25 final review): all chunk ranges are batched into a
    SINGLE transport call (one TCP connection), not one call per chunk — the
    transport (_read_solarman/_read_modbus) already accepts a list of ranges
    and reads them all inside one connection. The fake here mimics that: it
    receives every chunk's range in one `ranges` list and builds the combined
    result across all of them, same as the real transport would.
    """
    calls: list[list[tuple]] = []

    def fake_read_solarman(cfg, ranges):
        calls.append(ranges)
        out: dict = {}
        for unit_id, start, count, fc in ranges:
            assert fc == "FC03"
            slot = out.setdefault(unit_id, {})
            # Each register's value == its own address, so stitch-order bugs show up.
            for addr in range(start, start + count):
                slot[addr] = addr
        return out

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)

    hass = _FakeHass()
    result = await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=25)

    assert result == list(range(115, 115 + 77))
    # Exactly ONE transport call (one connection), carrying all 4 chunk ranges.
    assert len(calls) == 1
    assert [r[2] for r in calls[0]] == [25, 25, 25, 2]
    assert [r[1] for r in calls[0]] == [115, 140, 165, 190]


async def test_returns_none_on_short_chunk(monkeypatch):
    """Chunk 2 comes back short (20/25 registers) within the single batched
    call -> whole read is None, never a partial list. Mirrors how the real
    transport behaves when one range's read fails mid-connection: it skips
    just that range's addresses rather than raising, so the completeness
    check (every address present) is what must catch this."""

    def fake_read_solarman(cfg, ranges):
        out: dict = {}
        for i, (unit_id, start, count, _fc) in enumerate(ranges):
            slot = out.setdefault(unit_id, {})
            n = 20 if i == 1 else count  # chunk index 1 == "chunk 2" comes back short
            for addr in range(start, start + n):
                slot[addr] = addr
        return out

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)

    hass = _FakeHass()
    result = await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=25)

    assert result is None


async def test_returns_none_on_transport_error(monkeypatch):
    """Any chunk raising -> None, never a partial list."""

    def fake_read_solarman(cfg, ranges):
        raise OSError("connection refused")

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)

    hass = _FakeHass()
    result = await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=25)

    assert result is None


async def test_unsupported_model_returns_none():
    """Model not in CONFIG_RANGES -> None, no transport call at all."""
    hass = _FakeHass()
    result = await read_config_registers(hass, _cfg(), "victron_multiplus_ii_gx_6k5", chunk_size=25)

    assert result is None


@pytest.mark.parametrize("bad_chunk_size", [0, -1])
async def test_nonpositive_chunk_size_raises(bad_chunk_size):
    """chunk_size <= 0 would never advance offset -> raise instead of hanging."""
    hass = _FakeHass()

    with pytest.raises(ValueError):
        await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=bad_chunk_size)


# ── Brand-aware settings reads (settings_read_plan) ────────────────────

import json
from pathlib import Path

from custom_components.svitgrid.executors.smg_settings_executor import (
    EybondSmgSettingsExecutor,
)
from custom_components.svitgrid.settings_read_plan import (
    SOLIS_SETTINGS_ADDRESSES,
    settings_read_plan_for,
    settings_snapshot_kind_for,
)
from custom_components.svitgrid.settings_sync import read_settings_snapshot

_FIXTURES = Path(__file__).parent / "fixtures"

# The LuxPower family code "cBaa" as holding 7-8 carry it, low byte first.
_CBAA_WORDS = {7: ord("c") | ord("B") << 8, 8: ord("a") | ord("a") << 8}


def _stub_modbus(monkeypatch, overrides: dict | None = None, missing=()) -> list[list[tuple]]:
    """Stub the Modbus TCP read: each register holds its own address unless
    ``overrides`` says otherwise; a range starting at an address in
    ``missing`` does not answer, as the real transport skips a failed range."""
    calls: list[list[tuple]] = []

    def fake_read_modbus(cfg, ranges):
        calls.append(ranges)
        out: dict = {}
        for unit_id, start, count, fc in ranges:
            assert fc == "FC03"
            if start in missing:
                continue
            slot = out.setdefault(unit_id, {})
            for addr in range(start, start + count):
                slot[addr] = (overrides or {}).get(addr, addr & 0xFFFF)
        return out

    monkeypatch.setattr(transport, "_read_modbus", fake_read_modbus)
    return calls


def _tcp_cfg(model_id: str, slave_id: int = 1, port: int = 502) -> dict:
    return {
        "protocol": "modbus_tcp",
        "ip": "192.168.1.60",
        "port": port,
        "slave_id": slave_id,
        "model_id": model_id,
    }


def test_kind_table_names_representative_models():
    """Mirrors `settingsSnapshotKindFor` in the monorepo's
    `packages/inverter_protocol/lib/src/registers/settings_register_mapping.dart`.
    The check against the shared `settings_snapshot_kinds.json` fixture
    arrives with the cross-repo contract task; until then these are the
    representative ids per kind, negatives included."""
    expected = {
        "deye_sg04lp3": "deye",
        "deye_sg02lp1": "deye",
        "sunsynk_3phase": "deye",
        "solark_12k": "deye",
        "luxpower_sna_5000_wpv": "luxpower",
        "anenji_anj_6200": "smg",
        "anenji_anj_11kw": "smg",
        "edecoa_em_624a": "pi30",
        "must_pv18_5248": "must",
        "must_ph18_5248": "must",
        "must_pv19_6048": "must",
        "solis_s6_eh3p_h": "solis",
        "solis_rhi_48es_5g": "solis",
        "solis_s6_eh1p_l_plus": "solis",
        # Nothing to configure: the generic screen's brands, and SolaX.
        "solis_30k_5g": None,
        "luxpower_lxp_lb_eu_7k": None,
        "eg4_18kpv": None,
        "huawei_sun2000_30ktl_m3": None,
        "solax_x3_hybrid_g4_15k": None,
        "victron_multiplus_ii_gx_6k5": None,
        "srne_anenji_12k": None,
        "": None,
        None: None,
    }
    assert {m: settings_snapshot_kind_for(m) for m in expected} == expected


def test_luxpower_plan_reads_five_blocks_of_40():
    """The firmware's `BLOCKS` in `luxpower_settings.c`: holding 0-199 in five
    blocks of 40, which is also how the app parses the body."""
    plan = settings_read_plan_for("luxpower_sna_5000_wpv", _tcp_cfg("x", port=8899), False)
    assert plan is not None
    assert plan.kind == "luxpower"
    assert plan.blocks == ((0, 40), (40, 40), (80, 40), (120, 40), (160, 40))
    assert sum(count for _start, count in plan.blocks) == 200


async def test_luxpower_read_decodes_the_family_code_from_holding_7_and_8(monkeypatch):
    calls = _stub_modbus(monkeypatch, overrides=_CBAA_WORDS)
    cfg = _tcp_cfg("luxpower_sna_5000_wpv", port=8899)
    plan = settings_read_plan_for("luxpower_sna_5000_wpv", cfg, False)

    snapshot = await read_settings_snapshot(
        _FakeHass(), plan, "inv-lux", "luxpower_sna_5000_wpv", cfg
    )

    assert len(calls) == 1  # one connection
    body = snapshot["body"]
    assert body["protocol"] == "luxpower"
    assert body["familyCode"] == "cBaa"
    assert [b["startRegister"] for b in body["blocks"]] == [0, 40, 80, 120, 160]
    assert all(len(b["registers"]) == 40 for b in body["blocks"])
    assert body["blocks"][0]["registers"][7] == _CBAA_WORDS[7]
    assert body["blocks"][4]["registers"][39] == 199


async def test_luxpower_read_without_a_printable_family_code_fails(monkeypatch):
    """`luxpower_build_sync_body` posts no body without one."""
    _stub_modbus(monkeypatch, overrides={7: 0, 8: 0})
    cfg = _tcp_cfg("luxpower_sna_5000_wpv", port=8899)
    plan = settings_read_plan_for("luxpower_sna_5000_wpv", cfg, False)

    assert (
        await read_settings_snapshot(_FakeHass(), plan, "inv-lux", "luxpower_sna_5000_wpv", cfg)
        is None
    )


def test_must_plan_reads_two_blocks():
    """`BLOCKS` in the firmware's `must_settings.c`, plus the edition (20006)
    and pack (25275) words it reads after them."""
    plan = settings_read_plan_for("must_pv18_5248", _tcp_cfg("x", slave_id=4), False)
    assert plan is not None
    assert plan.kind == "must"
    assert plan.blocks == ((10101, 12), (20101, 43))
    assert plan.extra == {"optional": (20006, 25275)}


async def test_must_read_carries_edition_and_pack_and_avoids_forbidden_ranges(monkeypatch):
    calls = _stub_modbus(monkeypatch, overrides={20006: 10415, 25275: 24})
    cfg = _tcp_cfg("must_pv18_5248", slave_id=4)
    plan = settings_read_plan_for("must_pv18_5248", cfg, False)

    snapshot = await read_settings_snapshot(_FakeHass(), plan, "inv-must", "must_pv18_5248", cfg)

    body = snapshot["body"]
    assert body["protocol"] == "must"
    assert body["edition"] == "1.04.15"  # must_format_edition
    assert body["packVoltage"] == 24
    assert [(b["startRegister"], len(b["registers"])) for b in body["blocks"]] == [
        (10101, 12),
        (20101, 43),
    ]
    # Reading across these shuts a MUST down (`kMustForbiddenReadRanges`).
    for unit_id, start, count, _fc in calls[0]:
        assert unit_id == 4
        end = start + count - 1
        assert not (start <= 10100 and end >= 10009)
        assert not (start <= 20100 and end >= 20017)


async def test_must_read_leaves_out_an_unread_edition_and_an_unknown_pack(monkeypatch):
    """`must_build_sync_body` omits `edition` when 20006 did not answer and
    `packVoltage` unless 25275 holds 12, 24 or 48; the blocks still post."""
    _stub_modbus(monkeypatch, overrides={25275: 36}, missing=(20006,))
    cfg = _tcp_cfg("must_pv18_5248", slave_id=4)
    plan = settings_read_plan_for("must_pv18_5248", cfg, False)

    snapshot = await read_settings_snapshot(_FakeHass(), plan, "inv-must", "must_pv18_5248", cfg)

    assert "edition" not in snapshot["body"]
    assert "packVoltage" not in snapshot["body"]
    assert len(snapshot["body"]["blocks"]) == 2


async def test_must_read_with_a_missing_block_fails(monkeypatch):
    _stub_modbus(monkeypatch, missing=(10101,))
    cfg = _tcp_cfg("must_pv18_5248", slave_id=4)
    plan = settings_read_plan_for("must_pv18_5248", cfg, False)

    assert (
        await read_settings_snapshot(_FakeHass(), plan, "inv-must", "must_pv18_5248", cfg) is None
    )


class _FakeSession:
    def __init__(self, words: dict[int, int]) -> None:
        self.words = words
        self.reads: list[tuple[int, int]] = []

    async def read_registers(self, address, count, timeout_s=5.0):
        self.reads.append((address, count))
        return [self.words.get(a, a & 0xFFFF) for a in range(address, address + count)]


class _FakeHub:
    def __init__(self, serial: str, session) -> None:
        self.serial = serial
        self.session = session

    def session_for(self, serial):
        return self.session if serial == self.serial else None


def _eybond_cfg(model_id: str = "anenji_anj_6200") -> dict:
    return {"protocol": "eybond_at", "model_id": model_id, "inverter_serial": "SN1"}


async def test_smg_plan_reads_the_sync_blocks_over_the_eybond_session():
    """Protocol 11's sync blocks (`k_smg_ii_blocks` in the firmware's
    `smg_settings_sync.c`), read over the collector's live session. Not
    `read_all`: the app maps the raw blocks, not display values."""
    cfg = _eybond_cfg()
    plan = settings_read_plan_for("anenji_anj_6200", cfg, True)
    assert plan is not None and plan.kind == "smg"
    session = _FakeSession({184: 11})

    snapshot = await read_settings_snapshot(
        _FakeHass(), plan, "inv-smg", "anenji_anj_6200", cfg, eybond_hub=_FakeHub("SN1", session)
    )

    assert session.reads == [(184, 1), (303, 1), (313, 31)]
    assert snapshot["body"] == {
        "protocolNumber": 11,
        "blocks": [
            {"startRegister": 303, "registers": [303]},
            {"startRegister": 313, "registers": list(range(313, 344))},
        ],
    }


async def test_smg_protocol_4_reads_its_own_blocks():
    session = _FakeSession({184: 4})
    body = await EybondSmgSettingsExecutor(
        hub=_FakeHub("SN1", session), inverter_serial="SN1"
    ).read_sync_body()

    assert body["protocolNumber"] == 4
    assert [(b["startRegister"], len(b["registers"])) for b in body["blocks"]] == [
        (600, 10),
        (630, 27),
        (677, 2),
    ]


async def test_smg_read_fails_without_a_connected_collector_or_a_known_protocol():
    cfg = _eybond_cfg()
    plan = settings_read_plan_for("anenji_anj_6200", cfg, True)
    errors: list[str] = []

    absent = await read_settings_snapshot(
        _FakeHass(),
        plan,
        "inv-smg",
        "anenji_anj_6200",
        cfg,
        eybond_hub=_FakeHub("OTHER", None),
        errors_out=errors,
    )
    unknown = await read_settings_snapshot(
        _FakeHass(),
        plan,
        "inv-smg",
        "anenji_anj_6200",
        cfg,
        eybond_hub=_FakeHub("SN1", _FakeSession({184: 7})),
    )

    assert absent is None and "no collector connected" in errors[0]
    assert unknown is None


def test_solis_plan_reads_catalogue_addresses():
    """The addresses `SolisSettingsExecutor._readRaw` reads, hand-copied into
    the fixture from the monorepo's `solis/settings.dart`."""
    fixture = json.loads((_FIXTURES / "solis_settings_addresses.json").read_text())
    assert "solis/settings.dart" in fixture["_source"]
    assert list(SOLIS_SETTINGS_ADDRESSES) == fixture["addresses"]

    plan = settings_read_plan_for("solis_s6_eh3p_h", _tcp_cfg("solis_s6_eh3p_h"), False)
    assert plan is not None
    assert plan.kind == "solis"
    covered = [a for start, count in plan.blocks for a in range(start, start + count)]
    assert covered == fixture["addresses"]


async def test_solis_read_keys_registers_by_decimal_address(monkeypatch):
    _stub_modbus(monkeypatch, overrides={43110: 33})
    cfg = _tcp_cfg("solis_s6_eh3p_h")
    plan = settings_read_plan_for("solis_s6_eh3p_h", cfg, False)

    snapshot = await read_settings_snapshot(_FakeHass(), plan, "inv-solis", "solis_s6_eh3p_h", cfg)

    registers = snapshot["body"]["registers"]
    assert snapshot["body"]["protocol"] == "solis"
    assert list(registers) == [str(a) for a in SOLIS_SETTINGS_ADDRESSES]
    assert registers["43110"] == 33


def test_unknown_model_has_no_plan():
    for model_id in (
        "huawei_sun2000_30ktl_m3",
        "solax_x3_hybrid_g4_15k",
        "victron_multiplus_ii_gx_6k5",
        "solis_30k_5g",
        "luxpower_lxp_lb_eu_7k",
        "eg4_18kpv",
        "not_a_model",
    ):
        assert settings_read_plan_for(model_id, _tcp_cfg(model_id), False) is None, model_id


def test_a_kind_this_add_on_cannot_read_has_no_plan():
    """PI30 has settings but no harvest here; SMG II is read only over an
    EyBond collector, and a collector serves nothing else."""
    assert settings_read_plan_for("edecoa_em_624a", _tcp_cfg("edecoa_em_624a"), False) is None
    assert settings_read_plan_for("anenji_anj_6200", _tcp_cfg("anenji_anj_6200"), False) is None
    assert settings_read_plan_for("deye_sg04lp3", _eybond_cfg("deye_sg04lp3"), True) is None


def test_deye_plan_is_the_config_range():
    plan = settings_read_plan_for("deye_sg04lp3", _tcp_cfg("deye_sg04lp3"), False)
    assert plan is not None
    assert plan.kind == "deye"
    assert plan.blocks == ((115, 77),)
