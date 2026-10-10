#!/usr/bin/env bash
# Copy the repository-root presets into the component, so HACS installs them.
#
# HACS installs only custom_components/svitgrid/. The root presets/ stays the
# source: the monorepo's sync-ha-presets.mts and seed-from-yaml.cjs read it.
# tests/test_bundled_presets.py fails when the two copies differ.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_DIR/presets"
DEST="$REPO_DIR/custom_components/svitgrid/presets"

[ -d "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
mkdir -p "$DEST"
# Replace, not merge: a preset deleted at the root must leave the bundle too.
find "$DEST" -maxdepth 1 -name '*.yaml' -delete
cp "$SRC"/*.yaml "$DEST"/
echo "bundled $(ls "$DEST"/*.yaml | wc -l | tr -d ' ') preset(s) into $DEST"
