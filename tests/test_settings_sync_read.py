"""Tests for settings_sync.read_config_registers: chunked read + stitch + guard.

Uses a fake transport (monkeypatched onto harvest.transport) so no real
socket I/O happens. Mirrors the `_FakeHass` convention used in
tests/harvest/test_transport_write.py.
"""

from __future__ import annotations

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
    """25+25+13 = 63 registers, stitched back in address order."""
    calls: list[tuple] = []

    def fake_read_solarman(cfg, ranges):
        calls.append(ranges)
        unit_id, start, count, fc = ranges[0]
        assert fc == "FC03"
        # Each register's value == its own address, so stitch-order bugs show up.
        return {unit_id: {addr: addr for addr in range(start, start + count)}}

    monkeypatch.setattr(transport, "_read_solarman", fake_read_solarman)

    hass = _FakeHass()
    result = await read_config_registers(hass, _cfg(), "deye_sg04lp3", chunk_size=25)

    assert result == list(range(115, 115 + 63))
    # 3 chunks: 25, 25, 13
    assert [r[0][2] for r in calls] == [25, 25, 13]
    assert [r[0][1] for r in calls] == [115, 140, 165]


async def test_returns_none_on_short_chunk(monkeypatch):
    """Chunk 2 returns 20/25 registers -> whole read is None, never a partial list."""
    call_count = {"n": 0}

    def fake_read_solarman(cfg, ranges):
        call_count["n"] += 1
        unit_id, start, count, fc = ranges[0]
        if call_count["n"] == 2:
            # Short read: only 20 of the requested 25 registers come back.
            return {unit_id: {addr: addr for addr in range(start, start + 20)}}
        return {unit_id: {addr: addr for addr in range(start, start + count)}}

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
