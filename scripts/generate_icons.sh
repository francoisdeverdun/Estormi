#!/usr/bin/env bash
# Generate the macOS app icon set (.png variants + icon.icns) from the brand
# master PNG. Called by `make bundle`; can also be run standalone:
#   bash scripts/generate_icons.sh
#
# Resolves the repo root from this script's own location so it works from any
# working directory.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/assets/brand/estormi-app-icon.png"
[ -f "$SRC" ] || { echo "generate_icons: master icon $SRC not found" >&2; exit 1; }
ICONS_DIR="$ROOT/apps/estormi-macos/icons"
ICONSET="$ICONS_DIR/estormi-mark.iconset"

mkdir -p "$ICONS_DIR"
sips -z 32 32 "$SRC" --out "$ICONS_DIR/32x32.png" 2>/dev/null || true
sips -z 128 128 "$SRC" --out "$ICONS_DIR/128x128.png" 2>/dev/null || true
sips -z 256 256 "$SRC" --out "$ICONS_DIR/128x128@2x.png" 2>/dev/null || true

mkdir -p "$ICONSET"
for spec in icon_16x16.png:16 icon_16x16@2x.png:32 icon_32x32.png:32 \
            icon_32x32@2x.png:64 icon_64x64.png:64 icon_64x64@2x.png:128 \
            icon_128x128.png:128 icon_128x128@2x.png:256 icon_256x256.png:256 \
            icon_256x256@2x.png:512 icon_512x512.png:512 icon_512x512@2x.png:1024; do
  name="${spec%%:*}"; size="${spec##*:}"
  sips -z "$size" "$size" "$SRC" --out "$ICONSET/$name" >/dev/null 2>&1 || true
done

iconutil -c icns "$ICONSET" -o "$ICONS_DIR/icon.icns" 2>/dev/null || true
