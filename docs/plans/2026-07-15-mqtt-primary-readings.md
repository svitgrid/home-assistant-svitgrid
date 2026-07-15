# HA add-on — MQTT-primary readings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. This repo is the SEPARATE `home-assistant-svitgrid` (custom component `custom_components/svitgrid/`), NOT the svitgrid monorepo.

**Goal:** Make the HA add-on publish inverter readings **MQTT-primary** (QoS-1 with per-reading PUBACK confirmation), falling back to the existing HTTP batch ingest **only** for readings the broker did not acknowledge, and take its control (the `mqttPublishReadings` enable-flag + reporting cadence) over MQTT via `devices/{deviceId}/config` — mirroring the ESP32 edge firmware (W4b) and its server config-push (W4a, which already covers HA because HA authenticates as an edge-device).

**Architecture:** Invert today's HTTP-primary/MQTT-mirror in `reading_sender.drain_once`. Add PUBACK observability to the readings MQTT client (`mqtt_readings_publisher.py`). Add a `devices/{deviceId}/config` subscription (on the EXISTING wake client, `mqtt_wake.py`, one connection like the edge) that updates a shared control object. Keep HTTP as bootstrap (first drain each session) + fallback.

**Tech Stack:** Python 3.11, paho-mqtt (already a dep), Home Assistant custom component, pytest + `pytest_homeassistant_custom_component` (paho stubbed via `sys.modules` in tests).

## Global Constraints

- **PUBACK is the fallback trigger.** A reading is skipped-from-HTTP ONLY when the broker PUBACKs it (QoS-1). paho `on_publish(client, userdata, mid)` fires on PUBACK for QoS-1; correlate by the `mid` returned from `client.publish(...)`. Un-acked-within-timeout → that reading goes over HTTP. This mirrors the edge (`MQTT_PUBACK_TIMEOUT_MS = 5000`; use **5.0 s** here too).
- **Never lose a reading.** Any failure/timeout/disconnect/not-bootstrapped/flag-off path → the existing HTTP `push_readings_batch`. The SQLite store row stays `pending` until it is confirmed sent by EITHER path. Duplicate delivery (a reading that PUBACKs late AND also went HTTP) is acceptable and harmless.
- **First drain each session = HTTP (bootstrap).** Before the add-on has learned its control over MQTT, the first drain goes HTTP so it (a) learns `mqttPublishReadings` + `ingestIntervalMs` from the response and (b) proves the cloud path. Only after a successful HTTP bootstrap this session may MQTT-primary skip HTTP. (Mirror the edge's `g_http_ingest_confirmed_this_boot`.)
- **Threading.** paho runs its network loop in its own thread (`loop_start`); `on_publish` fires in THAT thread. Bridge to the asyncio loop safely (`loop.call_soon_threadsafe`, or a thread-safe `Event`/`Future` registry). Never block the HA event loop waiting on a PUBACK — await, don't sleep.
- **Island-safe.** All this lives in the cloud sender / readings-publish path, which never runs for island-mode entries (`cloud_ingest_enabled` gate). Do not touch island paths.
- **Fail-open.** A broker/token/parse error must never raise into the drain loop or crash the integration — degrade to HTTP.
- **TDD.** paho is stubbed in tests (`_install_paho_stub` pattern in `tests/test_mqtt_wake.py`). Write failing tests first. Run `pytest -v tests/<file>` per task; `ruff check .` clean.
- **Version bump** `custom_components/svitgrid/manifest.json` 0.14.0 → **0.15.0**; add a CHANGELOG entry. HACS release is a separate user-gated step (tag push).

---

## File Structure

- `custom_components/svitgrid/mqtt_readings_publisher.py` — add PUBACK tracking + `publish_and_wait` (Task 1).
- `custom_components/svitgrid/mqtt_control.py` (NEW) — a small shared `MqttControlState` (mqtt_primary flag + interval_s + bootstrapped) + `apply_config(state, payload)` (Task 2).
- `custom_components/svitgrid/mqtt_wake.py` — also subscribe `devices/{deviceId}/config` on connect; route config messages → `apply_config` (Task 2).
- `custom_components/svitgrid/reading_sender.py` — invert `drain_once` to MQTT-primary + HTTP bootstrap/fallback, reading the shared control state (Task 3).
- `custom_components/svitgrid/__init__.py` / wiring — construct + share `MqttControlState`, pass it to the wake client and the drain loop (Task 3).
- `custom_components/svitgrid/manifest.json` + `CHANGELOG.md` (Task 4).

---

### Task 1: PUBACK observability in the readings MQTT client

**Files:**
- Modify: `custom_components/svitgrid/mqtt_readings_publisher.py`
- Test: `tests/test_mqtt_readings_publisher.py`

**Interfaces:**
- Produces: `async def publish_and_wait(self, payload: str, timeout: float = 5.0) -> bool` — publishes QoS-1, returns True iff the broker PUBACKs within `timeout`; False on not-connected / publish-error / timeout. Keep `ensure_connected()`. The existing fire-and-forget `publish()` may be removed (no other caller after Task 3) OR retained — implementer's call, but note it in the report.

- [ ] **Step 1: Write failing tests.** With the paho stub: (a) `publish_and_wait` calls `client.publish(topic, payload, qos=1)`, captures the returned `mid`, and resolves True when the stub invokes the registered `on_publish(client, userdata, mid)` with that mid; (b) resolves False if `on_publish` never fires within a short timeout; (c) returns False immediately if not connected; (d) returns False if `publish()` returns non-zero `rc`. Drive `on_publish` from a different thread in the test to exercise the thread→asyncio bridge (or assert the bridge mechanism directly).

- [ ] **Step 2: Run, watch fail** (`pytest -v tests/test_mqtt_readings_publisher.py`).

- [ ] **Step 3: Implement.** Register `self._client.on_publish = self._on_publish` in the connect path. Maintain a `dict[int, asyncio.Future]` (or `Event`) keyed by mid, guarded for thread access. `publish_and_wait`: capture `self._loop = asyncio.get_running_loop()` (store the loop on connect); `info = client.publish(topic, payload, qos=1)`; if `info.rc != 0` return False; create a future, register under `info.mid`; `await asyncio.wait_for(future, timeout)`; on timeout/exception return False; always clean up the registry entry. `_on_publish(client, userdata, mid)` (paho thread): `self._loop.call_soon_threadsafe(self._resolve, mid)`; `_resolve` sets the future result True if present. Handle `mid` reuse/absent defensively.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit.** `feat(mqtt): PUBACK-confirmed publish_and_wait in readings client`

---

### Task 2: Config subscription + shared control state

**Files:**
- Create: `custom_components/svitgrid/mqtt_control.py`
- Modify: `custom_components/svitgrid/mqtt_wake.py`
- Test: `tests/test_mqtt_control.py` (new), extend `tests/test_mqtt_wake.py`

**Interfaces:**
- Produces:
  - `class MqttControlState` with mutable fields: `mqtt_primary: bool = False`, `interval_s: int | None = None`, `bootstrapped: bool = False`. (Plain object shared by reference.)
  - `def apply_config(state: MqttControlState, payload: str | bytes) -> None` — parse JSON defensively (malformed → no-op, never raise); set `state.mqtt_primary = bool(payload["mqttPublishReadings"])` when present; set `state.interval_s = int(payload["ingestIntervalMs"]/1000)` when a positive number. Missing fields leave current values (do NOT reset). Mirror the edge's `apply_control_config` field semantics EXCEPT: the edge resets mqttPublishReadings when absent; here **leave current** (HA's config-push from `buildDeviceConfig`/`buildHarvesterConfig` always includes it, but be lenient).

