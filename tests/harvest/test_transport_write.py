"""Tests for transport.py write path: write_registers + read_word (SP-C Task 8).

TDD — tests written first.  Implementation to follow in transport.py.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from custom_components.svitgrid.harvest.register_spec import RegisterSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(protocol="solarman_v5"):
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": protocol,
        "port": 8899 if protocol == "solarman_v5" else 502,
        "defaultSlaveId": 1, "flags": {}, "reads": [], "derivations": [], "writes": [],
    })


class _FakeHass:
    """Minimal hass stub that runs executor jobs synchronously."""
    async def async_add_executor_job(self, func, *args):
        return func(*args)


# ---------------------------------------------------------------------------
# _write_solarman — sync helper, happy path
# ---------------------------------------------------------------------------

def test_write_solarman_calls_write_holding_registers():
    """_write_solarman must call write_holding_registers once per (unit,addr,val)."""
    from custom_components.svitgrid.harvest.transport import _write_solarman

    fake_sm = MagicMock()
    FakeSM = MagicMock(return_value=fake_sm)

    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}
    writes = [(1, 200, 0xABCD)]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        _write_solarman(cfg, writes)

    fake_sm.write_holding_registers.assert_called_once_with(
        register_addr=200, values=[0xABCD]
    )


def test_write_solarman_multiple_writes():
    """_write_solarman writes every (unit, addr, val) tuple."""
    from custom_components.svitgrid.harvest.transport import _write_solarman

    fake_sm = MagicMock()
    FakeSM = MagicMock(return_value=fake_sm)

    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}
    writes = [(1, 100, 1), (1, 101, 2)]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        _write_solarman(cfg, writes)

    assert fake_sm.write_holding_registers.call_count == 2


def test_write_solarman_calls_disconnect():
    """_write_solarman must call disconnect in finally."""
    from custom_components.svitgrid.harvest.transport import _write_solarman

    fake_sm = MagicMock()
    FakeSM = MagicMock(return_value=fake_sm)

    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        _write_solarman(cfg, [(1, 200, 1)])

    fake_sm.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# _write_modbus — sync helper, happy path + error
# ---------------------------------------------------------------------------

def _make_modbus_write_stubs(is_error: bool = False):
    fake_result = MagicMock()
    fake_result.isError.return_value = is_error

    fake_client = MagicMock()
    fake_client.write_registers.return_value = fake_result

    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    return fake_client, FakeClientClass, fake_client_mod, fake_pymodbus


def test_write_modbus_calls_write_registers():
    """_write_modbus must call write_registers(addr, [val], device_id=uid)."""
    from custom_components.svitgrid.harvest.transport import _write_modbus

    fake_client, FakeClientClass, fake_client_mod, fake_pymodbus = _make_modbus_write_stubs()

    cfg = {"ip": "10.0.0.1", "port": "502"}
    writes = [(1, 300, 0x1234)]

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        _write_modbus(cfg, writes)

    fake_client.write_registers.assert_called_once_with(300, [0x1234], device_id=1)


def test_write_modbus_raises_on_error():
    """_write_modbus must raise RuntimeError when isError() is True."""
    from custom_components.svitgrid.harvest.transport import _write_modbus

    fake_client, FakeClientClass, fake_client_mod, fake_pymodbus = _make_modbus_write_stubs(
        is_error=True
    )

    cfg = {"ip": "10.0.0.1", "port": "502"}
    writes = [(1, 300, 0x1234)]

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}), pytest.raises(RuntimeError):
        _write_modbus(cfg, writes)


def test_write_modbus_calls_close():
    """_write_modbus must call client.close() in finally."""
    from custom_components.svitgrid.harvest.transport import _write_modbus

    fake_client, _, fake_client_mod, fake_pymodbus = _make_modbus_write_stubs()
    cfg = {"ip": "10.0.0.1", "port": "502"}

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        _write_modbus(cfg, [(1, 300, 1)])

    fake_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# write_registers — async dispatch
# ---------------------------------------------------------------------------

async def test_write_registers_dispatches_solarman():
    """write_registers dispatches to _write_solarman for solarman_v5 protocol."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    fake_sm = MagicMock()
    FakeSM = MagicMock(return_value=fake_sm)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        await transport.write_registers(hass, spec, cfg, [(1, 200, 42)])

    fake_sm.write_holding_registers.assert_called_once_with(register_addr=200, values=[42])


async def test_write_registers_dispatches_modbus():
    """write_registers dispatches to _write_modbus for modbus_tcp protocol."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("modbus_tcp")
    cfg = {"ip": "10.0.0.1", "port": "502"}

    fake_client, _, fake_client_mod, fake_pymodbus = _make_modbus_write_stubs()

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        await transport.write_registers(hass, spec, cfg, [(1, 300, 99)])

    fake_client.write_registers.assert_called_once_with(300, [99], device_id=1)


async def test_write_registers_raises_unsupported_protocol():
    """write_registers raises ValueError for unknown protocol."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    # Patch protocol after creation
    spec = RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": "unknown_proto",
        "port": 502, "defaultSlaveId": 1, "flags": {},
        "reads": [], "derivations": [], "writes": [],
    })
    with pytest.raises(ValueError, match="unsupported"):
        await transport.write_registers(hass, spec, {}, [(1, 0, 0)])


# ---------------------------------------------------------------------------
# read_word — async, reuses read client
# ---------------------------------------------------------------------------

async def test_read_word_solarman_returns_value():
    """read_word returns the register value for solarman_v5."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    fake_sm = MagicMock()
    fake_sm.read_holding_registers.return_value = [0xBEEF]
    FakeSM = MagicMock(return_value=fake_sm)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        result = await transport.read_word(hass, spec, cfg, unit_id=1, address=500)

    assert result == 0xBEEF


async def test_read_word_modbus_returns_value():
    """read_word returns the register value for modbus_tcp."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("modbus_tcp")
    cfg = {"ip": "10.0.0.1", "port": "502"}

    fake_result = MagicMock()
    fake_result.isError.return_value = False
    fake_result.registers = [0xCAFE]

    fake_client = MagicMock()
    fake_client.read_holding_registers.return_value = fake_result
    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        result = await transport.read_word(hass, spec, cfg, unit_id=247, address=843)

    assert result == 0xCAFE


async def test_read_word_returns_none_on_exception():
    """read_word returns None when the read raises an exception."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    fake_sm = MagicMock()
    fake_sm.read_holding_registers.side_effect = OSError("connection refused")
    FakeSM = MagicMock(return_value=fake_sm)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        result = await transport.read_word(hass, spec, cfg, unit_id=1, address=500)

    assert result is None


async def test_read_word_modbus_returns_none_on_error():
    """read_word returns None when modbus read isError()."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("modbus_tcp")
    cfg = {"ip": "10.0.0.1", "port": "502"}

    fake_result = MagicMock()
    fake_result.isError.return_value = True

    fake_client = MagicMock()
    fake_client.read_holding_registers.return_value = fake_result
    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        result = await transport.read_word(hass, spec, cfg, unit_id=1, address=100)

    assert result is None
