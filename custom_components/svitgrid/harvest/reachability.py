"""Inverter reachability checker (SP-D).

Checks whether the inverter at the supplied ip/port/slave_id answers a register
read.  Never raises; never writes.  Pairing no longer gates entry creation on
it (issue #6): the cloud has built the station by then, so a failed probe must
not abort the flow.

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

import asyncio
import logging

from . import transport
from .register_spec import RegisterSpec, SpecFlags

_LOGGER = logging.getLogger(__name__)

# Fallback probe address when no spec is supplied.
# Address 1 is the first Modbus holding register and is readable on
# most Deye/Sunsynk inverters.  Callers that know the model should
# supply a spec so a real register is probed instead.
_PROBE_ADDRESS: int = 1

# One failed read does not mean unreachable. A Solarman V5 stick often answers
# the first session's reads with empty frames, and holds its connection slot for
# a moment after a quick connect/close. Every attempt is a fresh session
# (`transport.read_word` opens and closes its own), so the probe tries 3 times
# with a back-off before each retry: about 10 s in total.
_PROBE_ATTEMPTS: int = 3
_PROBE_BACKOFF_S: tuple[float, ...] = (3.0, 7.0)


async def check_inverter_reachable(
    hass,
    harvest_config: dict,
    spec: RegisterSpec | None = None,
) -> bool:
    """Return True if the inverter at harvest_config's ip/port responds.

    Reads one register via the transport layer, retrying on a fresh session up
    to ``_PROBE_ATTEMPTS`` times with a short back-off.  Returns False only when
    every attempt returned None / timed out / raised.  Never raises, never
    writes.

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
    default_port = 8899 if harvest_config.get("protocol", "solarman_v5") == "solarman_v5" else 502
    cfg: dict = {
        "ip": harvest_config["ip"],
        "port": harvest_config.get("port", default_port),
        "logger_serial": harvest_config.get("logger_serial", ""),
        "slave_id": harvest_config.get("slave_id", 1),
    }
    unit_id: int = int(harvest_config.get("slave_id", 1))

    function_code: str = "FC03"
    if spec is not None:
        probe_spec = spec
        address: int = spec.reads[0].address if spec.reads else _PROBE_ADDRESS
        # FC03 (holding) and FC04 (input) are different register banks. An
        # Afore/KSTAR/Solis-5G model reads 100% FC04, so probing its first
        # address as FC03 can reject a perfectly reachable inverter.
        if spec.reads:
            function_code = spec.reads[0].function_code
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

    for attempt in range(_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_PROBE_BACKOFF_S[min(attempt - 1, len(_PROBE_BACKOFF_S) - 1)])
        try:
            result = await transport.read_word(
                hass, probe_spec, cfg, unit_id, address, function_code=function_code
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "reachability check attempt %d/%d raised for %s:%s (address=%s fc=%s): %s",
                attempt + 1,
                _PROBE_ATTEMPTS,
                harvest_config.get("ip"),
                harvest_config.get("port"),
                address,
                function_code,
                exc,
            )
            continue
        if result is not None:
            return True
        _LOGGER.warning(
            "reachability check attempt %d/%d returned no data for %s:%s (address=%s fc=%s)",
            attempt + 1,
            _PROBE_ATTEMPTS,
            harvest_config.get("ip"),
            harvest_config.get("port"),
            address,
            function_code,
        )
    return False
