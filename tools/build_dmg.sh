#!/bin/bash
# Build a drag-to-install MediaHub.dmg: compiles the SwiftUI shell, bundles the
# Python engine + icon, and packages a DMG with an /Applications drop target.
#
# Requirements on the BUILD machine: swiftc (Xcode CLT), hdiutil (built in).
# Requirement on the TARGET machine: python3 (Xcode CLT or Homebrew).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="/tmp/mh_build/MediaHub.app"
STAGE="/tmp/mh_dmg_stage"
OUT="$HOME/Desktop/MediaHub-Installer.dmg"
VERSION="2.0"

echo "▸ Cleaning build dirs"
rm -rf /tmp/mh_build "$STAGE" "$OUT"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "▸ Compiling SwiftUI + WKWebView shell"
swiftc -O -parse-as-library -framework WebKit -framework AppKit \
  "$ROOT/shell/MediaHubShell.swift" -o "$APP/Contents/MacOS/MediaHub"

echo "▸ Bundling Python engine (mediahub + embed + vision)"
for d in mediahub embed vision; do
  rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '_demo.html' \
    "$ROOT/$d" "$APP/Contents/Resources/app/"
done
cp "$ROOT/README.md" "$ROOT/DESIGN.md" "$APP/Contents/Resources/app/" 2>/dev/null || true

echo "▸ Prebuilding Vision + Face Swift tools (so the target needs no swiftc)"
VDIR="$APP/Contents/Resources/app/vision"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -O -framework Vision -framework AppKit -framework CoreImage \
    "$ROOT/vision/vision_tag.swift"   -o "$VDIR/vision_tag"   && echo "  ✓ vision_tag"
  swiftc -O -framework Vision -framework AppKit -framework CoreImage \
    "$ROOT/vision/face_detect.swift"  -o "$VDIR/face_detect"  && echo "  ✓ face_detect"
  touch "$VDIR/vision_tag" "$VDIR/face_detect"   # mtime >= source so they're used as-is
else
  echo "  ! swiftc not found — Vision/Faces will build on first use on the target (needs Xcode CLT)"
fi

# ── Optional: bundle a standalone Python so the target needs ZERO dependencies ──
BUNDLE_PYTHON="${BUNDLE_PYTHON:-1}"
PY_TAG="${PY_TAG:-20240814}"
PY_VER="${PY_VER:-3.12.5}"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}+${PY_TAG}-aarch64-apple-darwin-install_only.tar.gz"
if [ "$BUNDLE_PYTHON" = "1" ]; then
  echo "▸ Attempting to bundle standalone Python (${PY_VER})"
  TARB="/tmp/mh_python.tar.gz"
  if curl -fsSL "$PY_URL" -o "$TARB" 2>/dev/null; then
    tar -xzf "$TARB" -C "$APP/Contents/Resources/"   # extracts a 'python/' dir
    if [ -x "$APP/Contents/Resources/python/bin/python3" ]; then
      echo "  ✓ bundled Python -> Contents/Resources/python (zero external deps)"
    else
      echo "  ! extraction did not yield python/bin/python3 — falling back to system Python"
      rm -rf "$APP/Contents/Resources/python"
    fi
    rm -f "$TARB"
  else
    echo "  ! could not download standalone Python (offline?) — app will use the target's system python3"
    echo "    To bundle later: rerun on a networked Mac, or set BUNDLE_PYTHON=0 to skip intentionally."
  fi
fi

echo "▸ Icon"
cp "$ROOT/shell/MediaHub.icns" "$APP/Contents/Resources/MediaHub.icns"

echo "▸ Info.plist"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>MediaHub</string>
  <key>CFBundleDisplayName</key><string>MediaHub</string>
  <key>CFBundleIdentifier</key><string>com.kumodi.mediahub.shell</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleExecutable</key><string>MediaHub</string>
  <key>CFBundleIconFile</key><string>MediaHub</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.photography</string>
</dict>
</plist>
PLIST

echo "▸ Staging DMG contents (.app + Applications drop target)"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/MediaHub.app"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/INSTALL.txt" <<'TXT'
MediaHub — install
1. Drag MediaHub.app onto the Applications folder.
2. First launch: right-click MediaHub in Applications -> Open (unsigned app, macOS asks once).
3. Python is bundled inside the app — no install needed. (If the build skipped bundling,
   it falls back to the system python3.)
4. Apple Vision + Face tools are prebuilt inside the app (no Xcode needed). Optional AI
   accelerators (MLX) install from inside the app: Search -> On-device components.
TXT

echo "▸ Building DMG -> $OUT"
hdiutil create -volname "MediaHub" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null

echo "✓ Done: $OUT"
du -h "$OUT" | cut -f1 | sed 's/^/  size: /'
