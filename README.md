# GIMP 3.2+ Mesh Warp & Transform Presets Plugin

### Download code and binaries for the latest version here

https://github.com/hexsian/gimp-mesh-warp/releases

Demo: 

https://github.com/user-attachments/assets/8fe7900e-7de2-4509-82cb-0e841ce57e48

---

### NEWS

This is a fork of [bunnywaffle/gimp-mesh-warp](https://github.com/bunnywaffle/gimp-mesh-warp) with major additions: Non-Destructive Editing (NDE) through GIMP 3.2's `Gimp.DrawableFilter` API, a Puppet Pins mode with windowed Gaussian RBF in C, performance fixes, and a rewritten C GEGL operation that supports caching, GMutex thread safety, and tight output bounds.

Requires GIMP 3.2 or newer. The NDE workflow depends on `Gimp.DrawableFilter`, which was stabilised in GIMP 3.2. Older 3.0.x builds may work in a semi-destructive mode but the layer-effects UI will not show the filter.

Binaries are provided for Linux and Windows. macOS users must compile the GEGL operation from source. Building on Windows with MSYS2 has been tested; macOS is untested but should work with Homebrew dependencies.

---

### Third party GEGL GIMP Plugin — Mesh Warp

This is a third-party GEGL filter plugin for GIMP 3.2+, packaged as two pieces. The first is a small C GEGL operation called `custom:mesh-warp` which performs the actual backward pixel remap and provides ctypes fast-preview functions. The second is a Python plugin that provides the interactive dialog, canvas, grid editing, and puppet pins.

Unlike most chained-node GEGL plugins, this one contains its own pixel math (bilinear backward remapping with a per-pixel inverse solve). That is what allows it to run as a real non-destructive layer effect instead of a bake-and-done filter.

Everything is open source, GPL-3.0 licensed, and the original upstream work is credited below.

---

## What this plugin does

**15 Photoshop Warp presets** — Arc, Arc Lower, Arc Upper, Arch, Bulge, Shell Lower, Shell Upper, Flag, Wave, Fish, Rise, FishEye, Inflate, Squeeze, Twist, plus a Custom mode for free-form grid dragging. Bend / H-Distort / V-Distort sliders and a Vertical orientation toggle.

**Non-Destructive Editing** — warp is applied as a GIMP 3.2 layer effect. Re-open the filter at any time to change the grid, pins, or preset. Pixels are never baked.

**Puppet Pins mode** — click to place pins, drag to deform locally. Multiple pins blend smoothly via native C-accelerated Gaussian RBF math (no NumPy required). Adjustable Pin Radius.

**Seam-free output** — final layer commits use direct continuous backward remapping with sub-pixel bilinear interpolation. No grid, no wireframe, no zigzag seams.

**Interactive canvas** — wheel zoom, Space+drag or middle-drag pan, marquee selection, Shift+click multi-select, H toggles the grid overlay.

**Falloff, symmetry, smoothing** — cosine falloff brush, mirror symmetry (L/R and T/B), one-click Laplacian relaxation.

**Auto-expand layer** — artwork that warps past the original boundaries is preserved, never clipped.

One caveat about Puppet mode: it uses a Gaussian RBF, not ARAP (As-Rigid-As-Possible). It is excellent for localised warps on face, hair, fabric, and contours, but it cannot rotate limbs around a joint the way Photoshop's Puppet Warp does. If you need genuine pose changes, this plugin will not replace Photoshop Puppet Warp.

---

## Windows Installation

The `.dll` and `.py` files can either be placed together or in their respective system folders.

### Method 1: All-in-one plugin folder (Recommended)
Place both `mesh-warp.py` and `mesh-warp.dll` together in:

`C:\Users\USERNAME\AppData\Roaming\GIMP\3.2\plug-ins\mesh-warp\`

*(Create the `mesh-warp` subfolder if it does not exist).*

### Method 2: Separate GEGL folder
The Python plugin lives here:

`C:\Users\USERNAME\AppData\Roaming\GIMP\3.2\plug-ins\mesh-warp\mesh-warp.py`

And the GEGL operation `.dll` lives here:

`C:\Users\USERNAME\AppData\Local\gegl-0.4\plug-ins\mesh-warp.dll`

### Usage:
1. Restart GIMP.
2. **Open or create an image with an active layer** (the plugin requires an active drawable to appear).
3. The plugin will be available under:
   * **Filters → Deform → Mesh Warp...**
   * or **Tools → Transform Tools → Mesh Warp...**

For Windows portable apps, place `mesh-warp.dll` inside the `gegl-0.4\plug-ins\` folder of your GIMPPortable installation, and place `mesh-warp.py` inside `...\GIMPPortable\App\gimp\share\gimp\3.2\plug-ins\mesh-warp\`.

---

## Linux

The `.so` and `.py` files go in separate places.

The Python plugin goes here, and you may need to create the folder:

`~/.config/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`

The GEGL operation goes here:

`~/.local/share/gegl-0.4/plug-ins/mesh-warp.so`

Then restart GIMP and open **Filters → Deform → Mesh Warp...** (or **Tools → Transform Tools → Mesh Warp...**).

Precompiled `.so` binaries are attached to GitHub releases. If you use a release binary, skip compilation and copy the `.so` directly.

For Flatpak Linux (including Chromebook GIMP as Flatpak), put the `.so` here:

`~/.var/app/org.gimp.GIMP/data/gegl-0.4/plug-ins/mesh-warp.so`

The Python plugin remains at `~/.config/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`.

For Snap Linux:

`~/snap/gimp/COMMON_REVISION/.local/share/gegl-0.4/plug-ins/mesh-warp.so`

---

## macOS (untested, no precompiled binaries)

`.dylib` files for GEGL go here:

`~/Library/Application Support/GEGL/0.4/plug-ins/mesh-warp.dylib`

The Python plugin goes here:

`~/Library/Application Support/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`

To compile on macOS via Homebrew:

```bash
brew install pkg-config glib gegl
gcc -O3 -ftree-vectorize -fno-math-errno -fno-trapping-math -shared -fPIC \
    -I. mesh-warp.c -o mesh-warp.dylib -lm \
    $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
```

After copying the file into the GEGL folder, Gatekeeper may block it. Run:

```bash
cd ~/Library/Application\ Support/GEGL/0.4/plug-ins/
sudo xattr -rd com.apple.quarantine *.dylib
```

> **Note for Linux / macOS**: Ensure the Python script is executable:
> ```bash
> chmod +x mesh-warp/mesh-warp.py
> ```

---

## How to compile

Build dependencies vary by platform: C compiler, `pkg-config`, GLib development headers, and GEGL development headers.

* **Debian / Ubuntu**: `sudo apt install build-essential pkg-config libglib2.0-dev libgegl-dev`
* **Arch Linux**: `sudo pacman -S base-devel pkg-config glib2 gegl`
* **Fedora**: `sudo dnf install gcc pkg-config glib2-devel gegl-devel`
* **Windows (MSYS2 UCRT64 / MINGW64)**: `pacman -S mingw-w64-ucrt-x86_64-toolchain pkg-config mingw-w64-ucrt-x86_64-glib2 mingw-w64-ucrt-x86_64-gegl`

### Build command for Linux / macOS:

```bash
gcc -O3 -ftree-vectorize -fno-math-errno -fno-trapping-math -shared -fPIC -fopenmp \
    -I. mesh-warp.c -o mesh-warp.so -lm \
    $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
```

### Build command for Windows (MSYS2):

> **Important**: On Windows, you must pass `-Wl,--export-all-symbols` so that Python `ctypes` can access the preview rendering functions. Drop `-fopenmp` to prevent external dependencies like `libgomp-1.dll`:

```bash
gcc -O3 -shared -Wl,--export-all-symbols -I. mesh-warp.c -o mesh-warp.dll \
    $(pkg-config --cflags --libs gegl-0.4 glib-2.0)
```

*(If you build with `-fopenmp` for multithreading, you must bundle `libgomp-1.dll` and `libwinpthread-1.dll` alongside `mesh-warp.dll`, otherwise standard Windows GIMP installations will fail to load the library).*

---

## Frequently Asked Questions

**The plugin does not appear in the menu.**
* **Make sure an image is open:** The plugin uses `Gimp.ProcedureSensitivityMask.DRAWABLE`. If no image or layer is open, GIMP hides or disables the menu item.
* Check the correct menu location: **Filters → Deform → Mesh Warp...** or **Tools → Transform Tools → Mesh Warp...**.
* If GIMP crashed on a previous launch, it may have cached the failed state in `pluginrc`. Delete `pluginrc` inside `AppData\Roaming\GIMP\3.2\` and restart GIMP.

**Error: `AttributeError: function 'mesh_preview_render' not found`.**
* Your `mesh-warp.dll` was compiled without exported symbol visibility. Rebuild the `.dll` with the `-Wl,--export-all-symbols` flag, or ensure the functions are tagged with `__declspec(dllexport)`.

**Error: `Could not find module '...mesh-warp.dll' (or one of its dependencies)`.**
* The DLL was compiled with `-fopenmp`, but the target machine lacks `libgomp-1.dll`. Recompile the DLL without `-fopenmp` (recommended for distribution) or copy `libgomp-1.dll` into the plug-ins directory next to `mesh-warp.dll`.

**Do I need NumPy installed?**
* No. Unlike earlier Python-only implementations, all heavy mathematical lifting (bilinear sampling, Laplacian smoothing, and Gaussian RBF for puppet pins) is computed directly in the C operation via `ctypes`.

**The layer-effects list does not show Mesh Warp.**
* You need GIMP 3.2 or newer. The NDE workflow is built on the `Gimp.DrawableFilter` API, which was not stable in GIMP 3.0.x.

**Can I rotate limbs with Puppet mode like Photoshop Puppet Warp?**
* No. This plugin uses a Gaussian RBF, which translates but does not perform rigid rotation. It is designed for localized deforms (facial expressions, hair, cloth), not full-body character rigging.

---

## Credits and License

* Original plugin by [bunnywaffle](https://github.com/bunnywaffle/gimp-mesh-warp).
* Enhancements & GIMP 3.2+ NDE port by [Hexsian](https://github.com/hexsian/gimp-mesh-warp).

Licensed under GNU GPL v3.0 or later.
