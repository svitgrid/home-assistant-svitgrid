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
    """25+25+13 = 63 registers, stitched back in address order.

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

    assert result == list(range(115, 115 + 63))
    # Exactly ONE transport call (one connection), carrying all 3 chunk ranges.
    assert len(calls) == 1
    assert [r[2] for r in calls[0]] == [25, 25, 13]
    assert [r[1] for r in calls[0]] == [115, 140, 165]


async def test_returns_none_on_short_chunk(monkeypatch):
    """Chunk 2 comes back short (20/25 registers) within the single batched
    call -> whole read is None, never a partial list. Mirrors how the real
    transport behaves when one range's read fails mid-connection: it skips
    just that range's addresses rather than raising, so the completeness
    check (every address present) is what must catch this."""

    def fake_read_solarman(cfg, ranges):
        out: dict = {}
        for i, (unit_id, start, count, fc) in enumerate(ranges):
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
    result = await read_config_registers(
        hass, _cfg(), "victron_multiplus_ii_gx_6k5", chunk_size=25
    )

    assert result is None


@pytest.mark.parametrize("bad_chunk_size", [0, -1])
async def test_nonpositive_chunk_size_raises(bad_chunk_size):
    """chunk_size <= 0 would never advance offset -> raise instead of hanging."""
    hass = _FakeHass()

    with pytest.raises(ValueError):
        await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=bad_chunk_size)
