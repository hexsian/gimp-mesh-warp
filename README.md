# GIMP 3.2+ Mesh Warp & Transform Presets Plugin

### Download code and binaries for the latest version here

https://github.com/hexsian/gimp-mesh-warp

---

### NEWS

This is a fork of [bunnywaffle/gimp-mesh-warp](https://github.com/bunnywaffle/gimp-mesh-warp) with major additions: Non-Destructive Editing (NDE) through GIMP 3.2's `Gimp.DrawableFilter` API, a Puppet Pins mode with windowed Gaussian RBF, performance fixes, and a rewritten C GEGL operation that supports caching, GMutex thread safety, and tight output bounds.

Requires GIMP 3.2 or newer. The NDE workflow depends on `Gimp.DrawableFilter`, which was stabilised in GIMP 3.2. Older 3.0.x builds may work in a semi-destructive mode but the layer-effects UI will not show the filter.

Binaries are currently provided for Linux only. Windows and macOS users must compile the GEGL operation from source. Building on Windows with MSYS2 has been tested; macOS is untested but should work with Homebrew dependencies.

---

### Third party GEGL GIMP Plugin — Mesh Warp
====================================================

This is a third-party GEGL filter plugin for GIMP 3.2+, packaged as two pieces. The first is a small C GEGL operation called `custom:mesh-warp` which performs the actual backward pixel remap. The second is a Python plugin that provides the interactive dialog, canvas, grid editing, and puppet pins.

Unlike most chained-node GEGL plugins, this one contains its own pixel math (bilinear backward remapping with a per-pixel inverse solve). That is what allows it to run as a real non-destructive layer effect instead of a bake-and-done filter.

Everything is open source, GPL-3.0 licensed, and the original upstream work is credited below.

---

## What this plugin does

**15 Photoshop Warp presets** — Arc, Arc Lower, Arc Upper, Arch, Bulge, Shell Lower, Shell Upper, Flag, Wave, Fish, Rise, FishEye, Inflate, Squeeze, Twist, plus a Custom mode for free-form grid dragging. Bend / H-Distort / V-Distort sliders and a Vertical orientation toggle.

**Non-Destructive Editing** — warp is applied as a GIMP 3.2 layer effect. Re-open the filter at any time to change the grid, pins, or preset. Pixels are never baked.

**Puppet Pins mode** — click to place pins, drag to deform locally. Multiple pins blend smoothly via a windowed Gaussian RBF. Adjustable Pin Radius.

**Seam-free output** — final layer commits use direct continuous backward remapping with sub-pixel bilinear interpolation. No grid, no wireframe, no zigzag seams.

**Interactive canvas** — wheel zoom, Space+drag or middle-drag pan, marquee selection, Shift+click multi-select, H toggles the grid overlay.

**Falloff, symmetry, smoothing** — cosine falloff brush, mirror symmetry (L/R and T/B), one-click Laplacian relaxation.

**Auto-expand layer** — artwork that warps past the original boundaries is preserved, never clipped.

One caveat about Puppet mode: it uses a Gaussian RBF, not ARAP (As-Rigid-As-Possible). It is excellent for localised warps on face, hair, fabric, and contours, but it cannot rotate limbs around a joint the way Photoshop's Puppet Warp does. If you need genuine pose changes, this plugin will not replace Photoshop Puppet Warp.

---

## Windows

The `.dll` and `.py` files go in separate places.

The Python plugin lives here, and you may need to create the folder if it does not exist:

`C:\Users\USERNAME\AppData\Roaming\GIMP\3.2\plug-ins\mesh-warp\mesh-warp.py`

The GEGL operation lives here, and you may also need to create this folder:

`C:\Users\USERNAME\AppData\Local\gegl-0.4\plug-ins\mesh-warp.dll`

Then restart GIMP. The plugin should appear under Filters → Distorts → Mesh Warp.

For Windows portable apps, search for a folder called `gegl-0.4` inside your GIMPPortable installation, create a `plug-ins` subfolder if it does not exist, and drop `mesh-warp.dll` in there. The Python plugin goes in the equivalent `...\GIMPPortable\App\gimp\share\gimp\3.2\plug-ins\mesh-warp\` folder.

There are currently no precompiled Windows binaries. You must build `mesh-warp.dll` from source with MSYS2 and place the result in the GEGL folder listed above.

---

## Linux

The `.so` and `.py` files go in separate places.

The Python plugin goes here, and you may need to create the folder:

`~/.config/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`

The GEGL operation goes here:

`~/.local/share/gegl-0.4/plug-ins/mesh-warp.so`

Then restart GIMP and open Filters → Distorts → Mesh Warp.

Precompiled `.so` binaries will be attached to GitHub releases. If you use a release binary, skip the compilation step and just copy the `.so` into the GEGL folder listed above.

For Flatpak Linux (including Chromebook GIMP as Flatpak), GIMP's sandbox has its own GEGL directory. Put the `.so` here instead:

`~/.var/app/org.gimp.GIMP/data/gegl-0.4/plug-ins/mesh-warp.so`

The Python plugin still goes in the usual location at `~/.config/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`.

For Snap Linux, GIMP uses a versioned directory. The path looks like `~/snap/gimp/393/.local/share/gegl-0.4/plug-ins/mesh-warp.so` where the number 393 may vary by Snap revision. List `~/snap/gimp/` to find the correct number, then create the `plug-ins` folder if it does not exist. The Python plugin still goes in the usual `~/.config/GIMP/3.2/plug-ins/mesh-warp/` folder.

---

## macOS (untested, no precompiled binaries)

`.dylib` files for GEGL go here:

`~/Library/Application Support/GEGL/0.4/plug-ins/mesh-warp.dylib`

The Python plugin goes here:

`~/Library/Application Support/GIMP/3.2/plug-ins/mesh-warp/mesh-warp.py`

You may need to create the `plug-ins` folder if it does not exist.

To compile on macOS, install Homebrew first from https://brew.sh/ and then run the following commands:

`brew install pkg-config glib gegl`

Build the operation with:

`gcc -O2 -shared -fPIC mesh-warp.c -o mesh-warp.dylib $(pkg-config --cflags --libs gegl-0.4 glib-2.0)`

After copying the file into the GEGL folder, macOS Gatekeeper may block it. Right-click the `.dylib` in Finder and select Open, then approve the security prompt. To batch-approve everything in the folder, run `cd ~/Library/Application\ Support/GEGL/0.4/plug-ins/` and then `sudo xattr -rd com.apple.quarantine *.dylib`. Then restart GIMP.

---

## How to compile

Build dependencies vary by platform. The core requirements are a C compiler, `pkg-config`, GLib development headers, and GEGL development headers.

On Debian and Ubuntu, install the dependencies with `sudo apt install build-essential pkg-config libglib2.0-dev libgegl-dev`.

On Arch Linux, use `sudo pacman -S base-devel pkg-config glib2 gegl`.

On Fedora, run `sudo dnf install gcc pkg-config glib2-devel gegl-devel`.

On macOS with Homebrew, use `brew install pkg-config glib gegl`.

On Windows with MSYS2 MinGW64, run `pacman -S mingw-w64-x86_64-toolchain pkg-config mingw-w64-x86_64-glib2 mingw-w64-x86_64-gegl`.

Then build the GEGL operation. On Linux and macOS, use:

`gcc -O2 -shared -fPIC mesh-warp.c -o mesh-warp.so $(pkg-config --cflags --libs gegl-0.4 glib-2.0)`

On Windows, the same command produces a `.dll` instead of a `.so`, so change the output name to `mesh-warp.dll`.

The provided `build.sh` automates this on Linux, macOS, and MSYS2.

For Puppet-mode performance, NumPy is optional but strongly recommended. Puppet pins rebuild the grid via matrix math, and without NumPy the plugin falls back to a pure-Python loop that is 10 to 30 times slower. NumPy ships with most GIMP 3.2 builds. If your build does not include it, install it with `python3 -m pip install --user numpy`. For Flatpak GIMP, verify with `flatpak run --command=python3 org.gimp.GIMP -c "import numpy; print(numpy.__version__)"`.

---

## Frequently Asked Questions

**The plugin does not appear in Filters → Distorts.** Check GIMP's Error Console under Windows → Dockable Dialogs → Error Console for `dlopen` or `symbol lookup` errors. The most common causes are that `mesh-warp.so` or `mesh-warp.dll` is missing from the GEGL plug-ins folder or placed in the wrong folder (GEGL has its own directory separate from GIMP's), or that the GEGL operation was built against a different GEGL version than the one GIMP links against. Rebuild with the same GEGL headers GIMP uses. On Flatpak or Snap GIMP, the path is different as described in the Linux section above.

**The layer-effects list does not show Mesh Warp.** You need GIMP 3.2 or newer. The NDE workflow is built on the `Gimp.DrawableFilter` API, which was not stable in GIMP 3.0.x. On 3.0.x the plugin may still apply but as a traditional destructive filter.

**Applying the plugin crashes GIMP.** This usually means `mesh-warp.so` was built against a different GEGL version than the one GIMP uses. Rebuild the `.so` from source on the same machine that runs GIMP.

**Puppet mode is very slow with many pins.** Install NumPy. Without it, a 16×16 grid with 20 or more pins can drop below 20 FPS on modest hardware.

**Can I rotate a limb with Puppet mode like Photoshop Puppet Warp?** No. This plugin uses a Gaussian RBF, which translates but does not rotate. Genuine pose changes need ARAP (As-Rigid-As-Possible), which is not implemented here. The plugin is intended for localised warps on face, hair, fabric, and contours, not for rigging.

**My preview does not match the applied output at the edges.** The interactive canvas uses fast Cairo triangle rasterisation. The final Apply uses the C GEGL backward remapping. They may differ by sub-pixel amounts at edges under strong warp. This is intentional — the preview prioritises speed, the apply prioritises accuracy.

---

## Notes about the GEGL plug-ins folder

The GEGL `plug-ins` directory should contain only binary files (`.so`, `.dll`, `.dylib`) for your OS. Do not create subfolders for each plugin, and do not place source files, README files, or archives in there.

If you have multiple copies of the same dependency DLL on your system, GEGL may load an older one, which can cause plugins that require a newer version to fail. Keep a single copy of `mesh-warp.so` in the plug-ins directory.

If GIMP refuses to start after adding a file to the GEGL plug-ins folder, one of the files in that folder is not a valid GEGL operation. Remove it and try again.

---

## Credits and License

Original plugin by [bunnywaffle](https://github.com/bunnywaffle/gimp-mesh-warp).

Licensed under GNU GPL v3.0 or later. The LICENSE file in this repository contains the full GPL-3.0 text and both the original and derivative copyright notices.

If you use this plugin in a project, please credit both the upstream author and this fork.

Enjoy!

---

## Changelog for this fork

See `CHANGELOG.md` for the full history. Summary of what changed in v2.0.0: NDE via DrawableFilter, Puppet Pins mode, windowed Gaussian falloff, NumPy-accelerated pin rebuild, GMutex-protected GEGL cache, dynamic preview subdivision, dirty-rect redraw, and mode-switch state persistence. The upstream v1.0.0 had the original 15-preset plugin with grid editing, falloff, symmetry, and Laplacian smoothing.
