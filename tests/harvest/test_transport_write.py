"""Tests for transport.py write path: write_registers + read_word (SP-C Task 8).

TDD — tests written first.  Implementation to follow in transport.py.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from tests.harvest.lib_doubles import modbus_instance, solarman_instance, solarman_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(protocol="solarman_v5"):
    return RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": protocol,
            "port": 8899 if protocol == "solarman_v5" else 502,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [],
            "derivations": [],
            "writes": [],
        }
    )


class _FakeHass:
    """Minimal hass stub that runs executor jobs synchronously."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


# ---------------------------------------------------------------------------
# _write_solarman — sync helper, happy path
# ---------------------------------------------------------------------------


_SM_CFG = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}


def test_write_solarman_writes_each_register_with_fc16():
    """One FC16 (write multiple registers) frame per register, as the edge
    firmware (solarman_v5_build_write_frame) and the phone harvester
    (BatteryChargeExecutor) write a Deye: Deye loggers refuse FC06."""
    from custom_components.svitgrid.harvest.transport import _write_solarman

    sm = solarman_instance()
    mod, _ = solarman_module(sm)

    with patch.dict(sys.modules, {"pysolarmanv5": mod}):
        _write_solarman(_SM_CFG, [(1, 200, 0xABCD), (1, 166, 100)])

    calls = [c.kwargs for c in sm.write_multiple_holding_registers.call_args_list]
    assert calls == [
        {"register_addr": 200, "values": [0xABCD]},
        {"register_addr": 166, "values": [100]},
    ]
    sm.write_holding_register.assert_not_called()  # FC06


def test_write_solarman_reconnects_when_the_logger_drops_the_connection():
    """A logger that closes the socket after every reply (the mock logger,
    and the reason the phone harvester opens one connection per request) must
    still get every register, not only the first."""
    import pysolarmanv5

    from custom_components.svitgrid.harvest.transport import _write_solarman

    first, second = solarman_instance(), solarman_instance()
    first.write_multiple_holding_registers.side_effect = [
        None,
        pysolarmanv5.NoSocketAvailableError("Connection already closed."),
    ]
    mod, ctor = solarman_module()
    ctor.side_effect = [first, second]

    with patch.dict(sys.modules, {"pysolarmanv5": mod}):
        _write_solarman(_SM_CFG, [(1, 148, 2351), (1, 149, 51), (1, 166, 100)])

    landed = [
        c.kwargs["register_addr"]
        for sm in (first, second)
        for c in sm.write_multiple_holding_registers.call_args_list
    ]
    assert landed == [148, 149, 149, 166]
    assert ctor.call_count == 2
    # The retry is the reconnect; the library's own would re-send frames.
    assert all(c.kwargs["auto_reconnect"] is False for c in ctor.call_args_list)
    first.disconnect.assert_called()
    second.disconnect.assert_called_once()


def test_write_solarman_raises_when_the_retry_fails_too():
    from custom_components.svitgrid.harvest.transport import _write_solarman

    sm = solarman_instance()
    sm.write_multiple_holding_registers.side_effect = OSError("refused")
    mod, _ = solarman_module(sm)

    with patch.dict(sys.modules, {"pysolarmanv5": mod}), pytest.raises(OSError):
        _write_solarman(_SM_CFG, [(1, 148, 1)])


def test_write_solarman_calls_disconnect():
    """_write_solarman must call disconnect in finally."""
    from custom_components.svitgrid.harvest.transport import _write_solarman

    sm = solarman_instance()
    mod, _ = solarman_module(sm)

    with patch.dict(sys.modules, {"pysolarmanv5": mod}):
        _write_solarman(_SM_CFG, [(1, 200, 1)])

    sm.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# _write_modbus — sync helper, happy path + error
# ---------------------------------------------------------------------------


