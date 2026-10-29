#!/usr/bin/env bash
# Copy the monorepo's island settings fixtures into this repo's test fixtures.
# Run after the monorepo regenerates them with UPDATE_ISLAND_SETTINGS_FIXTURE=1
# (settings_register_mapping_test.dart and island-settings-contract-fixture.test.ts).
set -euo pipefail
INVERTER_PROTOCOL_DIR="${1:-$HOME/git/svitgrid/packages/inverter_protocol}"
FIXTURES_DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"
mkdir -p "$FIXTURES_DIR"

for SRC_REL in \
  test/fixtures/island_settings_snapshots.json \
  fixtures/settings_snapshot_kinds.json \
  fixtures/solis_settings_addresses.json; do
  SRC="$INVERTER_PROTOCOL_DIR/$SRC_REL"
  DEST="$FIXTURES_DIR/$(basename "$SRC_REL")"
  [ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC" "$DEST"
  echo "vendored $SRC -> $DEST"
done
