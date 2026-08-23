#!/usr/bin/env python3
"""Bench tool for an EyBond/SmartESS collector. Read-only unless asked.

Run it on a machine on the SAME LAN as the collector, with Home Assistant's
own harvest STOPPED -- the collector dials exactly one server, and two
listeners on one LAN will fight over it.

    python3 tools/bench_probe.py                      # identity + settings + one frame
    python3 tools/bench_probe.py --sweep 700-710      # probe an undeclared range
    python3 tools/bench_probe.py --write-probe        # the buzzer write probe

Why this is a tool and not a feature: no write has ever been confirmed against
this hardware. Adding a write path to the shipped integration before one has
would be shipping an unproven capability. This proves it first; the feature
comes after.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig  # noqa: E402
from custom_components.svitgrid.eybond_at.identity import identify, resolve_map  # noqa: E402
from custom_components.svitgrid.eybond_at.modbus_rtu import (  # noqa: E402
    ModbusError,
    build_write_single,
    parse_write_response,
    to_signed,
)

# Register 303 is buzzer mode. It is the right probe target for three reasons:
# harmless, instantly reversible, and AUDIBLE -- so the result can be confirmed
# without trusting our own read-back.
BUZZER_REG = 303
BUZZER_SILENT = 0

# The configuration block, decoded. Values verified against the bench unit on
# 2026-08-20/21; see docs/inverter-research in the svitgrid repo.
SETTINGS = [
    (300, "output mode", 1, ""),
    (301, "output priority", 1, ""),
    (302, "input voltage range", 1, ""),
    (303, "buzzer mode", 1, ""),
    (313, "equalisation mode", 1, ""),
    (320, "output voltage", 0.1, "V"),
    (321, "output frequency", 0.01, "Hz"),
    (323, "battery over-voltage", 0.1, "V"),
    (324, "max charge voltage (bulk)", 0.1, "V"),
    (325, "float charge voltage", 0.1, "V"),
    (327, "low-voltage cutoff, on mains", 0.1, "V"),
    (329, "low-voltage cutoff, off-grid", 0.1, "V"),
    (332, "max charge current", 0.1, "A"),
    (333, "max mains charge current", 0.1, "A"),
    (334, "equalisation voltage", 0.1, "V"),
    (335, "equalisation time", 1, "min"),
    (336, "equalisation timeout", 1, "min"),
    (337, "equalisation interval", 1, "d"),
    (341, "SOC: back to utility", 1, "%"),
    (342, "SOC: back to battery", 1, "%"),
    (343, "SOC: low DC cutoff", 1, "%"),
]

# Telemetry needed for the kettle cross-check.
TELEMETRY = [
    (202, "grid voltage", 0.1, "V", False),
    (204, "grid power", 1, "W", True),
    (210, "output voltage", 0.1, "V", False),
    (213, "load power", 1, "W", True),
    (215, "battery voltage", 0.1, "V", False),
    (216, "battery current", 0.1, "A", True),
    (217, "battery power", 1, "W", True),
    (219, "PV voltage", 0.1, "V", False),
    (220, "PV current", 0.1, "A", False),
    (223, "PV power", 1, "W", False),
    (227, "inverter temperature", 1, "°C", False),
    (229, "battery SOC", 1, "%", False),
]


def rule(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


async def wait_for_collector(hub: EybondAtHub, seconds: float):
    print(f"announcing on the LAN, listening on TCP {hub.listen_port} ...")
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        for session in hub.sessions:
            if session.serial:
                return session
        await hub.wait_for_change(1.0)
    for session in hub.sessions:
        return session
    return None


async def read_block(session, address: int, count: int):
    try:
        return await session.read_registers(address, count)
    except Exception as err:  # noqa: BLE001 - a bench tool reports, never raises
        print(f"  ! read {address}+{count} failed: {err}")
        return None


async def show_settings(session) -> None:
    rule("configuration block")
    words = await read_block(session, 300, 44)
    if not words:
        return
    for reg, name, scale, unit in SETTINGS:
        idx = reg - 300
        if idx >= len(words):
            continue
        raw = words[idx]
        value = raw * scale
        shown = f"{value:g}{unit}" if scale != 1 else f"{raw}{unit}"
        print(f"  {reg:>4}  {name:<30} {shown:>12}   (raw {raw})")


async def show_frame(session) -> None:
    """One telemetry frame, plus the cross-check that settles scale AND sign."""
    rule("live frame")
    words = await read_block(session, 201, 29)
    if not words:
        return
    values = {}
    for reg, name, scale, unit, signed in TELEMETRY:
        idx = reg - 201
        if idx >= len(words):
            continue
        raw = to_signed(words[idx]) if signed else words[idx]
        value = raw * scale
        values[reg] = value
        print(f"  {reg:>4}  {name:<26} {value:>10.2f} {unit:<3} (raw {words[idx]})")

    vbat, ibat, pbat = values.get(215), values.get(216), values.get(217)
    rule("cross-check  Vbat x I ~= P")
    if not vbat or not ibat:
        print("  battery is idle (V or I is zero) -- UNDECIDABLE.")
        print("  Put a kettle on the inverter output, or let the AC charger run.")
        print("  A battery that merely sits there proves nothing.")
        return
    product = vbat * ibat
    print(f"  {vbat:.1f} V x {ibat:.1f} A = {product:.0f} W   vs reported {pbat:.0f} W")
    if pbat and abs(product) > 1:
        ratio = abs(pbat / product)
        if 0.8 <= ratio <= 1.25:
            print("  AGREES -> current scale and sign convention both look right.")
        elif 8 <= ratio <= 12.5 or 0.08 <= ratio <= 0.125:
            print(f"  OFF BY ~10x (ratio {ratio:.2f}) -> the current SCALE is wrong.")
            print("  This is the LuxPower/EG4 bug class. Do not ship this map.")
        else:
            print(f"  DISAGREES (ratio {ratio:.2f}) -> wrong address, scale or sign.")
    if pbat and ((pbat > 0) != (ibat > 0)):
        print("  NOTE: reported power and current disagree in SIGN.")
    print(f"  sign: current is {'POSITIVE' if ibat > 0 else 'NEGATIVE'} right now — "
          "record whether the battery is CHARGING or DISCHARGING.")


async def sweep(session, start: int, end: int) -> None:
    rule(f"sweep {start}-{end}")
    words = await read_block(session, start, end - start + 1)
    if not words:
        print("  refused -- the range likely does not exist on this unit.")
        return
    for i, raw in enumerate(words):
        reg = start + i
        note = "" if raw else "   (zero — undecidable)"
        print(f"  {reg:>4}  raw {raw:>6}  0x{raw:04x}  signed {to_signed(raw):>7}{note}")


async def write_probe(session) -> int:
    """Write the buzzer register, verify, restore, verify again.

    Deliberately the ONLY write this tool can perform. If it succeeds, the
    configuration block is very likely writable and the settings feature is a
    UI question. If it is refused, no amount of UI work changes that.
    """
    rule("WRITE PROBE — register 303, buzzer mode")
    before = await read_block(session, BUZZER_REG, 1)
    if not before:
        print("  cannot read the register; aborting without writing.")
        return 1
    original = before[0]
    print(f"  current value: {original}")
    target = BUZZER_SILENT if original != BUZZER_SILENT else 3
    print(f"  writing: {target}   (listen to the inverter)")

    try:
        raw = await session._transact(  # noqa: SLF001 - bench tool, see module docstring
            build_write_single(session.slave_id, BUZZER_REG, target), 5.0
        )
        address, echoed = parse_write_response(raw)
        print(f"  echo: register {address} = {echoed}")
    except ModbusError as err:
        print(f"  REFUSED at the Modbus layer: {err}")
        print("  -> this register is not writable. Option C is not available.")
        return 2
    except Exception as err:  # noqa: BLE001
        print(f"  write failed: {err}")
        return 2

    after = await read_block(session, BUZZER_REG, 1)
    if not after:
        print("  wrote, but could not read back. UNPROVEN either way.")
        return 3
    print(f"  read back: {after[0]}")
    if after[0] != target:
        print("  ECHOED BUT DID NOT STICK -> read-only in practice.")
        print("  This is exactly why the echo is never trusted on its own.")
        restored = True
    else:
        print("  WRITE CONFIRMED. The configuration block is very likely writable.")
        restored = False

    print(f"  restoring {original} ...")
    with contextlib.suppress(Exception):
        await session._transact(  # noqa: SLF001
            build_write_single(session.slave_id, BUZZER_REG, original), 5.0
        )
    final = await read_block(session, BUZZER_REG, 1)
    if final:
        ok = final[0] == original
        print(f"  final value: {final[0]}  {'(restored)' if ok else '!! NOT RESTORED'}")
        if not ok:
            print("  Set buzzer mode back by hand at the inverter panel.")
    return 0 if not restored else 4


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wait", type=float, default=90, help="seconds to wait for the collector")
    ap.add_argument("--advertise", help="the LAN IP to announce (needed inside a container)")
    ap.add_argument("--sweep", help="probe a raw register range, e.g. 700-710")
    ap.add_argument("--write-probe", action="store_true", help="run the buzzer write probe")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    hub = EybondAtHub(
        HubConfig(listen_port=8899, advertised_ip=args.advertise, expected_collectors=0),
        on_diag=(lambda m: print(f"  . {m}")) if args.verbose else None,
    )
    await hub.start()
    try:
        session = await wait_for_collector(hub, args.wait)
        if session is None:
            print("\nNo collector connected.")
            print("  - is Home Assistant's harvest still running? stop it; it takes the socket")
            print("  - same LAN? client isolation on the AP will block the announce")
            print("  - inside a container, pass --advertise <your LAN ip>")
            return 1

        rule("identity")
        identity = await identify(session)
        print(f"  serial          {identity.serial}")
        print(f"  protocol        {identity.protocol_number}")
        print(f"  device type     0x{identity.device_type:04x}")
        print(f"  firmware        {identity.firmware}")
        try:
            resolve_map(identity)
            print("  register map    RECOGNISED")
        except Exception:  # noqa: BLE001
            print("  register map    UNKNOWN protocol — telemetry below is NOT decoded by it")

        if args.sweep:
            start, _, end = args.sweep.partition("-")
            await sweep(session, int(start), int(end or start))
            return 0

        await show_settings(session)
        await show_frame(session)

        if args.write_probe:
            return await write_probe(session)
        print("\n(read-only. add --write-probe to test whether this unit accepts a write)")
        return 0
    finally:
        await hub.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