- [ ] **Step 1: Write failing tests.** `apply_config`: valid `{"mqttPublishReadings":true,"ingestIntervalMs":30000}` → `mqtt_primary True`, `interval_s 30`; malformed/non-JSON/non-dict → no change, no raise; partial `{}` → unchanged. For `mqtt_wake`: on connect the client ALSO subscribes `devices/{deviceId}/config` (QoS 1) in addition to the wake topic; a message on the config topic routes to `apply_config` and updates the shared state; a message on the wake topic still triggers the wake event (unchanged).

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement.** `mqtt_control.py` as above. In `mqtt_wake.py`: derive the config topic (`devices/{deviceId}/config`) the same way the wake topic is derived from the token response; subscribe it on connect (additive, QoS 1); in the on_message handler, if the topic is the config topic call `apply_config(self._control, payload)` and return (do NOT also fire the wake event); otherwise the existing wake handling. Accept the shared `MqttControlState` via the wake client's constructor/start. Mirror the existing subscribe + reconnect + 12h remint discipline.

- [ ] **Step 4: Run tests, verify pass.**

- [ ] **Step 5: Commit.** `feat(mqtt): subscribe devices/{id}/config + shared MqttControlState`

---

### Task 3: Invert drain_once to MQTT-primary + HTTP bootstrap/fallback

**Files:**
- Modify: `custom_components/svitgrid/reading_sender.py` (`drain_once`)
- Modify: `custom_components/svitgrid/__init__.py` (construct + share `MqttControlState`; pass it to the wake client and to `drain_once`; give the drain loop access to `publish_and_wait`)
- Test: `tests/test_reading_sender.py`

