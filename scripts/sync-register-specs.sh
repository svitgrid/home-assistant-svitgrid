#!/usr/bin/env bash
# Refresh the bundled register specs from a svitgrid monorepo checkout.
#
#   scripts/sync-register-specs.sh [MONOREPO_DIR] [GIT_REF]
#
# MONOREPO_DIR defaults to ~/git/svitgrid. With GIT_REF (for example
# origin/main) the files are read from that ref with `git show`, so a dirty or
# behind working tree does not leak into the bundle. Without it they are
# copied from the working tree.
#
# The bundle is the offline fallback for direct Modbus harvest: the cloud's
# GET /api/v1/register-specs/{modelId} returns the same JSON these files hold
# (plus `updatedAt`), so build_spec reads both. tests/test_bundled_register_specs.py
# checks that every bundled file still builds.
set -euo pipefail
MONOREPO_DIR="${1:-$HOME/git/svitgrid}"
GIT_REF="${2:-}"
SPEC_PATH="packages/inverter_protocol/register-specs"
DEST="$(cd "$(dirname "$0")/.." && pwd)/custom_components/svitgrid/register_specs"

mkdir -p "$DEST"
# Replace, not merge: a spec deleted upstream must leave the bundle too.
find "$DEST" -maxdepth 1 -name '*.json' -delete

if [ -n "$GIT_REF" ]; then
  files=$(git -C "$MONOREPO_DIR" ls-tree -r --name-only "$GIT_REF" "$SPEC_PATH" | grep '\.json$')
  for f in $files; do
    git -C "$MONOREPO_DIR" show "$GIT_REF:$f" > "$DEST/$(basename "$f")"
  done
  source_desc="$MONOREPO_DIR@$GIT_REF"
else
  SRC="$MONOREPO_DIR/$SPEC_PATH"
  [ -d "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
  cp "$SRC"/*.json "$DEST"/
  source_desc="$SRC"
fi
echo "bundled $(ls "$DEST"/*.json | wc -l | tr -d ' ') register spec(s) from $source_desc"
