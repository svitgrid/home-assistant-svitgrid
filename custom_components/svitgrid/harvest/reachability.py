"""Inverter reachability checker for the HA config-flow (SP-D).

Used by the config-flow to verify it can reach the inverter at the supplied
ip/port/slave_id before completing setup.  Never raises; never writes.

Transport cfg shape passed to transport.read_word:
  {"ip": str, "port": int, "logger_serial": str, "slave_id": int}

  - ``ip``            — copied from harvest_config["ip"]
  - ``port``          — copied from harvest_config["port"]
  - ``logger_serial`` — copied from harvest_config.get("logger_serial", "")
                        (required by pysolarmanv5; empty string for Modbus-only)
  - ``slave_id``      — copied from harvest_config.get("slave_id", 1)
                        (pysolarmanv5 uses this as mb_slave_id at connect time)

unit_id arg to read_word:
  int(harvest_config.get("slave_id", 1))
  (Modbus device_id; ignored by Solarman V5 as it uses mb_slave_id from cfg)

Read address selection:
  - When a RegisterSpec is supplied and has at least one ReadDef:
      spec.reads[0].address  — the first model-specific register, guaranteed
      to return data on a live inverter.
  - Otherwise: _PROBE_ADDRESS (address 1).  Register 1 is the first Modbus
    holding register and is typically populated on Deye/Sunsynk inverters;
    it's a safe no-harm read on most Modbus devices.  Callers that know the
    model should always supply a spec so a real register is probed.
"""
from __future__ import annotations

import logging

from . import transport
from .register_spec import RegisterSpec, SpecFlags

_LOGGER = logging.getLogger(__name__)

# Fallback probe address when no spec is supplied.
# Address 1 is the first Modbus holding register and is readable on
# most Deye/Sunsynk inverters.  Callers that know the model should
# supply a spec so a real register is probed instead.
_PROBE_ADDRESS: int = 1


async def check_inverter_reachable(
    hass,
    harvest_config: dict,
    spec: RegisterSpec | None = None,
) -> bool:
    """Return True if the inverter at harvest_config's ip/port responds.

    Attempts ONE register read via the transport layer.  Returns False on
    None / timeout / any exception.  Never raises, never writes.

    Args:
        hass:           Home Assistant instance (passed through to transport).
        harvest_config: Snake-case config dict with keys: protocol, ip, port,
                        slave_id, model_id, logger_serial.
        spec:           Optional RegisterSpec for the inverter model.  When
                        provided its first ReadDef address is used as the probe
                        register so a real model-specific register is read.
                        When omitted a minimal spec is built from harvest_config
                        and _PROBE_ADDRESS is used.
    """
    default_port = (
        8899
        if harvest_config.get("protocol", "solarman_v5") == "solarman_v5"
        else 502
    )
    cfg: dict = {
        "ip": harvest_config["ip"],
        "port": harvest_config.get("port", default_port),
        "logger_serial": harvest_config.get("logger_serial", ""),
        "slave_id": harvest_config.get("slave_id", 1),
    }
    unit_id: int = int(harvest_config.get("slave_id", 1))

    if spec is not None:
        probe_spec = spec
        address: int = spec.reads[0].address if spec.reads else _PROBE_ADDRESS
    else:
        # Build a minimal RegisterSpec so transport.read_word can determine
        # the protocol (solarman_v5 vs modbus_tcp).  No reads are needed
        # since we supply the address directly.
        probe_spec = RegisterSpec(
            model_id=str(harvest_config.get("model_id", "unknown")),
            version=0,
            protocol=str(harvest_config.get("protocol", "solarman_v5")),
            port=int(harvest_config.get("port", default_port)),
            default_slave_id=unit_id,
            flags=SpecFlags(),
            reads=(),
            derivations=(),
            writes=(),
        )
        address = _PROBE_ADDRESS

    try:
        result = await transport.read_word(hass, probe_spec, cfg, unit_id, address)
        return result is not None
    except Exception:
        _LOGGER.debug(
            "reachability check failed for %s:%s",
            harvest_config.get("ip"),
            harvest_config.get("port"),
        )
        return False
