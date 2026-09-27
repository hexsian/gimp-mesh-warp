# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] — 2026-09-27

Fork enhancements over the upstream v1.0.0 release.

### Added

- **Non-Destructive Editing (NDE)** via `Gimp.DrawableFilter` and the companion
  `custom:mesh-warp` GEGL operation. Warp is now applied as a layer effect and
  can be re-edited at any time without touching the original pixels.
- **Puppet Pins mode** with a windowed Gaussian RBF deformation engine.
  Click empty space to place a pin, drag to deform, right-click to delete.
- **Pin Radius slider** (20–600 px) and **Clear Pins** button.
- **Mode toggle** between Grid and Puppet, with automatic grid-resolution bump
  to 12×12 when entering Puppet mode.
- **NumPy-accelerated pin rebuild** (`_rebuild_grid_from_pins_numpy`) for large
  grids and many pins — 10–30× faster than the pure-Python fallback.
- **GMutex-protected mesh cache** in the C GEGL operation to prevent
  double-free races across GEGL worker threads.
- **Dynamic triangle subdivision** in the Cairo preview so that triangle fill
  count stays roughly constant regardless of grid density.
- **Dirty-rectangle redraw** when dragging grid vertices, so that only the
  affected quads are repainted.
- **Automated install script** (`install.sh`) for Linux and macOS.
- **Build script** (`build.sh`) with platform detection for Linux, macOS, and
  Windows (MSYS2 MinGW).
- **Round-trip state persistence**: `cols`, `rows`, `grid-json`, `pins-json`,
  `mode`, `pad-left`, `pad-top`, `orig-width`, and `orig-height` are all saved
  in the filter configuration and restored on re-edit.
- **README documentation** covering Linux, Windows, macOS, Flatpak, and Snap
  installation paths.

### Fixed

- **Mode-switch state persistence**: switching between Grid and Puppet no
  longer discards pins. Pins are cleared only when the grid is edited (vertex
  drag, preset change, smooth, or reset).
- **Windowed Gaussian falloff**: replaced the pure Gaussian weight with a
  `(1 − (d/R)²)²` window that tapers to exactly zero at `R = 2.5 × sigma`,
  eliminating phantom deformation far from pins.
- **Coordinate-system round-tripping**: `pad_left` / `pad_top` / `orig_width` /
  `orig_height` are stored and restored so that the grid does not drift across
  re-edits on expanded layers.
- **Source-quad alignment**: the C operation now samples from the original
  content region inside the padded canvas rather than the whole padded
  bounding box, fixing the all-alpha output that occurred after the first
  Auto-Expand.
- **Accurate preview crash**: removed the GEGL-node-based accurate preview
  that failed silently due to a `Gegl.Buffer.new` GI signature mismatch.
- **`Gegl.Buffer.set` signature**: removed the invalid `rowstride` argument
  that caused a TypeError in the GI bindings.
- **Dialog deprecation**: replaced `flags=Gtk.DialogFlags.MODAL |
  Gtk.DialogFlags.DESTROY_WITH_PARENT` with `modal=True` and
  `destroy_with_parent=True`.
- **Radio-button connect**: added the missing `connect("toggled", ...)` on the
  Puppet radio button, so that Puppet-mode controls actually enable when
  selected.

### Changed

- **Default Puppet grid resolution** bumped from 4×4 to 12×12, with a dynamic
  subdiv heuristic in the Cairo preview that targets a constant triangle
  budget of 30 subdivs across the longer axis.
- **`_rbf_weight`** now uses `R = 2.5 × sigma` as the cutoff distance.
- **`_rebuild_grid_from_pins`** split into NumPy and pure-Python variants for
  graceful degradation when NumPy is unavailable.
- **`build.sh`** now auto-detects Linux, macOS, and MSYS2 MinGW environments,
  and picks the correct `.so` / `.dll` extension automatically.

### Removed

- **Accurate preview engine** (GEGL-node-based). It could not set
  `gegl:buffer-sink`'s `buffer` property because that property is exposed as
  `gpointer` and the GI bindings cannot marshal a `GeglBuffer` into it.
- **Dead code** related to the removed accurate preview: state variables,
  timer, and the corresponding toggle button.

---

## [1.0.0] — 2026-08-21

Original release by [bunnywaffle](https://github.com/bunnywaffle/gimp-mesh-warp).

### Added

- Initial release of the standalone Mesh Warp plugin.
- 15 Photoshop-style warp presets: Arc, Arc Lower, Arc Upper, Arch, Bulge,
  Shell Lower, Shell Upper, Flag, Wave, Fish, Rise, FishEye, Inflate, Squeeze,
  Twist, plus a Custom free-form mode.
- Grid editing with falloff, symmetry, lock-edges, Laplacian smoothing, and
  auto-expand layer.
- Cairo canvas preview using forward triangle rasterisation.
- Backward pixel remapping in the companion C GEGL operation
  (`custom:mesh-warp`).
- Bounding-box and required-for-output optimisation in the GEGL operation,
  including per-quad bbox caches.

---

## Version history

| Version | Date       | Notes                                          |
|---------|------------|------------------------------------------------|
| 2.0.0   | 2026-09-27 | NDE + Puppet Pins + performance and bug fixes  |
| 1.0.0   | 2026-08-21 | Original release by bunnywaffle                |