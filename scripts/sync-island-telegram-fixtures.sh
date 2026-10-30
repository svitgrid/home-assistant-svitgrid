#!/usr/bin/env bash
# Copy the monorepo's island Telegram fixtures into this repo.
# The copy file ships inside the integration; the alert cases are test-only.
set -euo pipefail
INVERTER_PROTOCOL_DIR="${1:-$HOME/git/svitgrid/packages/inverter_protocol}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COPY_DIR="$ROOT/custom_components/svitgrid/telegram/copy"
FIXTURES_DIR="$ROOT/tests/fixtures"
mkdir -p "$COPY_DIR" "$FIXTURES_DIR"

vendor() {
  local SRC="$INVERTER_PROTOCOL_DIR/fixtures/$1"
  [ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC" "$2/$1"
  echo "vendored $SRC -> $2/$1"
}
vendor island_telegram_copy.json "$COPY_DIR"
vendor island_alert_cases.json "$FIXTURES_DIR"
