#!/usr/bin/env bash
# Copy the monorepo's edge command fixtures into this repo's test fixtures.
# Run after the monorepo regenerates edge_command_payloads.json with
# UPDATE_EDGE_COMMAND_PAYLOADS=1 (edge-command-payloads-fixture.test.ts), or
# firmware_signed_commands.json and edge_paths.json with UPDATE_EDGE_CONTRACT=1
# (signing-command-parity.test.ts). edge_pair_code.json and
# island_capability_vocab.json are hand-written there.
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
  fixtures/island_capability_vocab.json; do
  SRC="$INVERTER_PROTOCOL_DIR/$SRC_REL"
  DEST="$FIXTURES_DIR/$(basename "$SRC_REL")"
  [ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC" "$DEST"
  echo "vendored $SRC -> $DEST"
done
