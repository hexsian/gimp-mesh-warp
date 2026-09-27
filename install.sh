#!/usr/bin/env bash
#
# install.sh — One-command installer for Linux / macOS.
#              Builds the GEGL operation (if needed) and copies both
#              the C operation and the Python plugin into place.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# For Windows, use build.sh inside an MSYS2 MinGW shell.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------
OS="$(uname -s)"

case "$OS" in
  Linux*)
    SO_EXT="so"
    PLUGIN_DIR="${HOME}/.config/GIMP/3.2/plug-ins/mesh-warp"
    GEGL_DIR="${HOME}/.local/share/gegl-0.4/plug-ins"
    if [ -d "${HOME}/.var/app/org.gimp.GIMP" ]; then
      GEGL_DIR="${HOME}/.var/app/org.gimp.GIMP/data/gegl-0.4/plug-ins"
      echo "==> Flatpak GIMP detected"
    fi
    ;;
  Darwin*)
    SO_EXT="so"
    PLUGIN_DIR="${HOME}/Library/Application Support/GIMP/3.2/plug-ins/mesh-warp"
    GEGL_DIR="${HOME}/Library/Application Support/GEGL/0.4/plug-ins"
    ;;
  *)
    echo "This script supports Linux and macOS only." >&2
    echo "For Windows, use build.sh in an MSYS2 MinGW shell." >&2
    exit 1
    ;;
esac

echo "==> Mesh Warp Installer"
echo "    Platform:    $OS"
echo "    Plugin dir:  $PLUGIN_DIR"
echo "    GEGL dir:    $GEGL_DIR"
echo

# ---------------------------------------------------------------------------
# Check for existing .so / .dll
# ---------------------------------------------------------------------------
OUTPUT="mesh-warp.${SO_EXT}"

if [ -f "$OUTPUT" ]; then
  echo "==> Found pre-built $OUTPUT — skipping compilation."
  echo "    (Delete it and re-run to rebuild.)"
else
  echo "==> No pre-built $OUTPUT found — building now..."
  if ! command -v gcc >/dev/null 2>&1; then
    echo "ERROR: gcc not found. Install build tools and try again." >&2
    exit 1
  fi
  if ! pkg-config --exists gegl-0.4; then
    echo "ERROR: gegl-0.4 not found. Install GEGL dev headers." >&2
    exit 1
  fi
  gcc -O2 -shared -fPIC mesh-warp.c -o "$OUTPUT" \
      $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
  echo "    Built: $OUTPUT"
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
echo
echo "==> Installing Python plugin..."
mkdir -p "$PLUGIN_DIR"
cp mesh-warp.py "$PLUGIN_DIR/"
chmod +x "$PLUGIN_DIR/mesh-warp.py" 2>/dev/null || true

echo "==> Installing GEGL operation..."
mkdir -p "$GEGL_DIR"
cp "$OUTPUT" "$GEGL_DIR/"

echo
echo "======================================================"
echo "  Installation complete."
echo "======================================================"
echo
echo "  Next steps:"
echo "    1. Restart GIMP."
echo "    2. Open an image."
echo "    3. Filters → Distorts → Mesh Warp…"
echo
echo "  If the filter does not appear, open GIMP's Error Console"
echo "  (Windows → Dockable Dialogs → Error Console) and look for"
echo "  'dlopen' or 'symbol' errors."
echo