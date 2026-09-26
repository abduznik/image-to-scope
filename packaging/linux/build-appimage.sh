#!/usr/bin/env bash
# Wrap the PyInstaller one-folder build (dist/ImageToScope) into an AppImage.
# Usage: packaging/linux/build-appimage.sh <output.AppImage>
set -euo pipefail
OUT="${1:?usage: build-appimage.sh <output.AppImage>}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/packaging/linux"
APPDIR="$ROOT/build/AppDir"

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$ROOT/dist/ImageToScope" "$APPDIR/usr/bin/"
install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 644 "$HERE/image-to-scope.desktop" "$APPDIR/image-to-scope.desktop"
install -m 644 "$HERE/image-to-scope.desktop" "$APPDIR/usr/share/applications/"
install -m 644 "$ROOT/assets/icon-256.png" "$APPDIR/image-to-scope.png"
install -m 644 "$ROOT/assets/icon-256.png" \
        "$APPDIR/usr/share/icons/hicolor/256x256/apps/image-to-scope.png"
ln -sf image-to-scope.png "$APPDIR/.DirIcon"

TOOL="$ROOT/build/appimagetool"
if [ ! -x "$TOOL" ]; then
  curl -fsSL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
# Extract-and-run so no FUSE is needed on the build machine.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$TOOL" --no-appstream "$APPDIR" "$OUT"
