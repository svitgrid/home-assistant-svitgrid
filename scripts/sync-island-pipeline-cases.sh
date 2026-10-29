#!/usr/bin/env bash
# Copy the monorepo's generated island pipeline fixture, and the
# reading-validator.ts its model tables are pinned against, into this repo's
# test fixtures. Run after the monorepo's
# island-pipeline-contract-fixture.test.ts regenerates the fixture.
set -euo pipefail
READING_CORE_DIR="${1:-$HOME/git/svitgrid/packages/reading-core}"
FIXTURES_DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures"

# --- pipeline cases ---
SRC_CASES="$READING_CORE_DIR/test/fixtures/island_pipeline_cases.jsonl"
DEST_CASES="$FIXTURES_DIR/island_pipeline_cases.jsonl"
[ -f "$SRC_CASES" ] || { echo "source not found: $SRC_CASES" >&2; exit 1; }
mkdir -p "$FIXTURES_DIR"
cp "$SRC_CASES" "$DEST_CASES"
echo "vendored $SRC_CASES -> $DEST_CASES"

# --- model tables ---
SRC_VALIDATOR="$READING_CORE_DIR/src/reading-validator.ts"
DEST_VALIDATOR="$FIXTURES_DIR/reading-validator.ts"
[ -f "$SRC_VALIDATOR" ] || { echo "source not found: $SRC_VALIDATOR" >&2; exit 1; }
cp "$SRC_VALIDATOR" "$DEST_VALIDATOR"
echo "vendored $SRC_VALIDATOR -> $DEST_VALIDATOR"
