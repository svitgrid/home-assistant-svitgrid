#!/usr/bin/env bash
# Copy the monorepo's generated golden vectors into this repo's test fixtures.
# Run after the monorepo's export_golden_vectors.dart regenerates them.
set -euo pipefail
SRC="${1:-$HOME/git/svitgrid/packages/inverter_protocol/golden-vectors.json}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/golden-vectors.json"
[ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
mkdir -p "$(dirname "$DEST")"
cp "$SRC" "$DEST"
echo "vendored $SRC -> $DEST"
