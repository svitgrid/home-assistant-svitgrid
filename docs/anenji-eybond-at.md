# Anenji / SmartESS collector support (`eybond_at`)

Reads an Anenji inverter through the EyBond/SmartESS Wi-Fi collector it ships
with, over the local network. No cloud account, no dongle swap, no router
changes.

**Status as of 2026-08-20:** the protocol stack is complete and tested against
captured frames. **It has never run against the real collector.** Battery, PV,
and load registers are identified but unexercised, because the bench unit has
none of that hardware attached.

## How it works

The collector is a TCP **client**. It dials its vendor cloud and never listens,
so nothing can poll it. It does implement one unauthenticated UDP command,
`set>server=<ip>:<port>;` on port **58899**, which re-points it at a server of
our choosing. We become that server on TCP **8899**.

```
add-on  --UDP 58899-->  collector      "dial me instead"
collector --TCP 8899->  add-on         one session, the collector's only one
add-on  --optional-->   vendor cloud   relay, so SmartESS keeps working
```

Measured: the collector dialled in **0.6 seconds** after the first announce.

## The protocol, as measured

Two protocols share the one socket:

| | |
| --- | --- |
| Control | AT-text lines. `AT+CMD?` query, `AT+CMD=value` write, `AT+CMD:value` reply |
| Data | **Bare Modbus RTU.** No wrapper, no length prefix, no transaction id |
| Framing | AT ends at CRLF; Modbus length is derived from the function code and byte count |
| Concurrency | Strictly serialized. 84/84 Modbus and 67/67 AT pairs over 89 polling cycles |

**There is no transaction id.** A response is matched to its request by order
and by nothing else. That single fact shapes the whole design: our own reads
are interleaved *between* the vendor cloud's, never concurrent with them, and a
frame that cannot be attributed ends the connection rather than being guessed
at.

## Choosing the register map

**Never from the brand or the model name.** Anenji ships at least two
platforms: the 12 kW split-phase is SRNE ASF-HF, and the SmartESS family clones
EASUN/ISolar SMG II. Inside SMG II the map is versioned again, by a protocol
number the device reports at register **184**.

Our bench unit reports **11**. The most complete published SMG II map documents
protocols 3 to 6 and calls register 202 "Total Grid Current" — which would make
an idle inverter draw 238 A. On protocol 11 it is AC voltage and reads 228.4 V.

So the map is dispatched from the device, and an unrecognised protocol number
**publishes nothing**. Decoding on a guess produces plausible numbers, which is
the failure nobody notices.

| Register | Meaning |
| --- | --- |
| 171 | Device type, packed BCD. `0x7803` matches the firmware prefix |
| 184 | **Protocol number** — selects the register map |
| 186 (12 regs) | Serial number, ASCII |
| 626 (8 regs) | Firmware string, ASCII |

## Configuration

Set `harvest_config` on the inverter:

```json
{
  "protocol": "eybond_at",
  "listen_port": 8899,
  "slave_id": 1
}
```

| Key | Default | Notes |
| --- | --- | --- |
| `protocol` | — | Must be `eybond_at` |
| `listen_port` | 8899 | Our TCP listener. 0 lets the OS choose |
| `announce_target` | `255.255.255.255` | Set a unicast address once the collector IP is known |
| `announce_port` | 58899 | The collector's fixed command port |
| `slave_id` | 1 | Modbus slave of the inverter behind the collector |
| `cloud_proxy_host` | none | Vendor cloud to relay to. See below |
| `cloud_proxy_port` | none | Required whenever the host is set |

### The vendor relay is off by default

Relaying keeps the customer's SmartESS app working, and we want that. But the
vendor host belongs to the collector, not to us — the bench unit answers
`AT+CLDSRVHOST1` with `dtu_ess.eybond.com,18899,TCP`, while others in this
family use `m2m.eybond.com` or `iot.eybond.com`. Hardcoding one would relay
some customers' traffic to a cloud that is not theirs.

`discover_upstream(link)` reads the endpoint from the device, for callers that
want to enable the relay automatically.

**With the relay off, the SmartESS app stops receiving data** while the add-on
holds the collector.

## The contract that matters most

**A dead, refusing, or throttled vendor cloud must never tear down the
collector session.**

This was learned by breaking it. On 2026-08-20 a hand-rolled proxy coupled the
two: an ordinary cloud disconnect closed the collector socket, the collector
redialled, the proxy opened a fresh cloud connection, the cloud closed it again
— **19 collector reconnects in 43 seconds**. With the two decoupled, one
collector session held for a **full hour** across roughly fifty cloud
disconnects.

`test_a_dead_upstream_never_tears_down_the_collector_session` and
`test_an_upstream_that_connects_then_hangs_up_leaves_the_collector_alone` both
exist for that. The second one matters more: the real cloud *accepted*, ran a
handshake, and then closed, and the first test alone did not catch a teardown
on close.

## Modules

| File | Role |
| --- | --- |
| `at_codec.py` | AT-text line protocol |
| `modbus_rtu.py` | CRC16, read requests, response parsing |
| `demux.py` | Frame boundaries on the shared socket |
| `scheduler.py` | Whose turn it is on the serialized line |
| `link.py` | Sockets: announcer, listener, isolated vendor relay |
| `identity.py` | Read the identity block, dispatch the map |
| `register_map.py` | The protocol-11 map, with per-field confidence |
| `reader.py` | One poll cycle |
| `harvest.py` | The loop, feeding the existing reading pipeline |
| `setup.py` | Config translation and startup |

Everything except `link.py` is pure: no sockets, no clock, no Home Assistant
imports.

## Confidence, and what is still unproven

`register_map.py` marks every field. `CONFIRMED` means verified against a live
value on hardware; `IDENTIFIED` means the address comes from a map that matched
our hardware everywhere it could be checked, but which we have not exercised.

**Confirmed:** grid voltage, grid frequency, output voltage, inverter
temperature.

**Identified, not confirmed:** battery voltage, current, power, SOC; PV
voltage, current, power; load power; grid power. All of these read zero on the
bench unit because it runs from a wall socket with no battery, no panels, and
no load. **Zero is undecidable, not evidence.**

Also unresolved: whether daily energy counters exist. The protocol documents
daily PV generation at register **702**, and our sweep read it as zero with no
panels attached.

See `docs/inverter-registers-deye-vs-anenji.md` and
`docs/inverter-research/2026-08-20-anenji-smg-ii-register-families.md` in the
main svitgrid repo for the measurements behind all of this.