**Interfaces:**
- Consumes: `MqttControlState` (Task 2), `publisher.publish_and_wait` (Task 1).

- [ ] **Step 1: Write failing tests.** Extend `tests/test_reading_sender.py`:
  - **Bootstrap:** with `control.bootstrapped = False`, drain does the HTTP batch even if `control.mqtt_primary` is True and the publisher is connected; on HTTP success `control.bootstrapped` becomes True.
  - **MQTT-primary happy path:** `control.mqtt_primary=True`, `bootstrapped=True`, publisher connected, `publish_and_wait` returns True for every row → NO HTTP call (`push_readings_batch` not called), all rows `mark_sent`.
  - **Partial PUBACK:** some rows `publish_and_wait` True, others False → the FALSE rows go through `push_readings_batch` (HTTP) and are marked per its result; the True rows are `mark_sent` without HTTP.
  - **Flag off / not connected:** HTTP path exactly as today.
  - Keep all existing `drain_once` tests green (adjust only where the new control param is required).

- [ ] **Step 2: Run, watch fail** (`pytest -v tests/test_reading_sender.py`).

- [ ] **Step 3: Implement.** Add a `control: MqttControlState` param to `drain_once`. New flow after `rows`/`readings` are built:
  ```
  if control.mqtt_primary and control.bootstrapped and publisher is not None and await publisher.ensure_connected():
      acked_keys, unacked = [], []   # partition rows by per-reading publish_and_wait
      for key, reading in zip(keys, readings):
          if await publisher.publish_and_wait(json.dumps(reading)):
              acked_keys.append(key)
          else:
              unacked.append((key, reading))
      if acked_keys: await _maybe(store.mark_sent(acked_keys))
      if unacked:
          # HTTP-fallback ONLY the un-acked readings (reuse the existing
          # push_readings_batch + result-mapping block, scoped to `unacked`)
          <existing HTTP path over the unacked subset, incl. DeviceEvicted/
           ReadingRejected/5xx/stopped handling and cadence update from response>
      return len(acked_keys) + <http sent count>
  else:
      <existing HTTP path over ALL rows, unchanged>
      # on success set control.bootstrapped = True
      # cadence + flag still updated from the response (bootstrap/fallback source)
  ```
  Refactor the existing HTTP block into a helper (e.g. `_http_send(rows_subset)`) so both the fallback and the full-HTTP path reuse it (DRY — do NOT copy-paste the DeviceEvicted/ReadingRejected/results-mapping logic). Preserve every existing behavior (island-safety, battery-sign flip, stopped/evicted/lifecycle, cadence-from-response). Wire the shared `MqttControlState` in `__init__.py` so the SAME instance is updated by the wake client (Task 2) and read here.

- [ ] **Step 4: Run tests, verify pass.** Full `pytest -v tests/test_reading_sender.py` + `pytest -q` (report counts; note any pre-existing unrelated failures — the multi-file `pytest_homeassistant_custom_component` teardown hang is a known unrelated issue per repo docs).

- [ ] **Step 5: Commit.** `feat(mqtt): MQTT-primary drain with per-reading PUBACK + HTTP bootstrap/fallback`

---

### Task 4: Version bump + changelog

**Files:**
- Modify: `custom_components/svitgrid/manifest.json` (0.14.0 → 0.15.0), `CHANGELOG.md`

- [ ] **Step 1: Bump `manifest.json` version to `0.15.0`.**
- [ ] **Step 2: Add a CHANGELOG entry** describing MQTT-primary readings (PUBACK-confirmed, HTTP fallback on un-ack, control over `devices/{id}/config`, first-drain-HTTP bootstrap), off by default (gated by the server `mqttPublishReadings` allowlist).
- [ ] **Step 3: Run full suite + ruff.** `pytest -q` and `ruff check .` — report results.
- [ ] **Step 4: Commit.** `chore(release): 0.15.0 — MQTT-primary readings (PUBACK + config-over-MQTT)`

---

## Self-Review Checklist (controller runs before final review)

- No reading can be lost: every non-PUBACK'd row goes HTTP; store rows stay pending until sent by either path.
- First drain each session is HTTP; MQTT-primary only after `bootstrapped`.
- Threading: `on_publish` (paho thread) → asyncio future resolution is race-free and cleans up its registry; no HA-event-loop blocking.
- Config subscription is additive to wake (one connection), routes correctly, never eats a wake.
- Island paths untouched; fail-open everywhere; ruff clean; version bumped.