def _make_modbus_write_stubs(is_error: bool = False):
    fake_result = MagicMock()
    fake_result.isError.return_value = is_error

    fake_client = modbus_instance()
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

    with (
        patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}),
        pytest.raises(RuntimeError),
    ):
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

    fake_sm = solarman_instance()
    FakeSM = MagicMock(return_value=fake_sm)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        await transport.write_registers(hass, spec, cfg, [(1, 200, 42)])

    fake_sm.write_multiple_holding_registers.assert_called_once_with(register_addr=200, values=[42])


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
    spec = RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "unknown_proto",
            "port": 502,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [],
            "derivations": [],
            "writes": [],
        }
    )
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

    fake_sm = solarman_instance()
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

    fake_client = modbus_instance()
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

    fake_sm = solarman_instance()
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

    fake_client = modbus_instance()
    fake_client.read_holding_registers.return_value = fake_result
    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {"pymodbus": fake_pymodbus, "pymodbus.client": fake_client_mod}):
        result = await transport.read_word(hass, spec, cfg, unit_id=1, address=100)

    assert result is None


# ---------------------------------------------------------------------------
# read_word — function_code passthrough (FC04 models)
# ---------------------------------------------------------------------------


async def test_read_word_defaults_to_fc03_for_the_write_path():
    """Pre-write / verify reads target HOLDING registers — the default stays FC03."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "1.2.3.4", "logger_serial": "1", "port": 8899, "slave_id": 1}
    captured: dict = {}

    def _fake(cfg_arg, ranges):
        captured["ranges"] = ranges
        return {1: {500: 7}}

    with patch.object(transport, "_read_solarman", _fake):
        result = await transport.read_word(hass, spec, cfg, unit_id=1, address=500)

    assert result == 7
    assert captured["ranges"] == [(1, 500, 1, "FC03")]


async def test_read_word_honours_an_explicit_fc04():
    """check_inverter_reachable must probe the bank the model actually uses."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "1.2.3.4", "logger_serial": "1", "port": 8899, "slave_id": 1}
    captured: dict = {}

    def _fake(cfg_arg, ranges):
        captured["ranges"] = ranges
        return {1: {40: 9}}

    with patch.object(transport, "_read_solarman", _fake):
        result = await transport.read_word(
            hass, spec, cfg, unit_id=1, address=40, function_code="FC04"
        )

    assert result == 9
    assert captured["ranges"] == [(1, 40, 1, "FC04")]


# ---------------------------------------------------------------------------
# read_words — one connection, contiguous ranges
# ---------------------------------------------------------------------------


async def test_read_words_reads_contiguous_addresses_as_one_range():
    """A slot table (148..177 on a Deye) is one FC03 frame, not 30 connections."""
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    fake_sm = solarman_instance()
    fake_sm.read_holding_registers.side_effect = lambda register_addr, quantity: [
        register_addr + i for i in range(quantity)
    ]
    FakeSM = MagicMock(return_value=fake_sm)
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        got = await transport.read_words(hass, spec, cfg, 1, [150, 148, 149, 300])

    assert got == {148: 148, 149: 149, 150: 150, 300: 300}
    assert FakeSM.call_count == 1
    calls = [c.kwargs for c in fake_sm.read_holding_registers.call_args_list]
    assert calls == [
        {"register_addr": 148, "quantity": 3},
        {"register_addr": 300, "quantity": 1},
    ]


async def test_read_words_returns_empty_on_exception():
    from custom_components.svitgrid.harvest import transport

    hass = _FakeHass()
    spec = _spec("solarman_v5")
    cfg = {"ip": "192.168.1.1", "logger_serial": "12345", "port": "8899", "slave_id": "1"}

    FakeSM = MagicMock(side_effect=OSError("connection refused"))
    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeSM  # type: ignore[attr-defined]

    with (
        patch.dict(sys.modules, {"pysolarmanv5": fake_mod}),
        patch.object(transport, "_SOLARMAN_CONNECT_BACKOFF", 0),
    ):
        got = await transport.read_words(hass, spec, cfg, 1, [148])

    assert got == {}
