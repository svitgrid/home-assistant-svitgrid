"""Tests for harvest/transport.py — plan_ranges (pure) + client stubs."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.transport import plan_ranges


def _spec(reads, protocol="solarman_v5"):
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": protocol, "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": reads, "derivations": [], "writes": [],
    })


# ---------------------------------------------------------------------------
# plan_ranges — pure logic tests (5 from brief)
# ---------------------------------------------------------------------------

def test_contiguous_reads_grouped_into_one_range():
    spec = _spec([{"field": "a", "address": 100}, {"field": "b", "address": 101},
                  {"field": "c", "address": 102}])
    ranges = plan_ranges(spec)
    assert ranges == [(1, 100, 3, "FC03")]


def test_32bit_read_spans_two_registers():
    spec = _spec([{"field": "e", "address": 200, "words": 2}])
    assert plan_ranges(spec) == [(1, 200, 2, "FC03")]


def test_distant_reads_split_into_separate_ranges():
    spec = _spec([{"field": "a", "address": 100}, {"field": "z", "address": 900}])
    ranges = plan_ranges(spec)
    assert (1, 100, 1, "FC03") in ranges and (1, 900, 1, "FC03") in ranges
    assert len(ranges) == 2


def test_per_unit_id_grouped_separately():
    spec = _spec([{"field": "a", "address": 843, "unitId": 100},
                  {"field": "b", "address": 784, "unitId": 247}], protocol="modbus_tcp")
    ranges = plan_ranges(spec)
    units = {r[0] for r in ranges}
    assert units == {100, 247}


def test_fc04_grouped_separately_from_fc03():
    spec = _spec([{"field": "a", "address": 10, "functionCode": "FC03"},
                  {"field": "b", "address": 11, "functionCode": "FC04"}])
    fcs = {r[3] for r in plan_ranges(spec)}
    assert fcs == {"FC03", "FC04"}


# ---------------------------------------------------------------------------
# plan_ranges — MAX_RANGE cap: a gap > MAX_RANGE forces a split
# ---------------------------------------------------------------------------

def test_max_range_cap_splits_long_run():
    """A contiguous block > MAX_RANGE (100) must be split."""
    # 101 addresses: 0..100 inclusive — should produce two ranges
    reads = [{"field": f"f{i}", "address": i} for i in range(101)]
    ranges = plan_ranges(_spec(reads))
    assert len(ranges) == 2
    counts = sorted(r[2] for r in ranges)
    assert sum(counts) == 101
    assert all(c <= 100 for c in counts)


# ---------------------------------------------------------------------------
# _read_solarman — happy path via monkeypatched stub
# ---------------------------------------------------------------------------

def test_read_solarman_assembles_raw_registers(monkeypatch):
    """Stub PySolarmanV5; assert RawRegisters maps addr->word correctly."""
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()
    # A 2-register range [100, 101] returns [0xAB, 0xCD]
    fake_instance.read_holding_registers.return_value = [0xAB, 0xCD]

    FakeClass = MagicMock(return_value=fake_instance)

    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeClass  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        cfg = {"ip": "192.168.1.1", "logger_serial": "123456789", "port": "8899", "slave_id": "1"}
        ranges = [(1, 100, 2, "FC03")]
        result = _read_solarman(cfg, ranges)

    # Constructor called with ip + serial as positionals; auto_reconnect=True since 2026-07-01
    FakeClass.assert_called_once_with(
        "192.168.1.1",
        123456789,
        port=8899,
        mb_slave_id=1,
        socket_timeout=3,
        auto_reconnect=True,
    )
    # read called with positional-style kwargs
    fake_instance.read_holding_registers.assert_called_once_with(
        register_addr=100, quantity=2
    )
    # Result assembled correctly
    assert result == {1: {100: 0xAB, 101: 0xCD}}
    # disconnect called
    fake_instance.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# _read_modbus — happy path via monkeypatched stub
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _read_solarman — resilience: partial success, per-range retry, all-fail, setup retry
# ---------------------------------------------------------------------------

def _fake_solarman_module(fake_instance=None, ctor_side_effect=None):
    """Return a (fake_mod, FakeClass) pair for patching pysolarmanv5."""
    if fake_instance is None:
        fake_instance = MagicMock()
    if ctor_side_effect is not None:
        FakeClass = MagicMock(side_effect=ctor_side_effect)
    else:
        FakeClass = MagicMock(return_value=fake_instance)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeClass  # type: ignore[attr-defined]
    return fake_mod, FakeClass, fake_instance


def test_read_solarman_partial_success_skips_failing_range():
    """(a) Some ranges raise, others succeed → partial RawRegisters returned, no exception."""
    from unittest.mock import patch
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()

    def _read_side_effect(*, register_addr, quantity):
        if register_addr == 100:
            return [0xAB]
        raise OSError("logger hiccup")

    fake_instance.read_holding_registers.side_effect = _read_side_effect
    fake_mod, _, _ = _fake_solarman_module(fake_instance=fake_instance)

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}), patch("time.sleep"):
        cfg = {"ip": "192.168.1.1", "logger_serial": "123", "port": "8899", "slave_id": "1"}
        result = _read_solarman(cfg, [(1, 100, 1, "FC03"), (1, 200, 1, "FC03")])

    # addr 100 succeeded; addr 200 failed both attempts and was skipped
    assert result == {1: {100: 0xAB}}


def test_read_solarman_range_retry_succeeds_on_reconnect():
    """(b) A range fails on first read but succeeds on retry → registers present."""
    from unittest.mock import patch
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()
    call_count = {"n": 0}

    def _read_side_effect(*, register_addr, quantity):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("broken pipe")
        return [0xBB]

    fake_instance.read_holding_registers.side_effect = _read_side_effect
    fake_mod, _, _ = _fake_solarman_module(fake_instance=fake_instance)

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}), patch("time.sleep"):
        cfg = {"ip": "192.168.1.1", "logger_serial": "123", "port": "8899", "slave_id": "1"}
        result = _read_solarman(cfg, [(1, 100, 1, "FC03")])

    # First attempt raised; retry succeeded
    assert result == {1: {100: 0xBB}}
    assert call_count["n"] == 2


def test_read_solarman_all_ranges_fail_raises():
    """(c) Every range raises on both attempts → _read_solarman raises (zero registers)."""
    import pytest
    from unittest.mock import patch
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()
    fake_instance.read_holding_registers.side_effect = OSError("logger dead")
    fake_mod, _, _ = _fake_solarman_module(fake_instance=fake_instance)

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}), patch("time.sleep"):
        cfg = {"ip": "192.168.1.1", "logger_serial": "123", "port": "8899", "slave_id": "1"}
        with pytest.raises(Exception):
            _read_solarman(cfg, [(1, 100, 1, "FC03"), (1, 200, 1, "FC03")])


def test_read_solarman_connection_setup_retry_succeeds():
    """(d) Constructor raises the first 2 attempts then succeeds → registers read normally."""
    from unittest.mock import patch
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()
    fake_instance.read_holding_registers.return_value = [0xCC]
    ctor_count = {"n": 0}

    def _ctor_side_effect(*args, **kwargs):
        ctor_count["n"] += 1
        if ctor_count["n"] < 3:
            raise OSError("connection refused")
        return fake_instance

    fake_mod, FakeClass, _ = _fake_solarman_module(ctor_side_effect=_ctor_side_effect)

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}), patch("time.sleep") as mock_sleep:
        cfg = {"ip": "192.168.1.1", "logger_serial": "123", "port": "8899", "slave_id": "1"}
        result = _read_solarman(cfg, [(1, 100, 1, "FC03")])

    assert result == {1: {100: 0xCC}}
    assert ctor_count["n"] == 3  # failed twice, succeeded on third attempt
    assert mock_sleep.call_count >= 2  # backoff called between failed attempts


def test_read_modbus_assembles_raw_registers(monkeypatch):
    """Stub ModbusTcpClient; assert RawRegisters maps addr->word correctly."""
    from custom_components.svitgrid.harvest.transport import _read_modbus

    fake_result = MagicMock()
    fake_result.isError.return_value = False
    fake_result.registers = [0x11, 0x22]

    fake_client = MagicMock()
    fake_client.read_holding_registers.return_value = fake_result

    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]

    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {
        "pymodbus": fake_pymodbus,
        "pymodbus.client": fake_client_mod,
    }):
        cfg = {"ip": "10.0.0.1", "port": "502", "slave_id": "1"}
        ranges = [(247, 784, 2, "FC03")]
        result = _read_modbus(cfg, ranges)

    FakeClientClass.assert_called_once_with("10.0.0.1", port=502, timeout=8)
    fake_client.connect.assert_called_once()
    fake_client.read_holding_registers.assert_called_once_with(
        784, count=2, device_id=247
    )
    assert result == {247: {784: 0x11, 785: 0x22}}
    fake_client.close.assert_called_once()


def test_read_modbus_fc04_uses_input_registers(monkeypatch):
    """FC04 ranges must use read_input_registers."""
    from custom_components.svitgrid.harvest.transport import _read_modbus

    fake_result = MagicMock()
    fake_result.isError.return_value = False
    fake_result.registers = [0x55]

    fake_client = MagicMock()
    fake_client.read_input_registers.return_value = fake_result

    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {
        "pymodbus": fake_pymodbus,
        "pymodbus.client": fake_client_mod,
    }):
        cfg = {"ip": "10.0.0.1", "port": "502", "slave_id": "1"}
        ranges = [(1, 10, 1, "FC04")]
        result = _read_modbus(cfg, ranges)

    fake_client.read_input_registers.assert_called_once_with(10, count=1, device_id=1)
    fake_client.read_holding_registers.assert_not_called()
    assert result == {1: {10: 0x55}}
