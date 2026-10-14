"""Test doubles for the wire libraries, specced against the installed classes.

A bare ``MagicMock`` answers any attribute, so a call to a method the library
does not have passes every test and fails only on a real logger. That is how
``_write_solarman`` called ``write_holding_registers`` (which pysolarmanv5 has
never had) from 8b10cf7 on, and no Solarman write ever landed. These doubles
raise ``AttributeError`` for a missing method and ``TypeError`` for a wrong
signature, as the real class does.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, NonCallableMagicMock, create_autospec

import pysolarmanv5
from pymodbus.client import ModbusTcpClient
from pysolarmanv5 import PySolarmanV5


def solarman_instance() -> NonCallableMagicMock:
    """A ``PySolarmanV5`` instance double with the real class's methods only."""
    return create_autospec(PySolarmanV5, instance=True)


def modbus_instance() -> NonCallableMagicMock:
    """A ``ModbusTcpClient`` instance double with the real class's methods only."""
    return create_autospec(ModbusTcpClient, instance=True)


def solarman_module(instance=None, ctor_side_effect=None):
    """A stand-in ``pysolarmanv5`` module whose constructor returns *instance*.

    Returns ``(module, constructor)``. The module carries the real
    ``NoSocketAvailableError`` so the transport's error handling is exercised
    with the library's own exception type.
    """
    if ctor_side_effect is not None:
        ctor = MagicMock(side_effect=ctor_side_effect)
    else:
        ctor = MagicMock(return_value=instance if instance is not None else solarman_instance())
    mod = types.ModuleType("pysolarmanv5")
    mod.PySolarmanV5 = ctor  # type: ignore[attr-defined]
    mod.NoSocketAvailableError = pysolarmanv5.NoSocketAvailableError  # type: ignore[attr-defined]
    return mod, ctor
