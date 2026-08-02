#!/bin/bash
# Update installed MediaHub app(s) with the latest engine code from this repo.
# (Fast in-place refresh — for a clean signed rebuild use tools/build_dmg.sh instead.)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
updated=0
for APP in "/Applications/MediaHub.app" "$HOME/Desktop/MediaHub-Native.app"; do
  [ -d "$APP" ] || continue
  DEST="$APP/Contents/Resources/app"
  for d in mediahub embed vision; do
    rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' --exclude '_demo.html' \
      "$ROOT/$d" "$DEST/"
  done
  cp "$ROOT/README.md" "$ROOT/DESIGN.md" "$DEST/" 2>/dev/null || true
  echo "✓ updated $APP"
  updated=$((updated+1))
done
[ "$updated" -gt 0 ] && echo "Done. Quit MediaHub and reopen it to load the new code." \
  || echo "No installed MediaHub.app found."
