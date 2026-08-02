#!/bin/bash
# One-step release: build the DMG, then create/replace a GitHub release with it.
#
#   bash tools/release.sh v2.1                 # tag + release title
#   bash tools/release.sh v2.1 "Notes here"    # custom notes
#
# Requires: gh (brew install gh && gh auth login) and swiftc for the DMG build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-}"
NOTES="${2:-Drag-to-install DMG — bundles Python + prebuilt Apple Vision/Face tools. First launch: right-click → Open (unsigned).}"
DMG="$HOME/Desktop/MediaHub-Installer.dmg"

if [ -z "$TAG" ]; then
  echo "usage: bash tools/release.sh <tag> [notes]   e.g. bash tools/release.sh v2.1"; exit 1
fi
command -v gh >/dev/null || { echo "gh not found. Install: brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated. Run: gh auth login"; exit 1; }

echo "▸ [1/3] Building DMG"
bash "$ROOT/tools/build_dmg.sh"
[ -f "$DMG" ] || { echo "DMG not found at $DMG"; exit 1; }

echo "▸ [2/3] Committing any pending source changes (optional, safe if clean)"
if ! git -C "$ROOT" diff --quiet || ! git -C "$ROOT" diff --cached --quiet; then
  echo "  (working tree has changes — commit them yourself before releasing if you want them tagged)"
fi

echo "▸ [3/3] Publishing GitHub release $TAG"
if gh release view "$TAG" -R "$(git -C "$ROOT" remote get-url origin)" >/dev/null 2>&1; then
  echo "  release $TAG exists — replacing the DMG asset"
  gh release upload "$TAG" "$DMG" --clobber
else
  gh release create "$TAG" "$DMG" -t "MediaHub $TAG" -n "$NOTES"
fi

echo "✓ Released $TAG"
gh release view "$TAG" --json url -q .url 2>/dev/null || true
