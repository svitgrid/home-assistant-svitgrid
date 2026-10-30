#!/usr/bin/env bash
# Copy the monorepo's edge command fixtures into this repo's test fixtures.
# Run after the monorepo regenerates edge_command_payloads.json with
# UPDATE_EDGE_COMMAND_PAYLOADS=1 (edge-command-payloads-fixture.test.ts).
set -euo pipefail
INVERTER_PROTOCOL_DIR="${1:-$HOME/git/svitgrid/packages/inverter_protocol}"
FIXTURES_DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"
mkdir -p "$FIXTURES_DIR"

for SRC_REL in \
  fixtures/edge_command_payloads.json \
  fixtures/priority_profile_resolution.json; do
  SRC="$INVERTER_PROTOCOL_DIR/$SRC_REL"
  DEST="$FIXTURES_DIR/$(basename "$SRC_REL")"
  [ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC" "$DEST"
  echo "vendored $SRC -> $DEST"
done
