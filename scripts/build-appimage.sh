#!/usr/bin/env bash
# build-appimage.sh — wrap the PyInstaller output into a Linux AppImage
#
# Prerequisites (installed by CI before this script runs):
#   - appimagetool  (downloaded as AppImage, placed in PATH)
#   - PyInstaller dist/fbsat59/ already built
#
# Output: dist/FBSAT59-x86_64.AppImage

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST_DIR="$REPO_ROOT/dist"
COLLECT_DIR="$DIST_DIR/fbsat59"
APPDIR="$DIST_DIR/AppDir"

# --------------------------------------------------------------------------- #
# Sanity check
# --------------------------------------------------------------------------- #
if [[ ! -d "$COLLECT_DIR" ]]; then
    echo "ERROR: PyInstaller output not found at $COLLECT_DIR" >&2
    echo "       Run 'pyinstaller scripts/fbsat59.spec' first." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# Build AppDir structure
# --------------------------------------------------------------------------- #
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy PyInstaller output into AppDir
cp -r "$COLLECT_DIR/." "$APPDIR/usr/bin/"

# --------------------------------------------------------------------------- #
# System libraries Qt Multimedia (FT4 TX audio output) needs at runtime.
#
# PySide6's libQt6Multimedia.so.6 links libpulse and libxkbcommon, which the
# PySide6 wheel does not ship. They are copied into the AppImage (with their
# own dependencies, e.g. libpulsecommon) so nothing has to be installed on the
# user's machine. libGL/libEGL and glibc deliberately stay host-provided.
# --------------------------------------------------------------------------- #
bundle_system_lib() {
    local soname="$1" path dep
    path="$(ldconfig -p | awk -v n="$soname" '$1 == n && /x86-64/ {print $NF; exit}')"
    if [[ -z "$path" ]]; then
        echo "ERROR: $soname not found on the build machine (apt install the package providing it)" >&2
        exit 1
    fi
    cp -L "$path" "$APPDIR/usr/lib/$soname"
    # Dependencies that are neither glibc nor GL (libpulse -> libpulsecommon, ...).
    while read -r dep; do
        case "$(basename "$dep")" in
            libpulsecommon-*)
                mkdir -p "$APPDIR/usr/lib/pulseaudio"
                cp -L "$dep" "$APPDIR/usr/lib/pulseaudio/"
                cp -L "$dep" "$APPDIR/usr/lib/"
                ;;
        esac
    done < <(ldd "$path" | awk '/=> \// {print $3}')
}

for lib in libpulse.so.0 libxkbcommon.so.0 libXext.so.6 libXrandr.so.2 libdrm.so.2; do
    bundle_system_lib "$lib"
done

# AppRun entry point
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/usr/bin:$HERE/usr/lib:${LD_LIBRARY_PATH:-}"
exec "$HERE/usr/bin/fbsat59" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# .desktop file (required by AppImage spec)
cat > "$APPDIR/usr/share/applications/fbsat59.desktop" << 'EOF'
[Desktop Entry]
Name=FBSAT59
Comment=Amateur Satellite Tracking
Exec=fbsat59
Icon=fbsat59
Type=Application
Categories=HamRadio;Science;
EOF
# Symlink to top-level (appimagetool expects .desktop at AppDir root)
ln -sf usr/share/applications/fbsat59.desktop "$APPDIR/fbsat59.desktop"

# Placeholder icon (256x256 PNG required; replace with real icon when available)
ICON_SRC="$REPO_ROOT/scripts/fbsat59.png"
if [[ -f "$ICON_SRC" ]]; then
    cp "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/256x256/apps/fbsat59.png"
    ln -sf usr/share/icons/hicolor/256x256/apps/fbsat59.png "$APPDIR/fbsat59.png"
else
    # Generate a minimal placeholder PNG using Python + Pillow (available via packaging extras)
    python3 - << 'PYEOF'
from PIL import Image, ImageDraw
img = Image.new("RGBA", (256, 256), (20, 30, 50, 255))
d = ImageDraw.Draw(img)
d.ellipse([16, 16, 240, 240], outline=(88, 166, 255), width=8)
d.text((80, 110), "GP+", fill=(88, 166, 255))
import os, pathlib
out = pathlib.Path(os.environ.get("APPDIR_ICON",
    "dist/AppDir/usr/share/icons/hicolor/256x256/apps/fbsat59.png"))
out.parent.mkdir(parents=True, exist_ok=True)
img.save(str(out))
PYEOF
    export APPDIR_ICON="$APPDIR/usr/share/icons/hicolor/256x256/apps/fbsat59.png"
    ln -sf usr/share/icons/hicolor/256x256/apps/fbsat59.png "$APPDIR/fbsat59.png"
fi

# --------------------------------------------------------------------------- #
# Fail the build if Qt Multimedia would not load (a missing system library
# would otherwise only show up as a TX error on the user's machine).
# --------------------------------------------------------------------------- #
QTMM="$(find "$APPDIR/usr/bin" -name 'libQt6Multimedia.so.6' | head -n 1)"
if [[ -n "$QTMM" ]]; then
    MISSING="$(LD_LIBRARY_PATH="$(dirname "$QTMM"):$APPDIR/usr/bin:$APPDIR/usr/lib" ldd "$QTMM" \
        | grep 'not found' | grep -v -E 'libGL|libEGL' || true)"
    if [[ -n "$MISSING" ]]; then
        echo "ERROR: Qt Multimedia has unresolved libraries in the AppImage:" >&2
        echo "$MISSING" >&2
        exit 1
    fi
else
    echo "WARNING: libQt6Multimedia.so.6 not found in the PyInstaller output" >&2
fi

# --------------------------------------------------------------------------- #
# Download appimagetool if not in PATH
# --------------------------------------------------------------------------- #
if ! command -v appimagetool &>/dev/null; then
    echo "Downloading appimagetool..."
    TOOL="$DIST_DIR/appimagetool-x86_64.AppImage"
    curl -fsSL -o "$TOOL" \
        "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$TOOL"
    alias appimagetool="$TOOL"
    APPIMAGETOOL="$TOOL"
else
    APPIMAGETOOL="appimagetool"
fi

# --------------------------------------------------------------------------- #
# Build AppImage
# --------------------------------------------------------------------------- #
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$DIST_DIR/FBSAT59-x86_64.AppImage"

echo ""
echo "AppImage created: $DIST_DIR/FBSAT59-x86_64.AppImage"
