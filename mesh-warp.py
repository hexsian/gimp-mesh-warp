#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mesh Warp & Transform Presets Plugin for GIMP 3.0 / 3.2+
=========================================================
Interactive Mesh Warp with Photoshop-style presets and puppet pins.

Pixel warp is delegated to the custom:mesh-warp GEGL operation
(mesh-warp.c), applied as a non-destructive GIMP 3.2 layer effect.

Two editing modes:
  - Grid: drag grid vertices directly
  - Puppet: place displacement pins; grid is derived via Gaussian RBF

Canvas preview uses fast Cairo triangle rasterization. The final Apply
always runs the C GEGL op (backward bilinear remap), so canvas preview
and output may differ by sub-pixel amounts at edges under strong warp.

Author: Antigravity
License: GNU General Public License v3 or later
"""

import math
import sys
import json

import gi
gi.require_version('Gimp', '3.0')
gi.require_version('GimpUi', '3.0')
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gegl', '0.4')

from gi.repository import Gimp, GimpUi, Gtk, Gdk, Gegl, GLib, GObject

try:
    import cairo
except ImportError:
    from gi.repository import cairo
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    
PLUG_IN_PROC = "plug-in-mesh-warp"
PLUG_IN_BINARY = "mesh_warp"

GEGL_OP_NAME = "custom:mesh-warp"
GEGL_OP_LABEL = "Mesh Warp"

PRESETS = [
    "Custom", "Arc", "Arc Lower", "Arc Upper", "Arch",
    "Bulge", "Shell Lower", "Shell Upper", "Flag", "Wave",
    "Fish", "Rise", "FishEye", "Inflate", "Squeeze", "Twist",
]


# ==============================================================================
# Puppet pin: displacement vector (source -> destination) in ORIGINAL space
# ==============================================================================

class Pin:
    __slots__ = ('sx', 'sy', 'dx', 'dy')

    def __init__(self, sx, sy):
        self.sx = float(sx)
        self.sy = float(sy)
        self.dx = float(sx)
        self.dy = float(sy)

    def to_list(self):
        return [self.sx, self.sy, self.dx, self.dy]

    @classmethod
    def from_list(cls, lst):
        p = cls(lst[0], lst[1])
        p.dx = float(lst[2])
        p.dy = float(lst[3])
        return p


# ==============================================================================
# Preset math
# ==============================================================================

def compute_affine_matrix(s0, s1, s2, d0, d1, d2):
    u0, v0 = s0; u1, v1 = s1; u2, v2 = s2
    x0, y0 = d0; x1, y1 = d1; x2, y2 = d2
    det = (u1 - u0) * (v2 - v0) - (v1 - v0) * (u2 - u0)
    if abs(det) < 1e-9:
        return None
    inv = 1.0 / det
    a = inv * ((v2 - v0) * (x1 - x0) - (v1 - v0) * (x2 - x0))
    b = inv * ((v2 - v0) * (y1 - y0) - (v1 - v0) * (y2 - y0))
    c = inv * (-(u2 - u0) * (x1 - x0) + (u1 - u0) * (x2 - x0))
    d = inv * (-(u2 - u0) * (y1 - y0) + (u1 - u0) * (y2 - y0))
    e = x0 - a * u0 - c * v0
    f = y0 - b * u0 - d * v0
    return (a, b, c, d, e, f)


def evaluate_warp_preset(preset_name, u, v, bend=0.5,
                         h_dist=0.0, v_dist=0.0, is_vertical=False):
    if preset_name == "Custom" or (abs(bend) < 1e-5 and abs(h_dist) < 1e-5 and abs(v_dist) < 1e-5):
        return u, v
    if is_vertical:
        u, v = v, u
    xc = 2.0 * u - 1.0
    yc = 2.0 * v - 1.0
    if abs(h_dist) > 1e-5:
        yc = yc * max(0.05, 1.0 - h_dist * xc * 0.5)
    if abs(v_dist) > 1e-5:
        xc = xc * max(0.05, 1.0 - v_dist * yc * 0.5)
    u_base = (xc + 1.0) * 0.5
    v_base = (yc + 1.0) * 0.5
    b = bend

    if preset_name == "Arc":
        curve = 1.0 - xc * xc
        v_out = v_base - b * 0.5 * curve; u_out = u_base
    elif preset_name == "Arc Lower":
        curve = 1.0 - xc * xc
        v_out = v_base + b * 0.5 * v_base * curve; u_out = u_base
    elif preset_name == "Arc Upper":
        curve = 1.0 - xc * xc
        v_out = v_base - b * 0.5 * (1.0 - v_base) * curve; u_out = u_base
    elif preset_name == "Arch":
        curve = 1.0 - xc * xc
        v_out = v_base - b * 0.45 * curve; u_out = u_base
    elif preset_name == "Bulge":
        cx = 1.0 - xc * xc; cy = 1.0 - yc * yc
        v_out = 0.5 + yc * 0.5 * (1.0 + b * 0.4 * cx)
        u_out = 0.5 + xc * 0.5 * (1.0 + b * 0.25 * cy)
    elif preset_name == "Shell Lower":
        curve = 1.0 - xc * xc
        u_out = 0.5 + xc * 0.5 * (1.0 + b * 0.5 * v_base)
        v_out = v_base + b * 0.35 * v_base * curve
    elif preset_name == "Shell Upper":
        curve = 1.0 - xc * xc
        u_out = 0.5 + xc * 0.5 * (1.0 + b * 0.5 * (1.0 - v_base))
        v_out = v_base - b * 0.35 * (1.0 - v_base) * curve
    elif preset_name == "Flag":
        wave = math.sin(u_base * 2.0 * math.pi)
        v_out = v_base - b * 0.25 * wave; u_out = u_base
    elif preset_name == "Wave":
        wave = math.sin(u_base * 2.0 * math.pi)
        v_out = v_base - b * 0.3 * wave * (0.8 + 0.4 * yc)
        u_out = u_base + b * 0.05 * math.cos(u_base * 2.0 * math.pi)
    elif preset_name == "Fish":
        scale_y = (1.0 - u_base * 0.65) * 0.7 + 0.3 + b * 0.4 * math.sin(u_base * math.pi)
        v_out = 0.5 + yc * 0.5 * scale_y
        u_out = u_base - b * 0.1 * math.sin(u_base * math.pi)
    elif preset_name == "Rise":
        v_out = v_base - b * 0.45 * (u_base - 0.5) - b * 0.15 * math.sin(u_base * math.pi)
        u_out = u_base
    elif preset_name == "FishEye":
        r = math.hypot(xc, yc)
        if r > 1e-6:
            rw = r * (1.0 + b * 0.5 * (1.0 - min(1.0, r)))
            ratio = rw / r
            u_out = 0.5 + 0.5 * xc * ratio
            v_out = 0.5 + 0.5 * yc * ratio
        else:
            u_out = v_out = 0.5
    elif preset_name == "Inflate":
        factor = (1.0 - xc * xc) * (1.0 - yc * yc)
        u_out = 0.5 + xc * 0.5 * (1.0 + b * 0.4 * factor)
        v_out = 0.5 + yc * 0.5 * (1.0 + b * 0.4 * factor)
    elif preset_name == "Squeeze":
        cx = 1.0 - xc * xc; cy = 1.0 - yc * yc
        v_out = 0.5 + yc * 0.5 * (1.0 - b * 0.45 * cx)
        u_out = 0.5 + xc * 0.5 * (1.0 - b * 0.2 * cy)
    elif preset_name == "Twist":
        r = math.hypot(xc, yc)
        theta = math.atan2(yc, xc) + b * math.pi * 0.75 * (1.0 - min(1.0, r))
        u_out = 0.5 + 0.5 * r * math.cos(theta)
        v_out = 0.5 + 0.5 * r * math.sin(theta)
    else:
        u_out, v_out = u_base, v_base

    if is_vertical:
        u_out, v_out = v_out, u_out
    return u_out, v_out


def resample_mesh_grid(old_grid, old_cols, old_rows, new_cols, new_rows):
    new_grid = []
    for r in range(new_rows + 1):
        v = r / float(new_rows)
        for c in range(new_cols + 1):
            u = c / float(new_cols)
            gx = u * old_cols; gy = v * old_rows
            i0 = max(0, min(int(math.floor(gx)), old_cols - 1))
            j0 = max(0, min(int(math.floor(gy)), old_rows - 1))
            tx = max(0.0, min(1.0, gx - i0))
            ty = max(0.0, min(1.0, gy - j0))
            p00 = old_grid[j0 * (old_cols + 1) + i0]
            p10 = old_grid[j0 * (old_cols + 1) + (i0 + 1)]
            p01 = old_grid[(j0 + 1) * (old_cols + 1) + i0]
            p11 = old_grid[(j0 + 1) * (old_cols + 1) + (i0 + 1)]
            x = (1-tx)*(1-ty)*p00[0] + tx*(1-ty)*p10[0] + (1-tx)*ty*p01[0] + tx*ty*p11[0]
            y = (1-tx)*(1-ty)*p00[1] + tx*(1-ty)*p10[1] + (1-tx)*ty*p01[1] + tx*ty*p11[1]
            new_grid.append([x, y])
    return new_grid


def laplacian_smooth_grid(grid, cols, rows, factor=0.4, lock_boundary=False):
    new_grid = [list(p) for p in grid]
    for r in range(rows + 1):
        for c in range(cols + 1):
            idx = r * (cols + 1) + c
            boundary = (r == 0 or r == rows or c == 0 or c == cols)
            if boundary and lock_boundary:
                continue
            nb = []
            if c > 0: nb.append(grid[r * (cols + 1) + (c - 1)])
            if c < cols: nb.append(grid[r * (cols + 1) + (c + 1)])
            if r > 0: nb.append(grid[(r - 1) * (cols + 1) + c])
            if r < rows: nb.append(grid[(r + 1) * (cols + 1) + c])
            if nb:
                ax = sum(p[0] for p in nb) / len(nb)
                ay = sum(p[1] for p in nb) / len(nb)
                new_grid[idx][0] = (1.0 - factor) * grid[idx][0] + factor * ax
                new_grid[idx][1] = (1.0 - factor) * grid[idx][1] + factor * ay
    return new_grid


# ==============================================================================
# Interactive Mesh Warp Dialog & Canvas
# ==============================================================================

class MeshWarpDialog(Gtk.Dialog):
    # Editing modes
    MODE_GRID   = 0
    MODE_PUPPET = 1

    def __init__(self, image, drawable):
        super().__init__(
            title="Interactive Mesh Warp & Presets",
            modal=True,
            destroy_with_parent=True
        )
        self.set_default_size(1100, 820)
        self.set_position(Gtk.WindowPosition.CENTER)

        self.image = image
        self.drawable = drawable

        # ------------------------------------------------------------------
        # Load existing filter state if present
        # ------------------------------------------------------------------
        self.existing_mesh_warp_filter = self.find_existing_mesh_warp_filter()
        loaded_state = None
        if self.existing_mesh_warp_filter is not None:
            loaded_state = self.try_load_filter_state(self.existing_mesh_warp_filter)
            if loaded_state is None:
                self.existing_mesh_warp_filter = None

        # ------------------------------------------------------------------
        # Coordinate systems
        # ------------------------------------------------------------------
        canvas_w = drawable.get_width()
        canvas_h = drawable.get_height()

        if loaded_state is not None:
            self.orig_width  = loaded_state['orig_width']
            self.orig_height = loaded_state['orig_height']
            self.pad_left    = loaded_state['pad_left']
            self.pad_top     = loaded_state['pad_top']
        else:
            self.orig_width  = canvas_w
            self.orig_height = canvas_h
            self.pad_left    = 0
            self.pad_top     = 0

        self.width  = self.orig_width
        self.height = self.orig_height
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h

        # ------------------------------------------------------------------
        # Extract source pixels (unfiltered snapshot for preview)
        # ------------------------------------------------------------------
        rect = Gegl.Rectangle.new(0, 0, canvas_w, canvas_h)
        buffer = drawable.get_buffer()
        self.raw_src_pixels = bytes(
            buffer.get(rect, 1.0, "cairo-ARGB32", Gegl.AbyssPolicy.NONE)
        )
        self.src_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, canvas_w, canvas_h)
        self.src_surface.get_data()[:] = self.raw_src_pixels
        self.src_surface.mark_dirty()

        # Reusable Cairo pattern for fast preview
        self._src_pattern = cairo.SurfacePattern(self.src_surface)
        self._src_pattern.set_filter(cairo.FILTER_BILINEAR)
        self._src_pattern.set_extend(cairo.EXTEND_PAD)

        # ------------------------------------------------------------------
        # Mesh state
        # ------------------------------------------------------------------
        self.cols = loaded_state['cols'] if loaded_state else 4
        self.rows = loaded_state['rows'] if loaded_state else 4
        self.orig_grid = []
        self.grid = []
        self.init_mesh()
        if loaded_state is not None:
            self.grid = loaded_state['grid']

        # ------------------------------------------------------------------
        # Puppet pin state
        # ------------------------------------------------------------------
        self.mode = loaded_state.get('mode', self.MODE_GRID) if loaded_state else self.MODE_GRID
        self.pins = []
        if loaded_state is not None and 'pins' in loaded_state:
            self.pins = [Pin.from_list(l) for l in loaded_state['pins']]

        self.puppet_radius = 150.0
        self.selected_pins = set()
        self.active_pin_index = None
        self.hovered_pin_index = None
        self.drag_start_pins = []

        # ------------------------------------------------------------------
        # Preset state
        # ------------------------------------------------------------------
        self.current_preset = "Custom"
        self.bend_val = 0.5
        self.h_dist_val = 0.0
        self.v_dist_val = 0.0
        self.is_vertical_preset = False
        self.updating_ui_preset = False
        self._preset_timer = None

        # ------------------------------------------------------------------
        # Viewport
        # ------------------------------------------------------------------
        self.pan_x = 25.0
        self.pan_y = 25.0
        self.zoom = 1.0
        self.pan_start_x = 0.0
        self.pan_start_y = 0.0
        self.is_panning = False
        self.space_pressed = False

        # ------------------------------------------------------------------
        # Selection & drag (grid mode)
        # ------------------------------------------------------------------
        self.selected_indices = set()
        self.active_index = None
        self.hovered_index = None
        self.is_dragging_points = False
        self.is_dragging_pins = False
        self.drag_mouse_start = (0.0, 0.0)
        self.drag_start_grid = []

        # Marquee box
        self.is_box_selecting = False
        self.box_start = (0.0, 0.0)
        self.box_current = (0.0, 0.0)

        # ------------------------------------------------------------------
        # Options
        # ------------------------------------------------------------------
        self.lock_boundary = False
        self.use_falloff = False
        self.falloff_radius = 120.0
        self.expand_layer = True
        self.symmetry_mode = 0
        self.show_grid = True
        self.show_ghost = False
        self.show_checker = True
        self.subdiv_level = 6

        # ------------------------------------------------------------------
        # Spatial index
        # ------------------------------------------------------------------
        self._spatial = None
        self._spatial_cell = 40.0
        self._spatial_key = None

        # ------------------------------------------------------------------
        # Build
        # ------------------------------------------------------------------
        self.build_ui()
        self.fit_to_view()

        # If loaded in puppet mode, recompute grid from pins immediately
        if self.mode == self.MODE_PUPPET and self.pins:
            self._rebuild_grid_from_pins()

    # ==================================================================
    # Mesh initialization
    # ==================================================================

    def init_mesh(self):
        self.orig_grid = []
        self.grid = []
        for r in range(self.rows + 1):
            y = (r / float(self.rows)) * self.height
            for c in range(self.cols + 1):
                x = (c / float(self.cols)) * self.width
                self.orig_grid.append([x, y])
                self.grid.append([x, y])

    def _rebuild_orig_grid(self):
        self.orig_grid = []
        for r in range(self.rows + 1):
            y = (r / float(self.rows)) * self.height
            for c in range(self.cols + 1):
                x = (c / float(self.cols)) * self.width
                self.orig_grid.append([x, y])

    # ==================================================================
    # Filter round-trip
    # ==================================================================

    def find_existing_mesh_warp_filter(self):
        try:
            filters = self.drawable.get_filters()
        except Exception:
            return None
        for filt in filters or []:
            try:
                if filt.get_operation_name() == GEGL_OP_NAME:
                    return filt
            except Exception:
                continue
        return None

    def try_load_filter_state(self, filt):
        try:
            econfig = filt.get_config()
            loaded_cols = int(econfig.get_property("cols"))
            loaded_rows = int(econfig.get_property("rows"))
            loaded_json = econfig.get_property("grid-json")

            try:
                pad_left    = int(econfig.get_property("pad-left"))
                pad_top     = int(econfig.get_property("pad-top"))
                orig_width  = int(econfig.get_property("orig-width"))
                orig_height = int(econfig.get_property("orig-height"))
            except Exception:
                pad_left = pad_top = 0
                orig_width = self.drawable.get_width()
                orig_height = self.drawable.get_height()

            try:
                loaded_pins_json = econfig.get_property("pins-json") or ""
                loaded_mode      = econfig.get_property("mode") or "grid"
            except Exception:
                loaded_pins_json = ""
                loaded_mode = "grid"

            padded_grid = [list(p) for p in json.loads(loaded_json)]
            expected_n = (loaded_cols + 1) * (loaded_rows + 1)
            if len(padded_grid) != expected_n:
                Gimp.message("Mesh Warp: grid size mismatch, starting fresh.")
                return None

            grid = [[x - pad_left, y - pad_top] for x, y in padded_grid]

            pins = []
            if loaded_pins_json:
                try:
                    raw = json.loads(loaded_pins_json)
                    pins = [list(p) for p in raw]
                except Exception:
                    pins = []

            mode = self.MODE_PUPPET if loaded_mode == "puppet" else self.MODE_GRID

            return {
                'cols': loaded_cols,
                'rows': loaded_rows,
                'grid': grid,
                'pad_left': pad_left,
                'pad_top': pad_top,
                'orig_width': orig_width,
                'orig_height': orig_height,
                'pins': pins,
                'mode': mode,
            }
        except Exception as e:
            Gimp.message(f"Mesh Warp: could not restore state ({e})")
            return None

    # ==================================================================
    # UI construction
    # ==================================================================

    def build_ui(self):
        content_area = self.get_content_area()
        content_area.set_spacing(6)
        content_area.set_border_width(8)

        # ---------------- Row 1: Mode + Presets ----------------
        preset_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        preset_bar.pack_start(Gtk.Label(label="<b>Mode:</b>", use_markup=True), False, False, 0)

        self.mode_grid_btn = Gtk.RadioButton.new_with_label(None, "Grid")
        self.mode_puppet_btn = Gtk.RadioButton.new_with_label_from_widget(self.mode_grid_btn, "Puppet")
        self.mode_grid_btn.set_active(self.mode == self.MODE_GRID)
        self.mode_puppet_btn.set_active(self.mode == self.MODE_PUPPET)
        self.mode_grid_btn.connect("toggled", self.on_mode_toggled)
        self.mode_puppet_btn.connect("toggled", self.on_mode_toggled)
        preset_bar.pack_start(self.mode_grid_btn, False, False, 0)
        preset_bar.pack_start(self.mode_puppet_btn, False, False, 0)

        preset_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        preset_bar.pack_start(Gtk.Label(label="<b>Preset:</b>", use_markup=True), False, False, 0)
        self.preset_combo = Gtk.ComboBoxText()
        for p in PRESETS:
            self.preset_combo.append_text(p)
        self.preset_combo.set_active(0)
        self.preset_combo.connect("changed", self.on_preset_changed)
        preset_bar.pack_start(self.preset_combo, False, False, 0)

        self.orient_toggle = Gtk.ToggleButton(label="Vertical")
        self.orient_toggle.set_tooltip_text("Toggle between Horizontal and Vertical Warp Orientation")
        self.orient_toggle.connect("toggled", self.on_orient_toggled)
        preset_bar.pack_start(self.orient_toggle, False, False, 0)

        preset_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        preset_bar.pack_start(Gtk.Label(label="Bend:"), False, False, 0)
        self.bend_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.bend_scale.set_value(50)
        self.bend_scale.set_size_request(100, -1)
        self.bend_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.bend_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.bend_scale, False, False, 0)

        preset_bar.pack_start(Gtk.Label(label="H-D:"), False, False, 0)
        self.h_dist_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.h_dist_scale.set_value(0)
        self.h_dist_scale.set_size_request(85, -1)
        self.h_dist_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.h_dist_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.h_dist_scale, False, False, 0)

        preset_bar.pack_start(Gtk.Label(label="V-D:"), False, False, 0)
        self.v_dist_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.v_dist_scale.set_value(0)
        self.v_dist_scale.set_size_request(85, -1)
        self.v_dist_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.v_dist_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.v_dist_scale, False, False, 0)

        content_area.pack_start(preset_bar, False, False, 0)

        # ---------------- Row 2: Grid controls ----------------
        grid_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        grid_bar.pack_start(Gtk.Label(label="Cols:"), False, False, 0)
        self.cols_spin = Gtk.SpinButton.new_with_range(2, 24, 1)
        self.cols_spin.set_value(self.cols)
        self.cols_spin.connect("value-changed", self.on_cols_changed)
        grid_bar.pack_start(self.cols_spin, False, False, 0)

        grid_bar.pack_start(Gtk.Label(label="Rows:"), False, False, 0)
        self.rows_spin = Gtk.SpinButton.new_with_range(2, 24, 1)
        self.rows_spin.set_value(self.rows)
        self.rows_spin.connect("value-changed", self.on_rows_changed)
        grid_bar.pack_start(self.rows_spin, False, False, 0)

        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        # Puppet radius — only meaningful in puppet mode
        self.puppet_label = Gtk.Label(label="Pin Radius:")
        grid_bar.pack_start(self.puppet_label, False, False, 0)
        self.puppet_radius_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 20, 600, 5)
        self.puppet_radius_scale.set_value(self.puppet_radius)
        self.puppet_radius_scale.set_size_request(140, -1)
        self.puppet_radius_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.puppet_radius_scale.connect("value-changed", self.on_puppet_radius_changed)
        grid_bar.pack_start(self.puppet_radius_scale, False, False, 0)

        self.puppet_clear_btn = Gtk.Button(label="Clear Pins")
        self.puppet_clear_btn.set_tooltip_text("Remove all puppet pins")
        self.puppet_clear_btn.connect("clicked", self.on_clear_pins_clicked)
        grid_bar.pack_start(self.puppet_clear_btn, False, False, 0)

        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        self.lock_check = Gtk.CheckButton(label="Lock Edges")
        self.lock_check.set_active(self.lock_boundary)
        self.lock_check.connect("toggled", self.on_lock_boundary_toggled)
        grid_bar.pack_start(self.lock_check, False, False, 0)

        self.falloff_check = Gtk.CheckButton(label="Falloff")
        self.falloff_check.set_active(self.use_falloff)
        self.falloff_check.connect("toggled", self.on_falloff_toggled)
        grid_bar.pack_start(self.falloff_check, False, False, 0)

        grid_bar.pack_start(Gtk.Label(label="Rad:"), False, False, 0)
        self.radius_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 20, 600, 10)
        self.radius_scale.set_value(self.falloff_radius)
        self.radius_scale.set_size_request(85, -1)
        self.radius_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.radius_scale.connect("value-changed", self.on_radius_changed)
        grid_bar.pack_start(self.radius_scale, False, False, 0)

        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        grid_bar.pack_start(Gtk.Label(label="Sym:"), False, False, 0)
        self.sym_combo = Gtk.ComboBoxText()
        self.sym_combo.append_text("None")
        self.sym_combo.append_text("Horizontal (L/R)")
        self.sym_combo.append_text("Vertical (T/B)")
        self.sym_combo.set_active(0)
        self.sym_combo.connect("changed", self.on_symmetry_changed)
        grid_bar.pack_start(self.sym_combo, False, False, 0)

        self.expand_check = Gtk.CheckButton(label="Auto-Expand")
        self.expand_check.set_active(self.expand_layer)
        self.expand_check.set_tooltip_text("Resize layer canvas so warped outer areas are preserved")
        self.expand_check.connect("toggled", lambda b: setattr(self, 'expand_layer', b.get_active()))
        grid_bar.pack_start(self.expand_check, False, False, 0)

        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        relax_btn = Gtk.Button(label="Smooth")
        relax_btn.set_tooltip_text("Apply Laplacian smoothing to eliminate sharp folds")
        relax_btn.connect("clicked", self.on_smooth_clicked)
        grid_bar.pack_start(relax_btn, False, False, 0)

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.set_tooltip_text("Reset all grid vertices and pins")
        reset_btn.connect("clicked", self.on_reset_clicked)
        grid_bar.pack_start(reset_btn, False, False, 0)

        content_area.pack_start(grid_bar, False, False, 0)

        # ---------------- Central canvas ----------------
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_can_focus(True)
        self.drawing_area.set_size_request(680, 500)
        self.drawing_area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.SCROLL_MASK |
            Gdk.EventMask.KEY_PRESS_MASK |
            Gdk.EventMask.KEY_RELEASE_MASK |
            Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.drawing_area.connect("draw", self.on_draw)
        self.drawing_area.connect("button-press-event", self.on_button_press)
        self.drawing_area.connect("button-release-event", self.on_button_release)
        self.drawing_area.connect("motion-notify-event", self.on_motion_notify)
        self.drawing_area.connect("scroll-event", self.on_scroll)
        self.drawing_area.connect("key-press-event", self.on_key_press)
        self.drawing_area.connect("key-release-event", self.on_key_release)
        self.drawing_area.connect("leave-notify-event", self.on_leave_notify)

        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(self.drawing_area)
        content_area.pack_start(frame, True, True, 0)

        # ---------------- Bottom bar ----------------
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.grid_toggle = Gtk.ToggleButton(label="Show Grid (H)")
        self.grid_toggle.set_active(True)
        self.grid_toggle.connect("toggled", self.on_toggle_grid)
        bottom_box.pack_start(self.grid_toggle, False, False, 0)

        self.ghost_toggle = Gtk.ToggleButton(label="Ghost Original")
        self.ghost_toggle.set_active(False)
        self.ghost_toggle.connect("toggled", self.on_toggle_ghost)
        bottom_box.pack_start(self.ghost_toggle, False, False, 0)

        fit_btn = Gtk.Button(label="Fit")
        fit_btn.connect("clicked", lambda b: self.fit_to_view())
        bottom_box.pack_start(fit_btn, False, False, 0)

        zoom_100_btn = Gtk.Button(label="100%")
        zoom_100_btn.connect("clicked", lambda b: self.set_zoom(1.0))
        bottom_box.pack_start(zoom_100_btn, False, False, 0)

        self.zoom_label = Gtk.Label(label="100%")
        bottom_box.pack_start(self.zoom_label, False, False, 0)

        bottom_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        self.status_label = Gtk.Label(
            label="Grid: drag pins. Puppet: click empty to add pin, drag to move, right-click to delete."
        )
        self.status_label.set_xalign(0.0)
        bottom_box.pack_start(self.status_label, True, True, 0)

        content_area.pack_start(bottom_box, False, False, 0)

        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_OK", Gtk.ResponseType.OK)

        self.show_all()
        self._update_mode_ui()

    def _update_mode_ui(self):
        """Enable/disable controls based on current mode."""
        puppet = (self.mode == self.MODE_PUPPET)
        self.puppet_label.set_sensitive(puppet)
        self.puppet_radius_scale.set_sensitive(puppet)
        self.puppet_clear_btn.set_sensitive(puppet)
        self.preset_combo.set_sensitive(not puppet)
        self.orient_toggle.set_sensitive(not puppet)
        self.bend_scale.set_sensitive(not puppet)
        self.h_dist_scale.set_sensitive(not puppet)
        self.v_dist_scale.set_sensitive(not puppet)

    # ==================================================================
    # Mode / preset handlers
    # ==================================================================

    def on_mode_toggled(self, btn):
        if not btn.get_active():
            return
        new_mode = self.MODE_PUPPET if btn is self.mode_puppet_btn else self.MODE_GRID
        if new_mode == self.mode:
            return

        if new_mode == self.MODE_PUPPET:
            # Auto-bump grid resolution: RBF needs dense sampling to
            # localize warp. 4x4 grid is far too coarse for puppet.
            if self.cols < 12 or self.rows < 12:
                new_cols = max(self.cols, 12)
                new_rows = max(self.rows, 12)
                self.grid = resample_mesh_grid(
                    self.grid, self.cols, self.rows, new_cols, new_rows)
                self.cols = new_cols
                self.rows = new_rows
                self._rebuild_orig_grid()
                self.cols_spin.set_value(new_cols)
                self.rows_spin.set_value(new_rows)
            # Preserve pins across mode switches — only grid edits clear them.
            if self.pins:
                self._rebuild_grid_from_pins()
        # Note: do NOT clear self.pins here — pin state persists across
        # mode switches, and gets cleared only when the grid is edited.

        self.mode = new_mode
        self.active_index = None
        self.active_pin_index = None
        self.hovered_index = None
        self.hovered_pin_index = None
        self._update_mode_ui()
        self._invalidate_spatial()
        self.drawing_area.queue_draw()

    def on_preset_changed(self, combo):
        if self.updating_ui_preset or self.mode == self.MODE_PUPPET:
            return
        self.current_preset = combo.get_active_text()
        if self.current_preset != "Custom":
            self.apply_current_preset_to_grid()

    def on_orient_toggled(self, btn):
        if self.mode == self.MODE_PUPPET:
            return
        self.is_vertical_preset = btn.get_active()
        if self.current_preset != "Custom":
            self.apply_current_preset_to_grid()

    def on_preset_param_changed(self, scale):
        self.bend_val = self.bend_scale.get_value() / 100.0
        self.h_dist_val = self.h_dist_scale.get_value() / 100.0
        self.v_dist_val = self.v_dist_scale.get_value() / 100.0
        if self.current_preset == "Custom" or self.mode == self.MODE_PUPPET:
            return
        if self._preset_timer is not None:
            GLib.source_remove(self._preset_timer)
        self._preset_timer = GLib.timeout_add(30, self._apply_preset_deferred)

    def _apply_preset_deferred(self):
        self._preset_timer = None
        self.apply_current_preset_to_grid()
        return False

    def set_preset_to_custom(self):
        if self.current_preset != "Custom":
            self.updating_ui_preset = True
            self.current_preset = "Custom"
            self.preset_combo.set_active(0)
            self.updating_ui_preset = False

    def apply_current_preset_to_grid(self):
        if self.current_preset == "Custom":
            return
        if self.pins:
            self.pins = []
            self.selected_pins.clear()
        bend = self.bend_val
        h_dist = self.h_dist_val
        v_dist = self.v_dist_val
        is_vert = self.is_vertical_preset
        for r in range(self.rows + 1):
            v_norm = r / float(self.rows)
            for c in range(self.cols + 1):
                u_norm = c / float(self.cols)
                u_w, v_w = evaluate_warp_preset(
                    self.current_preset, u_norm, v_norm,
                    bend=bend, h_dist=h_dist, v_dist=v_dist, is_vertical=is_vert
                )
                idx = r * (self.cols + 1) + c
                self.grid[idx][0] = u_w * self.width
                self.grid[idx][1] = v_w * self.height
        self._invalidate_spatial()
        self.drawing_area.queue_draw()

    # ==================================================================
    # Puppet RBF engine
    # ==================================================================

    def _rbf_weight(self, dist):
        """Windowed Gaussian RBF: smooth Gaussian core, tapered to exactly
        zero at R = 2.5*sigma via a (1 - (d/R)^2)^2 window. This kills the
        long-tail phantom deformation of a pure Gaussian, so vertices
        far from every pin snap cleanly to their original positions."""
        sigma = self.puppet_radius
        if sigma < 1e-3:
            return 1.0 if dist < 1e-3 else 0.0
        R = sigma * 2.5
        if dist >= R:
            return 0.0
        g = math.exp(-(dist * dist) / (2.0 * sigma * sigma))
        t = dist / R
        return g * (1.0 - t * t) ** 2

    def _rebuild_grid_from_pins(self):
        """Derive self.grid from pins. Uses numpy if available for large
        grids (10-30x faster), else pure Python fallback."""
        if not self.pins:
            for i, (ox, oy) in enumerate(self.orig_grid):
                self.grid[i][0] = ox
                self.grid[i][1] = oy
            self._invalidate_spatial()
            self.drawing_area.queue_draw()
            return

        if HAS_NUMPY:
            self._rebuild_grid_from_pins_numpy()
        else:
            self._rebuild_grid_from_pins_pure()

    def _rebuild_grid_from_pins_numpy(self):
        """Vectorized rebuild via numpy. Computes the full (N_vertex, N_pin)
        weight matrix in one shot and lets numpy handle the inner loops."""
        n_v = len(self.orig_grid)
        n_p = len(self.pins)

        # Vertex positions as flat arrays
        ox = np.fromiter((p[0] for p in self.orig_grid), dtype=np.float64, count=n_v)
        oy = np.fromiter((p[1] for p in self.orig_grid), dtype=np.float64, count=n_v)

        # Pin source positions + deltas
        sx  = np.fromiter((p.sx for p in self.pins), dtype=np.float64, count=n_p)
        sy  = np.fromiter((p.sy for p in self.pins), dtype=np.float64, count=n_p)
        ddx = np.fromiter((p.dx - p.sx for p in self.pins), dtype=np.float64, count=n_p)
        ddy = np.fromiter((p.dy - p.sy for p in self.pins), dtype=np.float64, count=n_p)

        sigma = self.puppet_radius
        R = sigma * 2.5
        R2 = R * R

        # (n_v, n_p) pairwise distances squared
        dx = ox[:, None] - sx[None, :]
        dy = oy[:, None] - sy[None, :]
        d2 = dx * dx + dy * dy

        # Weight matrix: windowed Gaussian, zero beyond R
        mask = d2 < R2
        safe_d2 = np.where(mask, d2, 1.0)
        d = np.sqrt(safe_d2)
        t = d / R
        w = np.exp(-safe_d2 / (2.0 * sigma * sigma)) * (1.0 - t * t) ** 2
        w = np.where(mask, w, 0.0)

        # Weighted sums per vertex
        sum_w  = w.sum(axis=1)                     # (n_v,)
        sum_dx = (w * ddx[None, :]).sum(axis=1)
        sum_dy = (w * ddy[None, :]).sum(axis=1)

        # Combine
        denom = np.maximum(sum_w, 1.0)
        new_x = ox + sum_dx / denom
        new_y = oy + sum_dy / denom

        # Vertices with no influence snap to original
        no_inf = sum_w < 1e-9
        new_x = np.where(no_inf, ox, new_x)
        new_y = np.where(no_inf, oy, new_y)

        # Write back to self.grid (list-of-list)
        for i in range(n_v):
            self.grid[i][0] = float(new_x[i])
            self.grid[i][1] = float(new_y[i])

        self._invalidate_spatial()
        self.drawing_area.queue_draw()

    def _rebuild_grid_from_pins_pure(self):
        """Pure-Python fallback for environments without numpy."""
        deltas = [(p.dx - p.sx, p.dy - p.sy) for p in self.pins]
        pin_pts = [(p.sx, p.sy) for p in self.pins]

        influence_r = self.puppet_radius * 2.5
        influence_r2 = influence_r * influence_r

        for i, (ox, oy) in enumerate(self.orig_grid):
            sum_w = 0.0
            sum_dx = 0.0
            sum_dy = 0.0
            for (sx, sy), (ddx, ddy) in zip(pin_pts, deltas):
                dx = ox - sx
                dy = oy - sy
                d2 = dx * dx + dy * dy
                if d2 >= influence_r2:
                    continue
                d = math.sqrt(d2)
                w = self._rbf_weight(d)
                sum_w  += w
                sum_dx += w * ddx
                sum_dy += w * ddy

            if sum_w > 1e-9:
                denom = max(sum_w, 1.0)
                self.grid[i][0] = ox + sum_dx / denom
                self.grid[i][1] = oy + sum_dy / denom
            else:
                self.grid[i][0] = ox
                self.grid[i][1] = oy

        self._invalidate_spatial()
        self.drawing_area.queue_draw()

    def on_puppet_radius_changed(self, scale):
        self.puppet_radius = scale.get_value()
        if self.mode == self.MODE_PUPPET and self.pins:
            self._rebuild_grid_from_pins()
        else:
            self.drawing_area.queue_draw()

    def on_clear_pins_clicked(self, btn):
        self.pins = []
        self.selected_pins.clear()
        if self.mode == self.MODE_PUPPET:
            self._rebuild_grid_from_pins()
        else:
            self.drawing_area.queue_draw()

    # ==================================================================
    # Grid handlers
    # ==================================================================

    def on_cols_changed(self, spin):
        new_cols = int(spin.get_value())
        if new_cols != self.cols:
            self.grid = resample_mesh_grid(self.grid, self.cols, self.rows, new_cols, self.rows)
            self.cols = new_cols
            self._rebuild_orig_grid()
            self.selected_indices.clear()
            self._invalidate_spatial()
            if self.mode == self.MODE_PUPPET and self.pins:
                self._rebuild_grid_from_pins()
            else:
                self.drawing_area.queue_draw()

    def on_rows_changed(self, spin):
        new_rows = int(spin.get_value())
        if new_rows != self.rows:
            self.grid = resample_mesh_grid(self.grid, self.cols, self.rows, self.cols, new_rows)
            self.rows = new_rows
            self._rebuild_orig_grid()
            self.selected_indices.clear()
            self._invalidate_spatial()
            if self.mode == self.MODE_PUPPET and self.pins:
                self._rebuild_grid_from_pins()
            else:
                self.drawing_area.queue_draw()

    def on_lock_boundary_toggled(self, btn):
        self.lock_boundary = btn.get_active()
        self.drawing_area.queue_draw()

    def on_falloff_toggled(self, btn):
        self.use_falloff = btn.get_active()
        self.drawing_area.queue_draw()

    def on_radius_changed(self, scale):
        self.falloff_radius = scale.get_value()
        self.drawing_area.queue_draw()

    def on_symmetry_changed(self, combo):
        self.symmetry_mode = combo.get_active()
        self.drawing_area.queue_draw()

    def on_toggle_grid(self, btn):
        self.show_grid = btn.get_active()
        self.drawing_area.queue_draw()

    def on_toggle_ghost(self, btn):
        self.show_ghost = btn.get_active()
        self.drawing_area.queue_draw()

    def on_smooth_clicked(self, btn):
        if self.mode == self.MODE_PUPPET:
            return
        self.set_preset_to_custom()
        if self.pins:
            self.pins = []
            self.selected_pins.clear()
        self.grid = laplacian_smooth_grid(
            self.grid, self.cols, self.rows, factor=0.4, lock_boundary=self.lock_boundary
        )
        self._invalidate_spatial()
        self.drawing_area.queue_draw()
        self.grid = laplacian_smooth_grid(
            self.grid, self.cols, self.rows, factor=0.4, lock_boundary=self.lock_boundary
        )
        self._invalidate_spatial()
        self.drawing_area.queue_draw()

    def on_reset_clicked(self, btn):
        self.pins = []
        self.selected_pins.clear()
        if self.mode == self.MODE_PUPPET:
            self._rebuild_grid_from_pins()
        else:
            self.set_preset_to_custom()
            self.grid = [list(p) for p in self.orig_grid]
            self.selected_indices.clear()
            self._invalidate_spatial()
            self.drawing_area.queue_draw()

    # ==================================================================
    # Viewport geometry
    # ==================================================================

    def screen_to_world(self, sx, sy):
        return ((sx - self.pan_x) / self.zoom,
                (sy - self.pan_y) / self.zoom)

    def world_to_screen(self, wx, wy):
        return (wx * self.zoom + self.pan_x,
                wy * self.zoom + self.pan_y)

    def fit_to_view(self):
        alloc = self.drawing_area.get_allocation()
        dw = alloc.width if alloc.width > 50 else 680
        dh = alloc.height if alloc.height > 50 else 500
        margin = 40.0
        avail_w = max(50.0, dw - margin * 2)
        avail_h = max(50.0, dh - margin * 2)
        scale_x = avail_w / float(self.canvas_w)
        scale_y = avail_h / float(self.canvas_h)
        self.zoom = min(scale_x, scale_y, 2.0)
        self.pan_x = (dw - self.canvas_w * self.zoom) / 2.0
        self.pan_y = (dh - self.canvas_h * self.zoom) / 2.0
        self._invalidate_spatial()
        if hasattr(self, 'zoom_label'):
            self.zoom_label.set_text(f"{int(round(self.zoom * 100))}%")
        self.drawing_area.queue_draw()

    def set_zoom(self, new_zoom, center_sx=None, center_sy=None):
        new_zoom = max(0.05, min(10.0, new_zoom))
        if center_sx is None or center_sy is None:
            alloc = self.drawing_area.get_allocation()
            center_sx = alloc.width / 2.0
            center_sy = alloc.height / 2.0
        self.pan_x = center_sx - (center_sx - self.pan_x) * (new_zoom / self.zoom)
        self.pan_y = center_sy - (center_sy - self.pan_y) * (new_zoom / self.zoom)
        self.zoom = new_zoom
        self._invalidate_spatial()
        self.zoom_label.set_text(f"{int(round(self.zoom * 100))}%")
        self.drawing_area.queue_draw()

    # ==================================================================
    # Spatial index
    # ==================================================================

    def _invalidate_spatial(self):
        self._spatial = None
        self._spatial_key = None

    def _ensure_spatial_index(self):
        key = (self.zoom, self.pan_x, self.pan_y,
               self.cols, self.rows, id(self.grid))
        if self._spatial is not None and self._spatial_key == key:
            return
        cell = self._spatial_cell
        buckets = {}
        for i, (wx, wy) in enumerate(self.grid):
            sx, sy = self.world_to_screen(wx, wy)
            k = (int(sx // cell), int(sy // cell))
            buckets.setdefault(k, []).append(i)
        self._spatial = buckets
        self._spatial_key = key

    # ==================================================================
    # Hit testing
    # ==================================================================

    def find_vertex_at_screen(self, sx, sy, hit_radius=10.0):
        if self.mode != self.MODE_GRID:
            return None
        self._ensure_spatial_index()
        cell = self._spatial_cell
        cx, cy = int(sx // cell), int(sy // cell)
        reach = int(math.ceil(hit_radius / cell))
        best = None
        best_dsq = hit_radius * hit_radius
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for i in self._spatial.get((cx + dx, cy + dy), ()):
                    wx, wy = self.grid[i]
                    px, py = self.world_to_screen(wx, wy)
                    dsq = (sx - px) ** 2 + (sy - py) ** 2
                    if dsq <= best_dsq:
                        best_dsq = dsq
                        best = i
        return best

    def find_pin_at_screen(self, sx, sy, hit_radius=14.0):
        """Find nearest puppet pin (dst position) within hit radius."""
        if self.mode != self.MODE_PUPPET:
            return None
        best = None
        best_dsq = hit_radius * hit_radius
        for i, pin in enumerate(self.pins):
            px, py = self.world_to_screen(pin.dx, pin.dy)
            dsq = (sx - px) ** 2 + (sy - py) ** 2
            if dsq <= best_dsq:
                best_dsq = dsq
                best = i
        return best

    def is_boundary_vertex(self, idx):
        c = idx % (self.cols + 1)
        r = idx // (self.cols + 1)
        return (r == 0 or r == self.rows or c == 0 or c == self.cols)

    def get_mirror_vertex_index(self, idx):
        c = idx % (self.cols + 1)
        r = idx // (self.cols + 1)
        if self.symmetry_mode == 1:
            return r * (self.cols + 1) + (self.cols - c)
        elif self.symmetry_mode == 2:
            return (self.rows - r) * (self.cols + 1) + c
        return None

    # ==================================================================
    # Mouse handlers
    # ==================================================================

    def on_button_press(self, widget, event):
        self.drawing_area.grab_focus()

        if event.button == 2 or (event.button == 1 and self.space_pressed):
            self.is_panning = True
            self.pan_start_x = event.x - self.pan_x
            self.pan_start_y = event.y - self.pan_y
            return True

        if self.mode == self.MODE_PUPPET:
            return self._on_puppet_press(event)
        return self._on_grid_press(event)

    def _on_puppet_press(self, event):
        if event.button == 3:
            hit = self.find_pin_at_screen(event.x, event.y)
            if hit is not None:
                del self.pins[hit]
                self.active_pin_index = None
                self.hovered_pin_index = None
                self._rebuild_grid_from_pins()
            return True

        if event.button == 1:
            hit = self.find_pin_at_screen(event.x, event.y, hit_radius=14.0)
            is_shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

            if hit is None:
                wx, wy = self.screen_to_world(event.x, event.y)
                margin = 200.0
                if (wx < -margin or wx > self.width + margin or
                        wy < -margin or wy > self.height + margin):
                    return True
                new_pin = Pin(wx, wy)
                self.pins.append(new_pin)
                self.active_pin_index = len(self.pins) - 1
                if not is_shift:
                    self.is_dragging_pins = True
                    self.drag_start_pins = [Pin.from_list(p.to_list()) for p in self.pins]
                    self.drawing_area.queue_draw()
                return True
            else:
                self.active_pin_index = hit
                self.hovered_pin_index = hit
                self.is_dragging_pins = True
                self.drag_start_pins = [Pin.from_list(p.to_list()) for p in self.pins]
                self.drawing_area.queue_draw()
                return True

        return False

    def _on_grid_press(self, event):
        if event.button == 3:
            hit = self.find_vertex_at_screen(event.x, event.y, hit_radius=12.0)
            if hit is not None:
                self.set_preset_to_custom()
                self.grid[hit] = list(self.orig_grid[hit])
                mirror = self.get_mirror_vertex_index(hit)
                if mirror is not None and mirror < len(self.grid):
                    self.grid[mirror] = list(self.orig_grid[mirror])
                self._invalidate_spatial()
                self.drawing_area.queue_draw()
            return True

        if event.button == 1:
            hit = self.find_vertex_at_screen(event.x, event.y, hit_radius=10.0)
            is_shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

            if hit is not None:
                if self.lock_boundary and self.is_boundary_vertex(hit):
                    return True
                # Manual grid edits invalidate puppet pins, because the
                # pins were computed against the previous grid state.
                if self.pins:
                    self.pins = []
                    self.selected_pins.clear()
                    self.hovered_pin_index = None
                self.set_preset_to_custom()
                if is_shift:
                    if hit in self.selected_indices:
                        self.selected_indices.remove(hit)
                    else:
                        self.selected_indices.add(hit)
                else:
                    if hit not in self.selected_indices:
                        self.selected_indices = {hit}
                self.active_index = hit
                self.is_dragging_points = True
                wx, wy = self.screen_to_world(event.x, event.y)
                self.drag_mouse_start = (wx, wy)
                self.drag_start_grid = [list(p) for p in self.grid]
                self.drawing_area.queue_draw()
                return True
            else:
                if not is_shift:
                    self.selected_indices.clear()
                self.is_box_selecting = True
                wx, wy = self.screen_to_world(event.x, event.y)
                self.box_start = (wx, wy)
                self.box_current = (wx, wy)
                self.drawing_area.queue_draw()
                return True

        return False

    def on_button_release(self, widget, event):
        if event.button == 2 or (event.button == 1 and self.is_panning):
            self.is_panning = False

        if event.button == 1 and self.is_dragging_pins:
            self.is_dragging_pins = False
            self.active_pin_index = None

        if event.button == 1 and self.is_dragging_points:
            self.is_dragging_points = False
            self.active_index = None

        if event.button == 1 and self.is_box_selecting:
            self.is_box_selecting = False
            bx0 = min(self.box_start[0], self.box_current[0])
            bx1 = max(self.box_start[0], self.box_current[0])
            by0 = min(self.box_start[1], self.box_current[1])
            by1 = max(self.box_start[1], self.box_current[1])
            for i, (wx, wy) in enumerate(self.grid):
                if self.lock_boundary and self.is_boundary_vertex(i):
                    continue
                if bx0 <= wx <= bx1 and by0 <= wy <= by1:
                    self.selected_indices.add(i)

        self.drawing_area.queue_draw()
        return True

    def on_motion_notify(self, widget, event):
        if self.is_panning:
            self.pan_x = event.x - self.pan_start_x
            self.pan_y = event.y - self.pan_start_y
            self._invalidate_spatial()
            self.drawing_area.queue_draw()
            return True

        if self.is_box_selecting:
            wx, wy = self.screen_to_world(event.x, event.y)
            self.box_current = (wx, wy)
            self.drawing_area.queue_draw()
            return True

        # --- Puppet pin drag ---
        if self.is_dragging_pins and self.active_pin_index is not None:
            wx, wy = self.screen_to_world(event.x, event.y)
            pin = self.pins[self.active_pin_index]
            pin.dx = wx
            pin.dy = wy
            self._rebuild_grid_from_pins()
            return True

        # --- Grid vertex drag ---
        if self.is_dragging_points and self.active_index is not None:
            cur_wx, cur_wy = self.screen_to_world(event.x, event.y)
            delta_x = cur_wx - self.drag_mouse_start[0]
            delta_y = cur_wy - self.drag_mouse_start[1]

            act_start_x, act_start_y = self.drag_start_grid[self.active_index]
            radius = self.falloff_radius
            dirty_quads = set()

            for i in range(len(self.grid)):
                if self.lock_boundary and self.is_boundary_vertex(i):
                    continue

                orig_x, orig_y = self.drag_start_grid[i]
                weight = 0.0

                if i in self.selected_indices:
                    weight = 1.0
                elif self.use_falloff:
                    dist = math.hypot(orig_x - act_start_x, orig_y - act_start_y)
                    if dist <= radius:
                        weight = 0.5 * (1.0 + math.cos(math.pi * dist / radius))

                if weight > 0.0:
                    self.grid[i][0] = orig_x + weight * delta_x
                    self.grid[i][1] = orig_y + weight * delta_y

                    if self.symmetry_mode != 0:
                        mirror = self.get_mirror_vertex_index(i)
                        if mirror is not None and mirror < len(self.grid) and mirror != i:
                            if not (self.lock_boundary and self.is_boundary_vertex(mirror)):
                                m_orig_x, m_orig_y = self.drag_start_grid[mirror]
                                if self.symmetry_mode == 1:
                                    self.grid[mirror][0] = m_orig_x - weight * delta_x
                                    self.grid[mirror][1] = m_orig_y + weight * delta_y
                                elif self.symmetry_mode == 2:
                                    self.grid[mirror][0] = m_orig_x + weight * delta_x
                                    self.grid[mirror][1] = m_orig_y - weight * delta_y

                    self._mark_dirty_quads_for_vertex(i, dirty_quads)

            self._invalidate_spatial()

            if dirty_quads:
                for (qr, qc) in dirty_quads:
                    self._queue_quad_redraw(qr, qc)
            else:
                self.drawing_area.queue_draw()
            return True

        # --- Hover detection ---
        if self.mode == self.MODE_PUPPET:
            hit = self.find_pin_at_screen(event.x, event.y, hit_radius=14.0)
            if hit != self.hovered_pin_index:
                self.hovered_pin_index = hit
                self.drawing_area.queue_draw()
        else:
            hit = self.find_vertex_at_screen(event.x, event.y, hit_radius=10.0)
            if hit != self.hovered_index:
                self.hovered_index = hit
                self.drawing_area.queue_draw()
        return False

    def _mark_dirty_quads_for_vertex(self, idx, dirty_quads):
        c = idx % (self.cols + 1)
        r = idx // (self.cols + 1)
        for dr in (-1, 0):
            for dc in (-1, 0):
                qr, qc = r + dr, c + dc
                if 0 <= qr < self.rows and 0 <= qc < self.cols:
                    dirty_quads.add((qr, qc))

    def _queue_quad_redraw(self, qr, qc):
        xs, ys = [], []
        for (dr, dc) in ((0, 0), (0, 1), (1, 0), (1, 1)):
            idx = (qr + dr) * (self.cols + 1) + (qc + dc)
            sx, sy = self.orig_grid[idx]
            dx, dy = self.grid[idx]
            xs.extend([sx, dx])
            ys.extend([sy, dy])
        sx0, sy0 = self.world_to_screen(min(xs), min(ys))
        sx1, sy1 = self.world_to_screen(max(xs), max(ys))
        pad = 6
        self.drawing_area.queue_draw_area(
            int(sx0 - pad), int(sy0 - pad),
            int(sx1 - sx0 + 2 * pad), int(sy1 - sy0 + 2 * pad)
        )

    def on_scroll(self, widget, event):
        scale = 1.15 if event.direction == Gdk.ScrollDirection.UP else (1.0 / 1.15)
        self.set_zoom(self.zoom * scale, event.x, event.y)
        return True

    def on_key_press(self, widget, event):
        key = event.keyval
        if key == Gdk.KEY_space:
            self.space_pressed = True
            return True
        elif key in (Gdk.KEY_h, Gdk.KEY_H):
            self.grid_toggle.set_active(not self.grid_toggle.get_active())
            return True
        elif key in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            if self.mode == self.MODE_PUPPET:
                if self.active_pin_index is not None and 0 <= self.active_pin_index < len(self.pins):
                    del self.pins[self.active_pin_index]
                    self.active_pin_index = None
                    self._rebuild_grid_from_pins()
            else:
                if self.selected_indices:
                    self.set_preset_to_custom()
                    for idx in self.selected_indices:
                        self.grid[idx] = list(self.orig_grid[idx])
                    self._invalidate_spatial()
                    self.drawing_area.queue_draw()
            return True
        elif key == Gdk.KEY_Escape:
            self.selected_indices.clear()
            self.selected_pins.clear()
            self.drawing_area.queue_draw()
            return True
        elif key in (Gdk.KEY_plus, Gdk.KEY_equal):
            self.set_zoom(self.zoom * 1.25)
            return True
        elif key in (Gdk.KEY_minus, Gdk.KEY_underscore):
            self.set_zoom(self.zoom / 1.25)
            return True
        elif key == Gdk.KEY_0:
            self.fit_to_view()
            return True
        return False

    def on_key_release(self, widget, event):
        if event.keyval == Gdk.KEY_space:
            self.space_pressed = False
            self.is_panning = False
            return True
        return False

    def on_leave_notify(self, widget, event):
        changed = False
        if self.hovered_index is not None:
            self.hovered_index = None
            changed = True
        if self.hovered_pin_index is not None:
            self.hovered_pin_index = None
            changed = True
        if changed:
            self.drawing_area.queue_draw()
        return False

    # ==================================================================
    # Canvas rendering
    # ==================================================================

    def on_draw(self, widget, cr):
        # Dark backdrop
        cr.set_source_rgb(0.13, 0.13, 0.14)
        cr.paint()

        cr.save()
        cr.translate(self.pan_x, self.pan_y)
        cr.scale(self.zoom, self.zoom)

        if self.show_checker:
            self.draw_checkerboard(cr, self.canvas_w, self.canvas_h)

        if self.show_ghost:
            cr.save()
            cr.set_source_surface(self.src_surface, 0, 0)
            cr.paint_with_alpha(0.35)
            cr.restore()

        self.render_warped_mesh(cr, subdiv=self.subdiv_level, overlap=0.6)

        if self.show_grid:
            self.draw_mesh_overlay(cr)

        if self.mode == self.MODE_PUPPET:
            self.draw_puppet_pins(cr)

        cr.restore()

        if self.is_box_selecting:
            self.draw_marquee_box(cr)

    def draw_checkerboard(self, cr, w, h, size=16):
        cr.save()
        cr.rectangle(0, 0, w, h)
        cr.clip()
        cols = int(math.ceil(w / float(size)))
        rows = int(math.ceil(h / float(size)))
        for r in range(rows):
            for c in range(cols):
                if (r + c) % 2 == 0:
                    cr.set_source_rgb(0.85, 0.85, 0.85)
                else:
                    cr.set_source_rgb(0.65, 0.65, 0.65)
                cr.rectangle(c * size, r * size, size, size)
                cr.fill()
        cr.restore()

    def render_warped_mesh(self, cr, subdiv=6, overlap=0.6):
        """Fast Cairo preview: forward triangle rasterization."""
        cols, rows = self.cols, self.rows
        # Aim for a roughly constant total triangle budget regardless of grid
        # density, so that 12x12 grid doesn't blow up fill count vs 4x4.
        target_total = 30  # subdivs across the longer axis
        effective = max(1, min(6, target_total // max(cols, rows)))
        K = effective
        ox, oy = self.pad_left, self.pad_top

        for r in range(rows):
            for c in range(cols):
                s00 = self.orig_grid[r * (cols + 1) + c]
                s10 = self.orig_grid[r * (cols + 1) + (c + 1)]
                s01 = self.orig_grid[(r + 1) * (cols + 1) + c]
                s11 = self.orig_grid[(r + 1) * (cols + 1) + (c + 1)]

                d00 = self.grid[r * (cols + 1) + c]
                d10 = self.grid[r * (cols + 1) + (c + 1)]
                d01 = self.grid[(r + 1) * (cols + 1) + c]
                d11 = self.grid[(r + 1) * (cols + 1) + (c + 1)]

                for sub_r in range(K):
                    v0 = sub_r / float(K)
                    v1 = (sub_r + 1) / float(K)
                    for sub_c in range(K):
                        u0 = sub_c / float(K)
                        u1 = (sub_c + 1) / float(K)

                        ms00 = self.bilinear_interp(s00, s10, s01, s11, u0, v0, ox, oy)
                        ms10 = self.bilinear_interp(s00, s10, s01, s11, u1, v0, ox, oy)
                        ms01 = self.bilinear_interp(s00, s10, s01, s11, u0, v1, ox, oy)
                        ms11 = self.bilinear_interp(s00, s10, s01, s11, u1, v1, ox, oy)

                        md00 = self.bilinear_interp(d00, d10, d01, d11, u0, v0, ox, oy)
                        md10 = self.bilinear_interp(d00, d10, d01, d11, u1, v0, ox, oy)
                        md01 = self.bilinear_interp(d00, d10, d01, d11, u0, v1, ox, oy)
                        md11 = self.bilinear_interp(d00, d10, d01, d11, u1, v1, ox, oy)

                        self.draw_affine_triangle(cr, ms00, ms10, ms01, md00, md10, md01, overlap)
                        self.draw_affine_triangle(cr, ms10, ms11, ms01, md10, md11, md01, overlap)
    @staticmethod
    def bilinear_interp(p00, p10, p01, p11, u, v, ox=0.0, oy=0.0):
        x = ((1.0 - u) * (1.0 - v) * p00[0] + u * (1.0 - v) * p10[0] +
             (1.0 - u) * v * p01[0] + u * v * p11[0] + ox)
        y = ((1.0 - u) * (1.0 - v) * p00[1] + u * (1.0 - v) * p10[1] +
             (1.0 - u) * v * p01[1] + u * v * p11[1] + oy)
        return (x, y)

    def draw_affine_triangle(self, cr, s0, s1, s2, d0, d1, d2, overlap=0.6):
        m_inv = compute_affine_matrix(d0, d1, d2, s0, s1, s2)
        if m_inv is None:
            return
        a, b, c, d, e, f = m_inv
        self._src_pattern.set_matrix(cairo.Matrix(xx=a, yx=b, xy=c, yy=d, x0=e, y0=f))

        if overlap > 0.0:
            cx = (d0[0] + d1[0] + d2[0]) / 3.0
            cy = (d0[1] + d1[1] + d2[1]) / 3.0
            def dilate(pt):
                dx = pt[0] - cx
                dy = pt[1] - cy
                dist = math.hypot(dx, dy)
                if dist > 1e-4:
                    return (pt[0] + (dx / dist) * overlap,
                            pt[1] + (dy / dist) * overlap)
                return pt
            d0 = dilate(d0); d1 = dilate(d1); d2 = dilate(d2)

        cr.save()
        cr.set_source(self._src_pattern)
        cr.new_path()
        cr.move_to(d0[0], d0[1])
        cr.line_to(d1[0], d1[1])
        cr.line_to(d2[0], d2[1])
        cr.close_path()
        cr.fill()
        cr.restore()

    def draw_mesh_overlay(self, cr):
        cols, rows = self.cols, self.rows
        line_w = max(1.0, 1.2 / self.zoom)
        ox, oy = self.pad_left, self.pad_top

        if self.mode == self.MODE_PUPPET:
            alpha = 0.25
            base_rgb = (0.1, 0.85, 1.0)
        else:
            alpha = 0.9
            base_rgb = (0.1, 0.85, 1.0)

        cr.save()
        cr.translate(ox, oy)

        cr.set_source_rgba(0.0, 0.0, 0.0, 0.35 if self.mode == self.MODE_PUPPET else 0.55)
        cr.set_line_width(line_w * 2.4)
        self.trace_grid_lines(cr)
        cr.stroke()

        cr.set_source_rgba(base_rgb[0], base_rgb[1], base_rgb[2], alpha)
        cr.set_line_width(line_w)
        self.trace_grid_lines(cr)
        cr.stroke()

        if (self.mode == self.MODE_GRID and self.use_falloff and
                (self.active_index is not None or self.hovered_index is not None)):
            center_idx = self.active_index if self.active_index is not None else self.hovered_index
            cx, cy = self.grid[center_idx]
            cr.save()
            cr.set_source_rgba(1.0, 0.8, 0.1, 0.3)
            cr.set_line_width(max(1.0, 1.5 / self.zoom))
            cr.set_dash([4.0 / self.zoom, 4.0 / self.zoom])
            cr.arc(cx, cy, self.falloff_radius, 0, 2 * math.pi)
            cr.stroke()
            cr.restore()

        if self.mode == self.MODE_GRID:
            radius = max(3.5, 5.0 / self.zoom)
            for i, (gx, gy) in enumerate(self.grid):
                is_selected = i in self.selected_indices
                is_active = (i == self.active_index)
                is_hovered = (i == self.hovered_index)
                is_boundary = self.is_boundary_vertex(i)

                cr.set_source_rgba(0.0, 0.0, 0.0, 0.75)
                cr.arc(gx, gy, radius + (1.5 / self.zoom), 0, 2 * math.pi)
                cr.fill()

                if is_active:
                    cr.set_source_rgb(0.2, 1.0, 0.3)
                elif is_selected:
                    cr.set_source_rgb(1.0, 0.7, 0.0)
                elif is_hovered:
                    cr.set_source_rgb(0.2, 0.9, 1.0)
                elif is_boundary and self.lock_boundary:
                    cr.set_source_rgb(0.5, 0.5, 0.5)
                else:
                    cr.set_source_rgb(1.0, 1.0, 1.0)

                cr.arc(gx, gy, radius, 0, 2 * math.pi)
                cr.fill()

                cr.set_source_rgba(0.0, 0.0, 0.0, 0.7)
                cr.arc(gx, gy, radius * 0.35, 0, 2 * math.pi)
                cr.fill()

        cr.restore()

    def trace_grid_lines(self, cr):
        cols, rows = self.cols, self.rows
        for r in range(rows + 1):
            for c in range(cols):
                p0 = self.grid[r * (cols + 1) + c]
                p1 = self.grid[r * (cols + 1) + (c + 1)]
                cr.move_to(p0[0], p0[1])
                cr.line_to(p1[0], p1[1])
        for c in range(cols + 1):
            for r in range(rows):
                p0 = self.grid[r * (cols + 1) + c]
                p1 = self.grid[(r + 1) * (cols + 1) + c]
                cr.move_to(p0[0], p0[1])
                cr.line_to(p1[0], p1[1])

    def draw_puppet_pins(self, cr):
        """Draw puppet pins in world space (padding offset applied)."""
        ox, oy = self.pad_left, self.pad_top
        cr.save()
        cr.translate(ox, oy)

        pin_radius = max(5.0, 8.0 / self.zoom)
        inner_r = pin_radius * 0.45

        for i, pin in enumerate(self.pins):
            dx, dy = pin.dx, pin.dy
            sx, sy = pin.sx, pin.sy
            moved = (abs(dx - sx) > 0.5 or abs(dy - sy) > 0.5)

            is_active = (i == self.active_pin_index)
            is_hovered = (i == self.hovered_pin_index)

            if moved:
                cr.save()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
                cr.set_line_width(max(1.0, 1.2 / self.zoom))
                cr.set_dash([4.0 / self.zoom, 4.0 / self.zoom])
                cr.move_to(sx, sy)
                cr.line_to(dx, dy)
                cr.stroke()
                cr.restore()

                cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
                cr.arc(sx, sy, pin_radius * 0.4, 0, 2 * math.pi)
                cr.fill()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.85)
            cr.arc(dx, dy, pin_radius + (1.5 / self.zoom), 0, 2 * math.pi)
            cr.fill()

            if is_active:
                cr.set_source_rgb(0.2, 1.0, 0.3)
            elif is_hovered:
                cr.set_source_rgb(0.2, 0.9, 1.0)
            else:
                cr.set_source_rgb(1.0, 0.9, 0.2)

            cr.arc(dx, dy, pin_radius, 0, 2 * math.pi)
            cr.fill()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.85)
            cr.set_line_width(max(1.0, 1.2 / self.zoom))
            cr.move_to(dx - inner_r, dy)
            cr.line_to(dx + inner_r, dy)
            cr.move_to(dx, dy - inner_r)
            cr.line_to(dx, dy + inner_r)
            cr.stroke()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.7)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(max(9.0, 11.0 / self.zoom))
            cr.move_to(dx + pin_radius * 1.3, dy - pin_radius * 0.8)
            cr.show_text(str(i + 1))

        cr.restore()

    def draw_marquee_box(self, cr):
        sx0, sy0 = self.world_to_screen(self.box_start[0], self.box_start[1])
        sx1, sy1 = self.world_to_screen(self.box_current[0], self.box_current[1])
        x = min(sx0, sx1)
        y = min(sy0, sy1)
        w = abs(sx1 - sx0)
        h = abs(sy1 - sy0)
        cr.save()
        cr.set_source_rgba(0.1, 0.6, 1.0, 0.2)
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgba(0.1, 0.8, 1.0, 0.9)
        cr.set_line_width(1.0)
        cr.set_dash([4.0, 3.0])
        cr.rectangle(x, y, w, h)
        cr.stroke()
        cr.restore()

    # ==================================================================
    # Apply
    # ==================================================================

    def apply_warp_to_drawable(self):
        """Push current mesh + pins + mode into the custom:mesh-warp filter."""
        self.image.undo_group_start()
        try:
            if not self.drawable.has_alpha():
                self.drawable.add_alpha()

            grid_min_x = min(p[0] for p in self.grid)
            grid_max_x = max(p[0] for p in self.grid)
            grid_min_y = min(p[1] for p in self.grid)
            grid_max_y = max(p[1] for p in self.grid)

            pad_left = self.pad_left
            pad_top  = self.pad_top
            cur_w = self.drawable.get_width()
            cur_h = self.drawable.get_height()

            if self.expand_layer:
                want_l = max(0, int(math.ceil(-grid_min_x)))
                want_t = max(0, int(math.ceil(-grid_min_y)))
                want_r = max(0, int(math.ceil(grid_max_x - self.width)))
                want_b = max(0, int(math.ceil(grid_max_y - self.height)))

                need_w = self.width  + want_l + want_r
                need_h = self.height + want_t + want_b

                if need_w > cur_w or need_h > cur_h:
                    add_l = max(0, want_l - pad_left)
                    add_t = max(0, want_t - pad_top)
                    add_r = max(0, need_w - cur_w - add_l)
                    add_b = max(0, need_h - cur_h - add_t)

                    self.drawable.resize(cur_w + add_l + add_r,
                                         cur_h + add_t + add_b,
                                         add_l, add_t)
                    pad_left = want_l
                    pad_top  = want_t
                    self.pad_left = pad_left
                    self.pad_top  = pad_top

            cur_w = self.drawable.get_width()
            cur_h = self.drawable.get_height()

            padded_grid = [[x + pad_left, y + pad_top] for x, y in self.grid]
            grid_json = json.dumps(padded_grid)

            mode_str = "puppet" if self.mode == self.MODE_PUPPET else "grid"
            pins_json = json.dumps([p.to_list() for p in self.pins])

            config_kwargs = {
                "cols":        self.cols,
                "rows":        self.rows,
                "grid-json":   grid_json,
                "pad-left":    pad_left,
                "pad-top":     pad_top,
                "orig-width":  self.orig_width,
                "orig-height": self.orig_height,
                "pins-json":   pins_json,
                "mode":        mode_str,
            }

            if self.existing_mesh_warp_filter is not None:
                try:
                    config = self.existing_mesh_warp_filter.get_config()
                    for k, v in config_kwargs.items():
                        config.set_property(k, v)
                    self.existing_mesh_warp_filter.update()
                except Exception as e:
                    Gimp.message(f"Mesh Warp: could not update existing filter, "
                                 f"creating a new one ({e}).")
                    self.existing_mesh_warp_filter = None

            if self.existing_mesh_warp_filter is None:
                filt = Gimp.DrawableFilter.new(self.drawable, GEGL_OP_NAME, GEGL_OP_LABEL)
                config = filt.get_config()
                for k, v in config_kwargs.items():
                    config.set_property(k, v)
                self.drawable.append_filter(filt)
                self.existing_mesh_warp_filter = filt

            self.drawable.update(0, 0, cur_w, cur_h)

        finally:
            self.image.undo_group_end()
            Gimp.displays_flush()


# ==============================================================================
# GIMP 3.0 / 3.2 PlugIn Procedure Definition
# ==============================================================================

class MeshWarpPlugin(Gimp.PlugIn):
    def do_set_i18n(self, procname):
        return True, 'gimp30-python', None

    def do_query_procedures(self):
        return [PLUG_IN_PROC]

    def do_create_procedure(self, name):
        procedure = None
        if name == PLUG_IN_PROC:
            procedure = Gimp.ImageProcedure.new(
                self, name, Gimp.PDBProcType.PLUGIN, self.run, None
            )
            procedure.set_image_types("RGB*, GRAY*, INDEXED*")
            procedure.set_sensitivity_mask(
                Gimp.ProcedureSensitivityMask.DRAWABLE |
                Gimp.ProcedureSensitivityMask.DRAWABLES
            )
            procedure.set_menu_label("_Mesh Warp...")
            procedure.add_menu_path("<Image>/Filters/Distorts")
            procedure.add_menu_path("<Image>/Tools/Transform Tools")
            procedure.set_attribution("Antigravity", "Antigravity", "2026")
            procedure.set_documentation(
                "Interactive Mesh Warp with Presets & Puppet Pins",
                "Interactively deform layers using a mesh-warp grid or puppet "
                "pins. Pixel warp is delegated to the custom:mesh-warp GEGL "
                "operation applied as a non-destructive GIMP 3.2 layer effect.",
                PLUG_IN_PROC
            )
        return procedure

    def run(self, procedure, run_mode, image, drawables, config, data):
        if len(drawables) != 1 or not isinstance(drawables[0], Gimp.Drawable):
            return procedure.new_return_values(
                Gimp.PDBStatusType.CALLING_ERROR,
                GLib.Error(f"Procedure '{PLUG_IN_PROC}' requires exactly one active drawable layer.")
            )
        drawable = drawables[0]
        if run_mode == Gimp.RunMode.NONINTERACTIVE:
            return procedure.new_return_values(
                Gimp.PDBStatusType.CALLING_ERROR,
                GLib.Error(f"Procedure '{PLUG_IN_PROC}' must be run interactively.")
            )

        GimpUi.init(PLUG_IN_BINARY)
        dialog = MeshWarpDialog(image, drawable)
        GimpUi.window_set_transient(dialog)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            dialog.apply_warp_to_drawable()
            dialog.destroy()
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)
        else:
            dialog.destroy()
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)


if __name__ == '__main__':
    Gimp.main(MeshWarpPlugin.__gtype__, sys.argv)
