#!/usr/bin/env bash
#
# build.sh — Build the mesh-warp GEGL operation and install both the
#            C operation and the Python plugin into the correct folders.
#
# Usage:
#   chmod +x build.sh
#   ./build.sh
#
# Supports: Linux, macOS, Windows (MSYS2/MinGW).
# Requires: gcc (or clang), pkg-config, GEGL dev headers, GLib dev headers.

set -euo pipefail

# ---------------------------------------------------------------------------
# Detect platform and set paths
# ---------------------------------------------------------------------------
OS="$(uname -s)"

case "$OS" in
  Linux*)
    SO_EXT="so"
    PLUGIN_DIR="${HOME}/.config/GIMP/3.2/plug-ins/mesh-warp"
    GEGL_DIR="${HOME}/.local/share/gegl-0.4/plug-ins"
    # Flatpak check
    if [ -d "${HOME}/.var/app/org.gimp.GIMP" ]; then
      GEGL_DIR="${HOME}/.var/app/org.gimp.GIMP/data/gegl-0.4/plug-ins"
      echo "==> Flatpak GIMP detected — using Flatpak GEGL plug-ins path"
    fi
    ;;
  Darwin*)
    SO_EXT="so"
    PLUGIN_DIR="${HOME}/Library/Application Support/GIMP/3.2/plug-ins/mesh-warp"
    GEGL_DIR="${HOME}/Library/Application Support/GEGL/0.4/plug-ins"
    ;;
  MINGW*|MSYS*|CYGWIN*)
    SO_EXT="dll"
    PLUGIN_DIR="${APPDATA:-$HOME}/GIMP/3.2/plug-ins/mesh-warp"
    GEGL_DIR="${LOCALAPPDATA:-$HOME}/gegl-0.4/plug-ins"
    ;;
  *)
    echo "Unsupported OS: $OS" >&2
    exit 1
    ;;
esac

echo "==> Platform: $OS"
echo "==> Plugin folder: $PLUGIN_DIR"
echo "==> GEGL folder:   $GEGL_DIR"
echo

# ---------------------------------------------------------------------------
# Check dependencies
# ---------------------------------------------------------------------------
for cmd in gcc pkg-config; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: '$cmd' not found. Install it and try again." >&2
    exit 1
  fi
done

if ! pkg-config --exists gegl-0.4; then
  echo "ERROR: gegl-0.4 not found. Install GEGL development headers." >&2
  exit 1
fi

if ! pkg-config --exists glib-2.0; then
  echo "ERROR: glib-2.0 not found. Install GLib development headers." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Build the GEGL operation
# ---------------------------------------------------------------------------
OUTPUT="mesh-warp.${SO_EXT}"
echo "==> Building ${OUTPUT} ..."
gcc -O2 -shared -fPIC mesh-warp.c -o "$OUTPUT" \
    $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
echo "    Built: $OUTPUT"
echo

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
echo "==> Installing Python plugin to: $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp mesh-warp.py "$PLUGIN_DIR/"
chmod +x "$PLUGIN_DIR/mesh-warp.py" 2>/dev/null || true

echo "==> Installing GEGL operation to: $GEGL_DIR"
mkdir -p "$GEGL_DIR"
cp "$OUTPUT" "$GEGL_DIR/"

echo
echo "==> Done."
echo "    Restart GIMP, then use: Filters → Distorts → Mesh Warp…"
echo
echo "    If the filter does not appear, check GIMP's Error Console"
echo "    (Windows → Dockable Dialogs → Error Console) for dlopen errors."