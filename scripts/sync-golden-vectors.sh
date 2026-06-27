#!/usr/bin/env bash
# Copy the monorepo's generated golden vectors into this repo's test fixtures.
# Run after the monorepo's export_golden_vectors.dart regenerates them.
set -euo pipefail
INVERTER_PROTOCOL_DIR="${1:-$HOME/git/svitgrid/packages/inverter_protocol}"
FIXTURES_DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"

# --- read golden vectors ---
SRC_READ="$INVERTER_PROTOCOL_DIR/golden-vectors.json"
DEST_READ="$FIXTURES_DIR/golden-vectors.json"
[ -f "$SRC_READ" ] || { echo "source not found: $SRC_READ" >&2; exit 1; }
mkdir -p "$FIXTURES_DIR"
cp "$SRC_READ" "$DEST_READ"
echo "vendored $SRC_READ -> $DEST_READ"

# --- write golden vectors ---
SRC_WRITE="$INVERTER_PROTOCOL_DIR/write-golden-vectors.json"
DEST_WRITE="$FIXTURES_DIR/write-golden-vectors.json"
[ -f "$SRC_WRITE" ] || { echo "source not found: $SRC_WRITE" >&2; exit 1; }
cp "$SRC_WRITE" "$DEST_WRITE"
echo "vendored $SRC_WRITE -> $DEST_WRITE"
