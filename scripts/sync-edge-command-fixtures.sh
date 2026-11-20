#!/usr/bin/env bash
# Copy the monorepo's edge command fixtures into this repo's test fixtures.
# Run after the monorepo regenerates edge_command_payloads.json with
# UPDATE_EDGE_COMMAND_PAYLOADS=1 (edge-command-payloads-fixture.test.ts), or
# firmware_signed_commands.json and edge_paths.json with UPDATE_EDGE_CONTRACT=1
# (signing-command-parity.test.ts). edge_pair_code.json and
# island_capability_vocab.json are hand-written there, and
# zigbee_condition_cases.json, bms_sync_version_cases.json and
# maintenance_due_cases.json are pinned against the cloud there
# (zigbee-conditions.test.ts, bms-roster-model.test.ts, maintenance-due.test.ts),
# and so is envelope_enforcement_cases.json (envelope-replay.test.ts).
# authorized-commands-by-mode.json and envelope-check-vectors.json hold what an
# edge box authorizes for an island event's mode (svitgrid D-2288), which
# test_envelope_check.py holds the island scheduler to. zigbee_device_profiles.json
# (prod's `config/zigbeeDeviceProfiles`) is read at runtime, so it goes into the
# integration itself, as does deye-settings.json, the Deye settings catalogue
# harvest/deye_settings.py plans set_inverter_setting with (svitgrid D-2346).
set -euo pipefail
INVERTER_PROTOCOL_DIR="${1:-$HOME/git/svitgrid/packages/inverter_protocol}"
FIXTURES_DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"
mkdir -p "$FIXTURES_DIR"

for SRC_REL in \
  fixtures/edge_command_payloads.json \
  fixtures/priority_profile_resolution.json \
  fixtures/firmware_signed_commands.json \
  fixtures/edge_paths.json \
  fixtures/edge_pair_code.json \
  fixtures/island_capability_vocab.json \
  fixtures/zigbee_condition_cases.json \
  fixtures/bms_sync_version_cases.json \
  fixtures/maintenance_due_cases.json \
  fixtures/envelope_enforcement_cases.json \
  test/fixtures/authorized-commands-by-mode.json \
  test/fixtures/envelope-check-vectors.json; do
  SRC="$INVERTER_PROTOCOL_DIR/$SRC_REL"
  DEST="$FIXTURES_DIR/$(basename "$SRC_REL")"
  [ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC" "$DEST"
  echo "vendored $SRC -> $DEST"
done

INTEGRATION_DIR="$(cd "$(dirname "$0")/.." && pwd)/custom_components/svitgrid"
SRC="$INVERTER_PROTOCOL_DIR/fixtures/zigbee_device_profiles.json"
[ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
cp "$SRC" "$INTEGRATION_DIR/zigbee_device_profiles.json"
echo "vendored $SRC -> $INTEGRATION_DIR/zigbee_device_profiles.json"

SRC="$INVERTER_PROTOCOL_DIR/deye-settings.json"
[ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
cp "$SRC" "$INTEGRATION_DIR/harvest/deye_settings.json"
echo "vendored $SRC -> $INTEGRATION_DIR/harvest/deye_settings.json"
