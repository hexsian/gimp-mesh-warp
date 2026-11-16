#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mesh Warp & Transform Presets Plugin for GIMP 3.0 / 3.2+
=========================================================
Heavy lifting (preview warp, RBF, resample, smooth) is done in C
(mesh-warp.c -> mesh-warp.so / mesh-warp.dll) and called via ctypes.
Python handles only the UI, state and event dispatch. Overlays stay in Cairo.
No NumPy needed: grids are flat ctypes double arrays [x0,y0,x1,y1,...] and
pixel buffers are Cairo surfaces handed to C by address.

Author: Antigravity
License: GNU General Public License v3 or later
"""

import math
import os
import sys
import json
import ctypes
import time
import traceback

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

# ---------------------------------------------------------------------------
# Load C core
# ---------------------------------------------------------------------------

def _core_names():
    if os.name == "nt":
        return ["mesh-warp.dll"]
    if sys.platform == "darwin":
        return ["mesh-warp.so", "mesh-warp.dylib"]
    return ["mesh-warp.so"]


def _core_dirs():
    here = os.path.dirname(os.path.realpath(__file__))
    dirs = [here, os.path.join(GLib.get_user_data_dir(), "gegl-0.4", "plug-ins")]
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA")
        if la:
            dirs.append(os.path.join(la, "gegl-0.4", "plug-ins"))
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _load_core():
    for d in _core_dirs():
        for name in _core_names():
            path = os.path.join(d, name)
            if not os.path.exists(path):
                continue
            if hasattr(os, "add_dll_directory"):      # Windows: let deps next to the DLL resolve
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
            return ctypes.CDLL(path)
    raise RuntimeError(
        "%s not found in the plugin folder or gegl-0.4/plug-ins." % _core_names()[0]
    )


_core = None
_CORE_ERROR = None
try:
    _core = _load_core()
except Exception as _e:                               # reported from run(), not at import
    _CORE_ERROR = str(_e)

_HAS_VP_RENDER = False
if _core is not None:
    try:
        _core.mesh_omp_max_threads.argtypes = []
        _core.mesh_omp_max_threads.restype = ctypes.c_int
        _nthreads = _core.mesh_omp_max_threads()
        if _nthreads <= 1:
            print(f"[mesh-warp] OpenMP threads={_nthreads} — "
                  f"build with -fopenmp for multithreading", file=sys.stderr)
    except AttributeError:
        pass

    _dp   = ctypes.POINTER(ctypes.c_double)

    _core.mesh_preview_render.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
        _dp, _dp, ctypes.c_int, ctypes.c_int,
        ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double,
    ]
    _core.mesh_preview_render.restype = None

    if hasattr(_core, "mesh_preview_render_vp"):
        _core.mesh_preview_render_vp.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            _dp, _dp, ctypes.c_int, ctypes.c_int,
            ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.c_double, ctypes.c_double,
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        _core.mesh_preview_render_vp.restype = None
        _HAS_VP_RENDER = True

    _core.mesh_rbf_build.argtypes = [
        _dp, ctypes.c_int, _dp, ctypes.c_int, ctypes.c_double, _dp]
    _core.mesh_rbf_build.restype = None

    _core.mesh_resample_grid.argtypes = [
        _dp, ctypes.c_int, ctypes.c_int, _dp, ctypes.c_int, ctypes.c_int]
    _core.mesh_resample_grid.restype = None

    _core.mesh_smooth_grid.argtypes = [
        _dp, ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.c_int]
    _core.mesh_smooth_grid.restype = None


def _new_grid(n_verts):
    """Zeroed flat grid [x0, y0, x1, y1, ...] with n_verts vertices."""
    return (ctypes.c_double * (2 * n_verts))()


def _copy_grid(g):
    return type(g).from_buffer_copy(g)


def _grid_from_pairs(pairs):
    flat = []
    for p in pairs:
        flat.append(float(p[0]))
        flat.append(float(p[1]))
    return (ctypes.c_double * len(flat))(*flat)


def _surface_ptr(surf):
    """Address of a Cairo ARGB32 image surface's pixel data (stride must be w*4)."""
    if surf.get_stride() != surf.get_width() * 4:
        raise RuntimeError("unexpected cairo stride")
    surf.flush()
    mv = surf.get_data()
    return ctypes.addressof(ctypes.c_char.from_buffer(mv))


PLUG_IN_PROC   = "plug-in-mesh-warp"
PLUG_IN_BINARY = "mesh_warp"
GEGL_OP_NAME   = "custom:mesh-warp"
GEGL_OP_LABEL  = "Mesh Warp"

PRESETS = [
    "Custom", "Arc", "Arc Lower", "Arc Upper", "Arch",
    "Bulge", "Shell Lower", "Shell Upper", "Flag", "Wave",
    "Fish", "Rise", "FishEye", "Inflate", "Squeeze", "Twist",
]


# ---------------------------------------------------------------------------
# Pin & preset math
# ---------------------------------------------------------------------------

class Pin:
    __slots__ = ('sx', 'sy', 'dx', 'dy')

    def __init__(self, sx, sy):
        self.sx = float(sx); self.sy = float(sy)
        self.dx = float(sx); self.dy = float(sy)

    def to_list(self):
        return [self.sx, self.sy, self.dx, self.dy]

    @classmethod
    def from_list(cls, lst):
        p = cls(lst[0], lst[1])
        p.dx = float(lst[2]); p.dy = float(lst[3])
        return p


def evaluate_warp_preset(preset_name, u, v, bend=0.5,
                         h_dist=0.0, v_dist=0.0, is_vertical=False):
    if preset_name == "Custom" or (abs(bend) < 1e-5 and abs(h_dist) < 1e-5
                                   and abs(v_dist) < 1e-5):
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
        sy_ = (1.0 - u_base * 0.65) * 0.7 + 0.3 + b * 0.4 * math.sin(u_base * math.pi)
        v_out = 0.5 + yc * 0.5 * sy_
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


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class MeshWarpDialog(Gtk.Dialog):
    MODE_GRID   = 0
    MODE_PUPPET = 1
    MAX_PREVIEW_DIM = 2048

    def __init__(self, image, drawable):
        super().__init__(
            title="Interactive Mesh Warp & Presets",
            modal=True, destroy_with_parent=True)
        self.set_default_size(1100, 820)
        self.set_position(Gtk.WindowPosition.CENTER)

        self._dead = False
        self._updating_ui = False
        self._mesh_seq = 0
        self._mesh_path_cache = None
        self._undo_stack = []
        self._redo_stack = []

        self._drag_pushed = False
        self._drag_active = None
        self._drag_timer = None
        self._pending_drag_xy = None
        self._last_drag_t = 0.0
        self._tick_ms = 16.0
        self._preset_timer = None

        self.image = image
        self.drawable = drawable

        self.existing_mesh_warp_filter = self.find_existing_mesh_warp_filter()
        loaded_state = None
        if self.existing_mesh_warp_filter is not None:
            loaded_state = self.try_load_filter_state(self.existing_mesh_warp_filter)
            if loaded_state is None:
                self.existing_mesh_warp_filter = None

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

        # ---- source snapshot ----
        rect = Gegl.Rectangle.new(0, 0, canvas_w, canvas_h)
        buf = drawable.get_buffer()
        self.raw_src_pixels = bytes(
            buf.get(rect, 1.0, "cairo-ARGB32", Gegl.AbyssPolicy.NONE))
        self._full_src_surface = None
        self._preview_surface  = None
        self._preview_key      = None
        self._preview_sx_scale = 1.0
        self._preview_sy_scale = 1.0
        self._preview_ptr      = None

        self._checker_surf = None
        self._checker_pattern = self._make_checker_pattern()

        # ---- warp output surface ----
        self._dst_ptr     = None
        self._dst_surface = None
        self._dst_w       = 0
        self._dst_h       = 0

        # ---- mesh (flat ctypes double arrays) ----
        self.cols = loaded_state['cols'] if loaded_state else 4
        self.rows = loaded_state['rows'] if loaded_state else 4
        self.orig_grid = None
        self.grid = None
        self.init_mesh()
        if loaded_state is not None:
            self.grid = _grid_from_pairs(loaded_state['grid'])

        # ---- puppet pins ----
        self.mode = loaded_state.get('mode', self.MODE_GRID) if loaded_state else self.MODE_GRID
        self.pins = []
        if loaded_state is not None and 'pins' in loaded_state:
            self.pins = [Pin.from_list(l) for l in loaded_state['pins']]
        self.puppet_radius = 150.0
        self.active_pin_index = None
        self.hovered_pin_index = None

        # ---- preset state ----
        self.current_preset = "Custom"
        self.bend_val = 0.5
        self.h_dist_val = 0.0
        self.v_dist_val = 0.0
        self.is_vertical_preset = False
        self.updating_ui_preset = False

        # ---- viewport ----
        self.pan_x = 25.0; self.pan_y = 25.0
        self.zoom = 1.0
        self.pan_start_x = 0.0; self.pan_start_y = 0.0
        self.is_panning = False
        self.space_pressed = False

        # ---- selection / drag ----
        self.selected_indices = set()
        self.active_index = None
        self.hovered_index = None
        self.is_dragging_points = False
        self.is_dragging_pins = False
        self.drag_mouse_start = (0.0, 0.0)
        self.drag_start_grid = None

        self.is_box_selecting = False
        self.box_start = (0.0, 0.0)
        self.box_current = (0.0, 0.0)

        # ---- options ----
        self.lock_boundary = False
        self.use_falloff = False
        self.falloff_radius = 120.0
        self.expand_layer = True
        self.symmetry_mode = 0
        self.show_grid = True
        self.show_ghost = False
        self.show_checker = True

        self._diag = os.environ.get('MESH_WARP_DIAG') == '1'
        self.MAX_PREVIEW_DIM = 2048

        self.build_ui()
        self.connect("key-press-event", self._on_dialog_key)
        self.connect("destroy", self._on_destroy)
        self.fit_to_view()

    def _on_destroy(self, widget):
        self._dead = True
        for attr in ("_drag_timer", "_preset_timer"):
            tid = getattr(self, attr, None)
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._mesh_path_cache = None
        self._preview_surface = None
        self._full_src_surface = None
        self._dst_surface = None

    # ==================================================================
    # Grid init & Undo/Redo
    # ==================================================================

    def _nv(self):
        return (self.cols + 1) * (self.rows + 1)

    def rebuild_orig_mesh(self):
        cols, rows = self.cols, self.rows
        g = _new_grid((cols + 1) * (rows + 1))
        k = 0
        for r in range(rows + 1):
            y = float(self.height) * r / rows
            for c in range(cols + 1):
                g[k] = float(self.width) * c / cols
                g[k + 1] = y
                k += 2
        self.orig_grid = g

    def _reset_vertex(self, i):
        self.grid[2 * i] = self.orig_grid[2 * i]
        self.grid[2 * i + 1] = self.orig_grid[2 * i + 1]

    def init_mesh(self):
        self.rebuild_orig_mesh()
        self.grid = _copy_grid(self.orig_grid)
        self._mesh_seq = getattr(self, "_mesh_seq", 0) + 1

    def _capture_state(self):
        return {
            'cols': int(self.cols),
            'rows': int(self.rows),
            'mode': int(self.mode),
            'grid': _copy_grid(self.grid),
            'pins': [p.to_list() for p in self.pins],
        }

    def _push_undo(self):
        self._undo_stack.append(self._capture_state())
        if len(self._undo_stack) > 64:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _apply_undo_state(self, st):
        self._updating_ui = True
        try:
            self.cols = st['cols']
            self.rows = st['rows']
            self.mode = st['mode']
            self.rebuild_orig_mesh()
            self.grid = _copy_grid(st['grid'])
            self.pins = [Pin.from_list(p) for p in st['pins']]
            self.selected_indices.clear()
            self.active_index = None
            self.active_pin_index = None
            self.cols_spin.set_value(self.cols)
            self.rows_spin.set_value(self.rows)
            if self.mode == self.MODE_PUPPET:
                self.mode_puppet_btn.set_active(True)
            else:
                self.mode_grid_btn.set_active(True)
            self._update_mode_ui()
            self._mesh_seq += 1
            self.drawing_area.queue_draw()
        finally:
            self._updating_ui = False

    def _do_undo(self):
        if not self._undo_stack:
            return False
        self._redo_stack.append(self._capture_state())
        self._apply_undo_state(self._undo_stack.pop())
        return True

    def _do_redo(self):
        if not self._redo_stack:
            return False
        self._undo_stack.append(self._capture_state())
        self._apply_undo_state(self._redo_stack.pop())
        return True

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
                'cols': loaded_cols, 'rows': loaded_rows, 'grid': grid,
                'pad_left': pad_left, 'pad_top': pad_top,
                'orig_width': orig_width, 'orig_height': orig_height,
                'pins': pins, 'mode': mode,
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

        preset_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        preset_bar.pack_start(Gtk.Label(label="<b>Mode:</b>", use_markup=True), False, False, 0)

        self.mode_grid_btn = Gtk.RadioButton.new_with_label(None, "Grid")
        self.mode_puppet_btn = Gtk.RadioButton.new_with_label_from_widget(
            self.mode_grid_btn, "Puppet")
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
        self.orient_toggle.connect("toggled", self.on_orient_toggled)
        preset_bar.pack_start(self.orient_toggle, False, False, 0)
        preset_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        preset_bar.pack_start(Gtk.Label(label="Bend:"), False, False, 0)
        self.bend_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.bend_scale.set_value(50); self.bend_scale.set_size_request(100, -1)
        self.bend_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.bend_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.bend_scale, False, False, 0)

        preset_bar.pack_start(Gtk.Label(label="H-D:"), False, False, 0)
        self.h_dist_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.h_dist_scale.set_size_request(85, -1)
        self.h_dist_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.h_dist_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.h_dist_scale, False, False, 0)

        preset_bar.pack_start(Gtk.Label(label="V-D:"), False, False, 0)
        self.v_dist_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self.v_dist_scale.set_size_request(85, -1)
        self.v_dist_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.v_dist_scale.connect("value-changed", self.on_preset_param_changed)
        preset_bar.pack_start(self.v_dist_scale, False, False, 0)

        content_area.pack_start(preset_bar, False, False, 0)

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

        self.puppet_label = Gtk.Label(label="Pin Radius:")
        grid_bar.pack_start(self.puppet_label, False, False, 0)
        self.puppet_radius_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 20, 600, 5)
        self.puppet_radius_scale.set_value(self.puppet_radius)
        self.puppet_radius_scale.set_size_request(140, -1)
        self.puppet_radius_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.puppet_radius_scale.connect("value-changed", self.on_puppet_radius_changed)
        grid_bar.pack_start(self.puppet_radius_scale, False, False, 0)

        self.puppet_clear_btn = Gtk.Button(label="Clear Pins")
        self.puppet_clear_btn.connect("clicked", self.on_clear_pins_clicked)
        grid_bar.pack_start(self.puppet_clear_btn, False, False, 0)
        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        self.lock_check = Gtk.CheckButton(label="Lock Edges")
        self.lock_check.connect("toggled", self.on_lock_boundary_toggled)
        grid_bar.pack_start(self.lock_check, False, False, 0)

        self.falloff_check = Gtk.CheckButton(label="Falloff")
        self.falloff_check.connect("toggled", self.on_falloff_toggled)
        grid_bar.pack_start(self.falloff_check, False, False, 0)

        grid_bar.pack_start(Gtk.Label(label="Rad:"), False, False, 0)
        self.radius_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 20, 600, 10)
        self.radius_scale.set_value(self.falloff_radius)
        self.radius_scale.set_size_request(85, -1)
        self.radius_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.radius_scale.connect("value-changed", self.on_radius_changed)
        grid_bar.pack_start(self.radius_scale, False, False, 0)
        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        grid_bar.pack_start(Gtk.Label(label="Sym:"), False, False, 0)
        self.sym_combo = Gtk.ComboBoxText()
        for t in ("None", "Horizontal (L/R)", "Vertical (T/B)"):
            self.sym_combo.append_text(t)
        self.sym_combo.set_active(0)
        self.sym_combo.connect("changed", self.on_symmetry_changed)
        grid_bar.pack_start(self.sym_combo, False, False, 0)

        self.expand_check = Gtk.CheckButton(label="Auto-Expand")
        self.expand_check.set_active(True)
        self.expand_check.connect("toggled",
            lambda b: setattr(self, 'expand_layer', b.get_active()))
        grid_bar.pack_start(self.expand_check, False, False, 0)
        grid_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        relax_btn = Gtk.Button(label="Smooth")
        relax_btn.connect("clicked", self.on_smooth_clicked)
        grid_bar.pack_start(relax_btn, False, False, 0)

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.connect("clicked", self.on_reset_clicked)
        grid_bar.pack_start(reset_btn, False, False, 0)

        content_area.pack_start(grid_bar, False, False, 0)

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
            Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.drawing_area.connect("draw", self.on_draw)
        self.drawing_area.connect("button-press-event", self.on_button_press)
        self.drawing_area.connect("button-release-event", self.on_button_release)
        self.drawing_area.connect("motion-notify-event", self.on_motion_notify)
        self.drawing_area.connect("scroll-event", self.on_scroll)
        self.drawing_area.connect("key-release-event", self.on_key_release)
        self.drawing_area.connect("leave-notify-event", self.on_leave_notify)

        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(self.drawing_area)
        content_area.pack_start(frame, True, True, 0)

        bottom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.grid_toggle = Gtk.ToggleButton(label="Show Grid (H)")
        self.grid_toggle.set_active(True)
        self.grid_toggle.connect("toggled", self.on_toggle_grid)
        bottom_box.pack_start(self.grid_toggle, False, False, 0)

        self.ghost_toggle = Gtk.ToggleButton(label="Ghost Original")
        self.ghost_toggle.connect("toggled", self.on_toggle_ghost)
        bottom_box.pack_start(self.ghost_toggle, False, False, 0)

        fit_btn = Gtk.Button(label="Fit")
        fit_btn.connect("clicked", lambda b: self.fit_to_view())
        bottom_box.pack_start(fit_btn, False, False, 0)

        zoom_btn = Gtk.Button(label="100%")
        zoom_btn.connect("clicked", lambda b: self.set_zoom(1.0))
        bottom_box.pack_start(zoom_btn, False, False, 0)

        self.zoom_label = Gtk.Label(label="100%")
        bottom_box.pack_start(self.zoom_label, False, False, 0)
        bottom_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        self.status_label = Gtk.Label(
            label="Grid: drag points · Shift multi · Ctrl+Z undo. Puppet: click empty add pin, drag move, right-click delete.")
        self.status_label.set_xalign(0.0)
        bottom_box.pack_start(self.status_label, True, True, 0)

        content_area.pack_start(bottom_box, False, False, 0)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_OK",     Gtk.ResponseType.OK)

        self.show_all()
        self._update_mode_ui()

    def _update_mode_ui(self):
        puppet = (self.mode == self.MODE_PUPPET)
        for w in (self.puppet_label, self.puppet_radius_scale, self.puppet_clear_btn):
            w.set_sensitive(puppet)
        for w in (self.preset_combo, self.orient_toggle, self.bend_scale,
                  self.h_dist_scale, self.v_dist_scale):
            w.set_sensitive(not puppet)

    # ==================================================================
    # C-backed mesh operations
    # ==================================================================

    def _rebuild_grid_from_pins(self):
        if not self.pins:
            self.grid = _copy_grid(self.orig_grid)
        else:
            n_v = len(self.orig_grid) // 2
            flat = []
            for p in self.pins:
                flat.extend(p.to_list())
            pins_arr = (ctypes.c_double * len(flat))(*flat)
            out = _new_grid(n_v)
            _core.mesh_rbf_build(
                self.orig_grid, n_v,
                pins_arr, len(self.pins),
                float(self.puppet_radius),
                out)
            self.grid = out
        self._mesh_seq += 1
        self.drawing_area.queue_draw()

    def _resample_grid(self, new_cols, new_rows):
        out = _new_grid((new_cols + 1) * (new_rows + 1))
        _core.mesh_resample_grid(
            self.grid, self.cols, self.rows,
            out, new_cols, new_rows)
        return out

    def _smooth_grid(self):
        _core.mesh_smooth_grid(
            self.grid, self.cols, self.rows,
            0.4, 1 if self.lock_boundary else 0)
        self._mesh_seq += 1

    def _render_warp_c(self, use_vp=False):
        self._refresh_preview_surface()
        src_surf = self._preview_surface
        src_w, src_h = src_surf.get_width(), src_surf.get_height()

        if self._preview_ptr is None:
            self._preview_ptr = _surface_ptr(src_surf)

        self._dst_surface.flush()

        if use_vp and _HAS_VP_RENDER:
            _core.mesh_preview_render_vp(
                self._dst_ptr, self._dst_w, self._dst_h,
                self._preview_ptr, src_w, src_h,
                self.grid,
                self.orig_grid,
                self.cols, self.rows,
                float(self.pad_left), float(self.pad_top),
                self._preview_sx_scale, self._preview_sy_scale,
                float(self.zoom), float(self.pan_x), float(self.pan_y),
                int(self.canvas_w), int(self.canvas_h),
                1 if self.show_checker else 0)
        else:
            _core.mesh_preview_render(
                self._dst_ptr, self._dst_w, self._dst_h,
                self._preview_ptr, src_w, src_h,
                self.grid,
                self.orig_grid,
                self.cols, self.rows,
                float(self.pad_left), float(self.pad_top),
                self._preview_sx_scale, self._preview_sy_scale,
                float(self.zoom), float(self.pan_x), float(self.pan_y))
        self._dst_surface.mark_dirty()

    # ==================================================================
    # Surfaces
    # ==================================================================

    def _get_full_src_surface(self):
        if self._full_src_surface is None:
            self._full_src_surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, self.canvas_w, self.canvas_h)
            self._full_src_surface.get_data()[:] = self.raw_src_pixels
            self._full_src_surface.mark_dirty()
            self.raw_src_pixels = None
        return self._full_src_surface

    def _refresh_preview_surface(self):
        alloc = self.drawing_area.get_allocation()
        ww = max(getattr(alloc, 'width', 0) or 680, 100)
        wh = max(getattr(alloc, 'height', 0) or 500, 100)
        fit = min(ww / self.canvas_w, wh / self.canvas_h)
        s   = min(1.0, max(self.zoom, fit))
        tw  = max(1, int(self.canvas_w * s))
        th  = max(1, int(self.canvas_h * s))

        cap = self.MAX_PREVIEW_DIM
        if tw > cap or th > cap:
            f = cap / max(tw, th)
            tw = max(1, int(tw * f))
            th = max(1, int(th * f))

        key = (tw, th)
        if self._preview_key == key and self._preview_surface is not None:
            return

        full = self._get_full_src_surface()
        if tw >= self.canvas_w and th >= self.canvas_h:
            surf = full
        else:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, tw, th)
            c = cairo.Context(surf)
            c.scale(tw / self.canvas_w, th / self.canvas_h)
            c.set_source_surface(full, 0, 0)
            c.get_source().set_filter(cairo.FILTER_GOOD)
            c.paint()
            surf.flush()

        self._preview_surface  = surf
        self._preview_key      = key
        self._preview_sx_scale = surf.get_width()  / self.canvas_w
        self._preview_sy_scale = surf.get_height() / self.canvas_h
        self._preview_ptr      = None

    def _ensure_dst_surface(self, w, h):
        if self._dst_surface is not None and self._dst_w == w and self._dst_h == h:
            return
        self._dst_w, self._dst_h = w, h
        self._dst_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        self._dst_ptr = _surface_ptr(self._dst_surface)

    # ==================================================================
    # Mode / preset handlers
    # ==================================================================

    def on_mode_toggled(self, btn):
        if self._updating_ui or not btn.get_active():
            return
        new_mode = self.MODE_PUPPET if btn is self.mode_puppet_btn else self.MODE_GRID
        if new_mode == self.mode:
            return
        self._push_undo()
        if new_mode == self.MODE_PUPPET:
            if self.cols < 12 or self.rows < 12:
                nc = max(self.cols, 12)
                nr = max(self.rows, 12)
                resampled = self._resample_grid(nc, nr)
                self.cols, self.rows = nc, nr
                self.rebuild_orig_mesh()
                self.grid = resampled
                self._mesh_seq += 1
                self._updating_ui = True
                try:
                    self.cols_spin.set_value(nc)
                    self.rows_spin.set_value(nr)
                finally:
                    self._updating_ui = False
            if self.pins:
                self._rebuild_grid_from_pins()
        self.mode = new_mode
        self.active_index = None
        self.active_pin_index = None
        self.hovered_index = None
        self.hovered_pin_index = None
        self._update_mode_ui()
        self.drawing_area.queue_draw()

    def on_preset_changed(self, combo):
        if self.updating_ui_preset or self.mode == self.MODE_PUPPET:
            return
        new_preset = combo.get_active_text()
        if new_preset != "Custom":
            self._push_undo()
        self.current_preset = new_preset
        if self.current_preset != "Custom":
            self.apply_current_preset_to_grid()

    def on_orient_toggled(self, btn):
        if self.mode == self.MODE_PUPPET:
            return
        if self.current_preset != "Custom":
            self._push_undo()
        self.is_vertical_preset = btn.get_active()
        if self.current_preset != "Custom":
            self.apply_current_preset_to_grid()

    def on_preset_param_changed(self, scale):
        self.bend_val   = self.bend_scale.get_value()   / 100.0
        self.h_dist_val = self.h_dist_scale.get_value() / 100.0
        self.v_dist_val = self.v_dist_scale.get_value() / 100.0
        if self.current_preset == "Custom" or self.mode == self.MODE_PUPPET:
            return
        if self._preset_timer is not None:
            GLib.source_remove(self._preset_timer)
        self._preset_timer = GLib.timeout_add(16, self._apply_preset_deferred)

    def _apply_preset_deferred(self):
        self._preset_timer = None
        if self._dead:
            return False
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
        b, hd, vd, iv = (self.bend_val, self.h_dist_val,
                         self.v_dist_val, self.is_vertical_preset)
        for r in range(self.rows + 1):
            vn = r / float(self.rows)
            for c in range(self.cols + 1):
                un = c / float(self.cols)
                uw, vw = evaluate_warp_preset(
                    self.current_preset, un, vn,
                    bend=b, h_dist=hd, v_dist=vd, is_vertical=iv)
                idx = r * (self.cols + 1) + c
                self.grid[2 * idx] = uw * self.width
                self.grid[2 * idx + 1] = vw * self.height
        self._mesh_seq += 1
        self.drawing_area.queue_draw()

    def on_puppet_radius_changed(self, scale):
        self.puppet_radius = scale.get_value()
        if self.mode == self.MODE_PUPPET and self.pins:
            self._rebuild_grid_from_pins()
        else:
            self.drawing_area.queue_draw()

    def on_clear_pins_clicked(self, btn):
        self._push_undo()
        self.pins = []
        if self.mode == self.MODE_PUPPET:
            self._rebuild_grid_from_pins()
        else:
            self.drawing_area.queue_draw()

    # ==================================================================
    # Grid handlers
    # ==================================================================

    def on_cols_changed(self, spin):
        if self._updating_ui:
            return
        nc = int(spin.get_value())
        if nc == self.cols:
            return
        self._push_undo()
        resampled = self._resample_grid(nc, self.rows)
        self.cols = nc
        self.rebuild_orig_mesh()
        self.grid = resampled
        self._mesh_seq += 1
        self.selected_indices.clear()
        self.active_index = None
        self.hovered_index = None
        if self.mode == self.MODE_PUPPET and self.pins:
            self._rebuild_grid_from_pins()
        else:
            self.drawing_area.queue_draw()

    def on_rows_changed(self, spin):
        if self._updating_ui:
            return
        nr = int(spin.get_value())
        if nr == self.rows:
            return
        self._push_undo()
        resampled = self._resample_grid(self.cols, nr)
        self.rows = nr
        self.rebuild_orig_mesh()
        self.grid = resampled
        self._mesh_seq += 1
        self.selected_indices.clear()
        self.active_index = None
        self.hovered_index = None
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
        self._push_undo()
        self.set_preset_to_custom()
        if self.pins:
            self.pins = []
        self._smooth_grid()
        self.drawing_area.queue_draw()

    def on_reset_clicked(self, btn):
        self._push_undo()
        self.pins = []
        if self.mode == self.MODE_PUPPET:
            self._rebuild_grid_from_pins()
        else:
            self.set_preset_to_custom()
            self.grid = _copy_grid(self.orig_grid)
            self.selected_indices.clear()
            self._mesh_seq += 1
            self.drawing_area.queue_draw()

    # ==================================================================
    # Viewport
    # ==================================================================

    def screen_to_world(self, sx, sy):
        return ((sx - self.pan_x) / self.zoom, (sy - self.pan_y) / self.zoom)

    def world_to_screen(self, wx, wy):
        return (wx * self.zoom + self.pan_x, wy * self.zoom + self.pan_y)

    def fit_to_view(self):
        alloc = self.drawing_area.get_allocation()
        dw = alloc.width if alloc.width > 50 else 680
        dh = alloc.height if alloc.height > 50 else 500
        margin = 40.0
        aw = max(50.0, dw - margin * 2)
        ah = max(50.0, dh - margin * 2)
        self.zoom = min(aw / self.canvas_w, ah / self.canvas_h, 2.0)
        self.pan_x = (dw - self.canvas_w * self.zoom) / 2.0
        self.pan_y = (dh - self.canvas_h * self.zoom) / 2.0
        if hasattr(self, 'zoom_label'):
            self.zoom_label.set_text(f"{int(round(self.zoom * 100))}%")
        self.drawing_area.queue_draw()

    def set_zoom(self, new_zoom, cx=None, cy=None):
        new_zoom = max(0.05, min(10.0, new_zoom))
        if cx is None or cy is None:
            alloc = self.drawing_area.get_allocation()
            cx, cy = alloc.width / 2.0, alloc.height / 2.0
        self.pan_x = cx - (cx - self.pan_x) * (new_zoom / self.zoom)
        self.pan_y = cy - (cy - self.pan_y) * (new_zoom / self.zoom)
        self.zoom = new_zoom
        self.zoom_label.set_text(f"{int(round(self.zoom * 100))}%")
        self.drawing_area.queue_draw()

    # ==================================================================
    # Fast vectorized hit test
    # ==================================================================

    def find_vertex_at_screen(self, sx, sy, hit_radius=10.0):
        if self.mode != self.MODE_GRID or self.grid is None or len(self.grid) == 0:
            return None
        g = self.grid
        z, ox, oy = self.zoom, self.pan_x + self.pad_left * self.zoom, self.pan_y + self.pad_top * self.zoom
        best = None
        best_d = hit_radius * hit_radius
        for i in range(len(g) // 2):
            dx = g[2 * i] * z + ox - sx
            dy = g[2 * i + 1] * z + oy - sy
            d = dx * dx + dy * dy
            if d <= best_d:
                best_d = d
                best = i
        return best

    def find_pin_at_screen(self, sx, sy, hit_radius=14.0):
        if self.mode != self.MODE_PUPPET:
            return None
        best = None
        best_dsq = hit_radius * hit_radius
        for i, pin in enumerate(self.pins):
            px, py = self.world_to_screen(pin.dx + self.pad_left, pin.dy + self.pad_top)
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
        if self.symmetry_mode == 2:
            return (self.rows - r) * (self.cols + 1) + c
        return None

    # ==================================================================
    # Mouse & Drag Coalescing
    # ==================================================================

    def _prepare_grid_drag(self):
        n = len(self.grid) // 2
        direct_w = [0.0] * n
        start = self.drag_start_grid
        ax, ay = start[2 * self.active_index], start[2 * self.active_index + 1]
        radius = self.falloff_radius

        for i in range(n):
            if self.lock_boundary and self.is_boundary_vertex(i):
                continue
            if i in self.selected_indices:
                direct_w[i] = 1.0
            elif self.use_falloff:
                ox, oy = start[2 * i], start[2 * i + 1]
                d = math.hypot(ox - ax, oy - ay)
                if d <= radius:
                    direct_w[i] = 0.5 * (1.0 + math.cos(math.pi * d / radius))

        if self.symmetry_mode == 0:
            self._drag_active = [(i, w, w) for i, w in enumerate(direct_w) if w != 0.0]
            return

        dx_mul = [0.0] * n
        dy_mul = [0.0] * n
        ac = self.active_index % (self.cols + 1)
        ar = self.active_index // (self.cols + 1)

        for i in range(n):
            m = self.get_mirror_vertex_index(i)
            if m is None or m >= n or m == i:
                dx_mul[i] = direct_w[i]
                dy_mul[i] = direct_w[i]
            elif i < m:
                wi, wm = direct_w[i], direct_w[m]
                if wi == 0.0 and wm == 0.0:
                    continue
                if wi > wm:
                    drv, mir, w = i, m, wi
                elif wm > wi:
                    drv, mir, w = m, i, wm
                else:
                    if self.symmetry_mode == 1:
                        ci = i % (self.cols + 1)
                        same_side = (ci <= self.cols / 2.0) == (ac <= self.cols / 2.0)
                    else:
                        ri = i // (self.cols + 1)
                        same_side = (ri <= self.rows / 2.0) == (ar <= self.rows / 2.0)
                    drv, mir, w = (i, m, wi) if same_side else (m, i, wm)

                dx_mul[drv] = w
                dy_mul[drv] = w
                if not (self.lock_boundary and self.is_boundary_vertex(mir)):
                    if self.symmetry_mode == 1:
                        dx_mul[mir] = -w
                        dy_mul[mir] =  w
                    else:
                        dx_mul[mir] =  w
                        dy_mul[mir] = -w

        self._drag_active = [(i, dx_mul[i], dy_mul[i]) for i in range(n)
                             if dx_mul[i] != 0.0 or dy_mul[i] != 0.0]

    def _apply_drag_xy(self, sx, sy):
        if self.is_dragging_pins and self.active_pin_index is not None:
            if not self._drag_pushed:
                self._push_undo()
                self._drag_pushed = True
            wx, wy = self.screen_to_world(sx, sy)
            pin = self.pins[self.active_pin_index]
            pin.dx = wx - self.pad_left
            pin.dy = wy - self.pad_top
            self._rebuild_grid_from_pins()
            return

        if self.is_dragging_points and self.active_index is not None:
            if not self._drag_pushed:
                self._push_undo()
                self._drag_pushed = True
            cur_wx, cur_wy = self.screen_to_world(sx, sy)
            dx = cur_wx - self.drag_mouse_start[0]
            dy = cur_wy - self.drag_mouse_start[1]
            g = self.grid
            s = self.drag_start_grid
            for i, mx, my in (self._drag_active or ()):
                g[2 * i] = s[2 * i] + mx * dx
                g[2 * i + 1] = s[2 * i + 1] + my * dy
            self._mesh_seq += 1
            self.drawing_area.queue_draw()

    def _flush_drag(self):
        self._drag_timer = None
        if self._dead:
            return False
        xy = self._pending_drag_xy
        self._pending_drag_xy = None
        if xy is not None:
            self._last_drag_t = time.monotonic()
            self._apply_drag_xy(xy[0], xy[1])
        return False

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
                self._push_undo()
                del self.pins[hit]
                self.active_pin_index = None
                self.hovered_pin_index = None
                self._rebuild_grid_from_pins()
            return True
        if event.button == 1:
            hit = self.find_pin_at_screen(event.x, event.y)
            is_shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            if hit is None:
                wx, wy = self.screen_to_world(event.x, event.y)
                wx -= self.pad_left
                wy -= self.pad_top
                margin = 200.0
                if (wx < -margin or wx > self.width + margin or
                        wy < -margin or wy > self.height + margin):
                    return True
                self._push_undo()
                self.pins.append(Pin(wx, wy))
                self.active_pin_index = len(self.pins) - 1
                self._drag_pushed = True
                if not is_shift:
                    self.is_dragging_pins = True
                self._rebuild_grid_from_pins()
                return True
            self.active_pin_index = hit
            self.hovered_pin_index = hit
            self.is_dragging_pins = True
            self._drag_pushed = False
            self.drawing_area.queue_draw()
            return True
        return False

    def _on_grid_press(self, event):
        if event.button == 3:
            hit = self.find_vertex_at_screen(event.x, event.y, 12.0)
            if hit is not None:
                self._push_undo()
                self.set_preset_to_custom()
                self._reset_vertex(hit)
                mirror = self.get_mirror_vertex_index(hit)
                if mirror is not None and mirror < self._nv():
                    self._reset_vertex(mirror)
                self._mesh_seq += 1
                self.drawing_area.queue_draw()
            return True
        if event.button == 1:
            hit = self.find_vertex_at_screen(event.x, event.y, 10.0)
            is_shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            if hit is not None:
                if self.lock_boundary and self.is_boundary_vertex(hit):
                    return True
                if self.pins:
                    self.pins = []
                    self.hovered_pin_index = None
                self.set_preset_to_custom()
                if is_shift:
                    if hit in self.selected_indices:
                        self.selected_indices.remove(hit)
                    else:
                        self.selected_indices.add(hit)
                elif hit not in self.selected_indices:
                    self.selected_indices = {hit}
                self.active_index = hit
                self.is_dragging_points = True
                self._drag_pushed = False
                wx, wy = self.screen_to_world(event.x, event.y)
                self.drag_mouse_start = (wx, wy)
                self.drag_start_grid = _copy_grid(self.grid)
                self._prepare_grid_drag()
                self.drawing_area.queue_draw()
                return True
            if not is_shift:
                self.selected_indices.clear()
            self.is_box_selecting = True
            wx, wy = self.screen_to_world(event.x, event.y)
            self.box_start = (wx - self.pad_left, wy - self.pad_top)
            self.box_current = (wx - self.pad_left, wy - self.pad_top)
            self.drawing_area.queue_draw()
            return True
        return False

    def on_button_release(self, widget, event):
        if self._drag_timer is not None:
            try:
                GLib.source_remove(self._drag_timer)
            except Exception:
                pass
            self._drag_timer = None

        if self._pending_drag_xy is not None:
            xy = self._pending_drag_xy
            self._pending_drag_xy = None
            self._apply_drag_xy(xy[0], xy[1])

        if event.button == 2 or (event.button == 1 and self.is_panning):
            self.is_panning = False
        if event.button == 1 and self.is_dragging_pins:
            self.is_dragging_pins = False
            self.active_pin_index = None
            self._drag_pushed = False
        if event.button == 1 and self.is_dragging_points:
            self.is_dragging_points = False
            self.active_index = None
            self._drag_pushed = False
            self._drag_active = None
        if event.button == 1 and self.is_box_selecting:
            self.is_box_selecting = False
            bx0 = min(self.box_start[0], self.box_current[0])
            bx1 = max(self.box_start[0], self.box_current[0])
            by0 = min(self.box_start[1], self.box_current[1])
            by1 = max(self.box_start[1], self.box_current[1])
            for i in range(self._nv()):
                if self.lock_boundary and self.is_boundary_vertex(i):
                    continue
                wx, wy = self.grid[2 * i], self.grid[2 * i + 1]
                if bx0 <= wx <= bx1 and by0 <= wy <= by1:
                    self.selected_indices.add(i)
        self.drawing_area.queue_draw()
        return True

    def on_motion_notify(self, widget, event):
        if self.is_panning:
            self.pan_x = event.x - self.pan_start_x
            self.pan_y = event.y - self.pan_start_y
            self.drawing_area.queue_draw()
            return True
        if self.is_box_selecting:
            wx, wy = self.screen_to_world(event.x, event.y)
            self.box_current = (wx - self.pad_left, wy - self.pad_top)
            self.drawing_area.queue_draw()
            return True

        if ((self.is_dragging_pins and self.active_pin_index is not None) or
                (self.is_dragging_points and self.active_index is not None)):
            now = time.monotonic()
            min_dt = max(0.012, min(0.050, self._tick_ms * 0.0011))
            if self._drag_timer is None and (now - self._last_drag_t) >= min_dt:
                self._last_drag_t = now
                self._apply_drag_xy(event.x, event.y)
            else:
                self._pending_drag_xy = (event.x, event.y)
                if self._drag_timer is None:
                    delay = max(12, int((min_dt - (now - self._last_drag_t)) * 1000.0))
                    self._drag_timer = GLib.timeout_add(delay, self._flush_drag)
            return True

        if self.mode == self.MODE_PUPPET:
            hit = self.find_pin_at_screen(event.x, event.y)
            if hit != self.hovered_pin_index:
                self.hovered_pin_index = hit
                self.drawing_area.queue_draw()
        else:
            hit = self.find_vertex_at_screen(event.x, event.y)
            if hit != self.hovered_index:
                self.hovered_index = hit
                self.drawing_area.queue_draw()
        return False

    def on_scroll(self, widget, event):
        if event.direction == Gdk.ScrollDirection.UP:
            self.set_zoom(self.zoom * 1.15, event.x, event.y)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.set_zoom(self.zoom / 1.15, event.x, event.y)
        else:
            return False
        return True

    def _on_dialog_key(self, widget, event):
        key = event.keyval
        focus = self.get_focus()
        if isinstance(focus, Gtk.Entry):
            return False

        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

        if ctrl and key in (Gdk.KEY_z, Gdk.KEY_Z):
            return self._do_redo() if shift else self._do_undo()
        if ctrl and key in (Gdk.KEY_y, Gdk.KEY_Y):
            return self._do_redo()

        if key == Gdk.KEY_space:
            self.space_pressed = True; return True
        if key in (Gdk.KEY_h, Gdk.KEY_H) and not ctrl:
            self.grid_toggle.set_active(not self.grid_toggle.get_active()); return True
        if key in (Gdk.KEY_Delete, Gdk.KEY_BackSpace):
            if self.mode == self.MODE_PUPPET:
                if self.active_pin_index is not None and 0 <= self.active_pin_index < len(self.pins):
                    self._push_undo()
                    del self.pins[self.active_pin_index]
                    self.active_pin_index = None
                    self._rebuild_grid_from_pins()
            elif self.selected_indices:
                self._push_undo()
                self.set_preset_to_custom()
                for idx in self.selected_indices:
                    self._reset_vertex(idx)
                self._mesh_seq += 1
                self.drawing_area.queue_draw()
            return True
        if key == Gdk.KEY_Escape:
            if self.selected_indices:
                self.selected_indices.clear(); self.drawing_area.queue_draw(); return True
            return False
        if key in (Gdk.KEY_plus, Gdk.KEY_equal):       self.set_zoom(self.zoom * 1.25); return True
        if key in (Gdk.KEY_minus, Gdk.KEY_underscore): self.set_zoom(self.zoom / 1.25); return True
        if key == Gdk.KEY_0:
            self.fit_to_view(); return True
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
            self.hovered_index = None; changed = True
        if self.hovered_pin_index is not None:
            self.hovered_pin_index = None; changed = True
        if changed:
            self.drawing_area.queue_draw()
        return False

    # ==================================================================
    # Canvas rendering
    # ==================================================================

    def _make_checker_pattern(self):
        size = 16
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size * 2, size * 2)
        c = cairo.Context(surf)
        c.set_source_rgb(0.85, 0.85, 0.85); c.paint()
        c.set_source_rgb(0.65, 0.65, 0.65)
        c.rectangle(0, 0, size, size)
        c.rectangle(size, size, size, size)
        c.fill()
        p = cairo.SurfacePattern(surf)
        p.set_extend(cairo.EXTEND_REPEAT)
        self._checker_surf = surf
        return p

    def on_draw(self, widget, cr):
        t0 = time.perf_counter()

        alloc = self.drawing_area.get_allocation()
        w, h = alloc.width, alloc.height
        if w < 2 or h < 2:
            return

        is_dragging = (self.is_dragging_points or self.is_dragging_pins
                       or self.is_panning)

        self._ensure_dst_surface(w, h)

        if not self.show_ghost and _HAS_VP_RENDER:
            self._render_warp_c(use_vp=True)
            cr.set_source_surface(self._dst_surface, 0, 0)
            cr.paint()
        else:
            cr.set_source_rgb(0.13, 0.13, 0.14)
            cr.paint()

            cr.save()
            cr.translate(self.pan_x, self.pan_y)
            cr.scale(self.zoom, self.zoom)
            if self.show_checker:
                cr.rectangle(0, 0, self.canvas_w, self.canvas_h)
                cr.clip()
                cr.set_source(self._checker_pattern)
                cr.paint()
            if self.show_ghost:
                self._refresh_preview_surface()
                psurf = self._preview_surface
                cr.scale(self.canvas_w / psurf.get_width(),
                         self.canvas_h / psurf.get_height())
                cr.set_source_surface(psurf, 0, 0)
                cr.paint_with_alpha(0.35)
            cr.restore()

            self._render_warp_c(use_vp=False)
            cr.set_source_surface(self._dst_surface, 0, 0)
            cr.paint()

        cr.save()
        cr.translate(self.pan_x, self.pan_y)
        cr.scale(self.zoom, self.zoom)
        if self.show_grid:
            self.draw_mesh_overlay(cr, fast=is_dragging)
        if self.mode == self.MODE_PUPPET:
            self.draw_puppet_pins(cr)
        cr.restore()

        if self.is_box_selecting:
            self.draw_marquee_box(cr)

        dt_ms = (time.perf_counter() - t0) * 1000.0
        self._tick_ms = 0.65 * self._tick_ms + 0.35 * max(1.0, dt_ms)

        if self._diag:
            print(f"[mesh-warp] frame={dt_ms:5.1f}ms  zoom={self.zoom:.2f}  "
                  f"preview={self._preview_surface.get_width()}x"
                  f"{self._preview_surface.get_height()}", file=sys.stderr)

    def _get_cached_grid_path(self):
        key = (self._mesh_seq, self.cols, self.rows)
        cache = self._mesh_path_cache
        if cache is not None and cache[0] == key:
            return cache[1]

        tmp_surf = cairo.ImageSurface(cairo.FORMAT_A8, 1, 1)
        tmp_cr = cairo.Context(tmp_surf)
        cols, rows = self.cols, self.rows
        g = self.grid

        for r in range(rows + 1):
            base = r * (cols + 1)
            tmp_cr.move_to(g[2 * base], g[2 * base + 1])
            for c in range(1, cols + 1):
                tmp_cr.line_to(g[2 * (base + c)], g[2 * (base + c) + 1])

        for c in range(cols + 1):
            tmp_cr.move_to(g[2 * c], g[2 * c + 1])
            for r in range(1, rows + 1):
                idx = r * (cols + 1) + c
                tmp_cr.line_to(g[2 * idx], g[2 * idx + 1])

        path = tmp_cr.copy_path()
        tmp_surf.finish()
        self._mesh_path_cache = (key, path)
        return path

    def draw_mesh_overlay(self, cr, fast=False):
        line_w = max(1.0, 1.2 / self.zoom)
        cr.save()
        cr.translate(self.pad_left, self.pad_top)
        alpha = 0.25 if self.mode == self.MODE_PUPPET else 0.9

        grid_path = self._get_cached_grid_path()

        if not fast:
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.55)
            cr.set_line_width(line_w * 2.4)
            cr.new_path()
            cr.append_path(grid_path)
            cr.stroke()

        cr.set_source_rgba(0.1, 0.85, 1.0, alpha)
        cr.set_line_width(line_w)
        cr.new_path()
        cr.append_path(grid_path)
        cr.stroke()

        if (self.mode == self.MODE_GRID and self.use_falloff and
                (self.active_index is not None or self.hovered_index is not None)):
            ci = self.active_index if self.active_index is not None else self.hovered_index
            cx, cy = self.grid[2 * ci], self.grid[2 * ci + 1]
            cr.save()
            cr.set_source_rgba(1.0, 0.8, 0.1, 0.3)
            cr.set_line_width(max(1.0, 1.5 / self.zoom))
            cr.set_dash([4.0 / self.zoom, 4.0 / self.zoom])
            cr.arc(cx, cy, self.falloff_radius, 0, 2 * math.pi)
            cr.stroke()
            cr.restore()

        if self.mode == self.MODE_GRID:
            radius = max(3.5, 5.0 / self.zoom)
            if fast:
                if self.active_index is not None:
                    gx, gy = self.grid[2 * self.active_index], self.grid[2 * self.active_index + 1]
                    cr.set_source_rgba(0.0, 0.0, 0.0, 0.75)
                    cr.arc(gx, gy, radius + 1.5 / self.zoom, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgb(0.2, 1.0, 0.3)
                    cr.arc(gx, gy, radius, 0, 2 * math.pi)
                    cr.fill()
            else:
                for i in range(self._nv()):
                    gx, gy = self.grid[2 * i], self.grid[2 * i + 1]
                    is_sel = i in self.selected_indices
                    is_act = (i == self.active_index)
                    is_hov = (i == self.hovered_index)
                    is_bnd = self.is_boundary_vertex(i)
                    cr.set_source_rgba(0.0, 0.0, 0.0, 0.75)
                    cr.arc(gx, gy, radius + 1.5 / self.zoom, 0, 2 * math.pi)
                    cr.fill()
                    if is_act:      cr.set_source_rgb(0.2, 1.0, 0.3)
                    elif is_sel:    cr.set_source_rgb(1.0, 0.7, 0.0)
                    elif is_hov:    cr.set_source_rgb(0.2, 0.9, 1.0)
                    elif is_bnd and self.lock_boundary:
                                    cr.set_source_rgb(0.5, 0.5, 0.5)
                    else:           cr.set_source_rgb(1.0, 1.0, 1.0)
                    cr.arc(gx, gy, radius, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgba(0.0, 0.0, 0.0, 0.7)
                    cr.arc(gx, gy, radius * 0.35, 0, 2 * math.pi)
                    cr.fill()
        cr.restore()

    def draw_puppet_pins(self, cr):
        cr.save()
        cr.translate(self.pad_left, self.pad_top)
        pin_radius = max(5.0, 8.0 / self.zoom)
        inner = pin_radius * 0.45
        for i, pin in enumerate(self.pins):
            dx, dy = pin.dx, pin.dy
            sx, sy = pin.sx, pin.sy
            if abs(dx - sx) > 0.5 or abs(dy - sy) > 0.5:
                cr.save()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
                cr.set_line_width(max(1.0, 1.2 / self.zoom))
                cr.set_dash([4.0 / self.zoom, 4.0 / self.zoom])
                cr.move_to(sx, sy); cr.line_to(dx, dy); cr.stroke()
                cr.restore()
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.35)
                cr.arc(sx, sy, pin_radius * 0.4, 0, 2 * math.pi); cr.fill()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.85)
            cr.arc(dx, dy, pin_radius + 1.5 / self.zoom, 0, 2 * math.pi)
            cr.fill()

            if i == self.active_pin_index:    cr.set_source_rgb(0.2, 1.0, 0.3)
            elif i == self.hovered_pin_index: cr.set_source_rgb(0.2, 0.9, 1.0)
            else:                             cr.set_source_rgb(1.0, 0.9, 0.2)
            cr.arc(dx, dy, pin_radius, 0, 2 * math.pi); cr.fill()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.85)
            cr.set_line_width(max(1.0, 1.2 / self.zoom))
            cr.move_to(dx - inner, dy); cr.line_to(dx + inner, dy)
            cr.move_to(dx, dy - inner); cr.line_to(dx, dy + inner)
            cr.stroke()

            cr.set_source_rgba(0.0, 0.0, 0.0, 0.7)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            cr.set_font_size(max(9.0, 11.0 / self.zoom))
            cr.move_to(dx + pin_radius * 1.3, dy - pin_radius * 0.8)
            cr.show_text(str(i + 1))
        cr.restore()

    def draw_marquee_box(self, cr):
        sx0, sy0 = self.world_to_screen(self.box_start[0] + self.pad_left,
                                        self.box_start[1] + self.pad_top)
        sx1, sy1 = self.world_to_screen(self.box_current[0] + self.pad_left,
                                        self.box_current[1] + self.pad_top)
        x, y = min(sx0, sx1), min(sy0, sy1)
        w, h = abs(sx1 - sx0), abs(sy1 - sy0)
        cr.save()
        cr.set_source_rgba(0.1, 0.6, 1.0, 0.2)
        cr.rectangle(x, y, w, h); cr.fill()
        cr.set_source_rgba(0.1, 0.8, 1.0, 0.9)
        cr.set_line_width(1.0); cr.set_dash([4.0, 3.0])
        cr.rectangle(x, y, w, h); cr.stroke()
        cr.restore()

    # ==================================================================
    # Apply
    # ==================================================================

    def apply_warp_to_drawable(self):
        self.image.undo_group_start()
        try:
            try:
                if hasattr(self.drawable, "has_alpha") and not self.drawable.has_alpha():
                    self.drawable.add_alpha()
            except Exception:
                pass

            gxs = self.grid[0::2]
            gys = self.grid[1::2]
            gmin_x, gmax_x = float(min(gxs)), float(max(gxs))
            gmin_y, gmax_y = float(min(gys)), float(max(gys))

            pad_left, pad_top = self.pad_left, self.pad_top
            cur_w = self.drawable.get_width()
            cur_h = self.drawable.get_height()

            if self.expand_layer:
                wl = max(0, int(math.ceil(-gmin_x)))
                wt = max(0, int(math.ceil(-gmin_y)))
                wr = max(0, int(math.ceil(gmax_x - self.width)))
                wb = max(0, int(math.ceil(gmax_y - self.height)))

                cur_r = max(0, cur_w - self.width - pad_left)
                cur_b = max(0, cur_h - self.height - pad_top)

                add_l = max(0, wl - pad_left)
                add_t = max(0, wt - pad_top)
                add_r = max(0, wr - cur_r)
                add_b = max(0, wb - cur_b)

                if add_l or add_t or add_r or add_b:
                    self.drawable.resize(cur_w + add_l + add_r,
                                         cur_h + add_t + add_b,
                                         add_l, add_t)
                    pad_left += add_l
                    pad_top  += add_t
                    self.pad_left, self.pad_top = pad_left, pad_top

            cur_w = self.drawable.get_width()
            cur_h = self.drawable.get_height()

            padded = [[gxs[i] + pad_left, gys[i] + pad_top] for i in range(len(gxs))]
            grid_json = json.dumps(padded)
            mode_str = "puppet" if self.mode == self.MODE_PUPPET else "grid"
            pins_json = json.dumps([p.to_list() for p in self.pins])

            kw = {
                "cols": self.cols, "rows": self.rows,
                "grid-json": grid_json,
                "pad-left": pad_left, "pad-top": pad_top,
                "orig-width": self.orig_width, "orig-height": self.orig_height,
                "pins-json": pins_json, "mode": mode_str,
            }

            if self.existing_mesh_warp_filter is not None:
                try:
                    cfg = self.existing_mesh_warp_filter.get_config()
                    for k, v in kw.items():
                        cfg.set_property(k, v)
                    self.existing_mesh_warp_filter.update()
                except Exception as e:
                    Gimp.message(f"Mesh Warp: could not update filter ({e}); recreating.")
                    self.existing_mesh_warp_filter = None

            if self.existing_mesh_warp_filter is None:
                filt = Gimp.DrawableFilter.new(self.drawable, GEGL_OP_NAME, GEGL_OP_LABEL)
                cfg = filt.get_config()
                for k, v in kw.items():
                    cfg.set_property(k, v)
                self.drawable.append_filter(filt)
                self.existing_mesh_warp_filter = filt

            self.drawable.update(0, 0, cur_w, cur_h)
        finally:
            self.image.undo_group_end()
            Gimp.displays_flush()


# ==============================================================================
# Plugin entry
# ==============================================================================

class MeshWarpPlugin(Gimp.PlugIn):
    def do_set_i18n(self, procname):
        return True, 'gimp30-python', None

    def do_query_procedures(self):
        return [PLUG_IN_PROC]

    def do_create_procedure(self, name):
        if name != PLUG_IN_PROC:
            return None
        p = Gimp.ImageProcedure.new(self, name, Gimp.PDBProcType.PLUGIN, self.run, None)
        p.set_image_types("RGB*, GRAY*, INDEXED*")
        p.set_sensitivity_mask(
            Gimp.ProcedureSensitivityMask.DRAWABLE |
            Gimp.ProcedureSensitivityMask.DRAWABLES)
        p.set_menu_label("_Mesh Warp...")
        p.add_menu_path("<Image>/Filters/Deform")
        p.add_menu_path("<Image>/Tools/Transform Tools")
        p.set_attribution("Hex", "Hex", "2026")
        p.set_documentation(
            "Interactive Mesh Warp with Presets & Puppet Pins",
            "Interactively deform layers using a mesh-warp grid or puppet pins. "
            "Warp math runs in C with OpenMP; Python handles only the UI.",
            PLUG_IN_PROC)
        return p

    def run(self, procedure, run_mode, image, drawables, config, data):
        if run_mode == Gimp.RunMode.NONINTERACTIVE:
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        if len(drawables) != 1 or not isinstance(drawables[0], Gimp.Drawable):
            Gimp.message("Mesh Warp requires exactly one active layer.")
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        if _core is None:
            Gimp.message("Mesh Warp: native core missing.\n%s" % _CORE_ERROR)
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        try:
            GimpUi.init(PLUG_IN_BINARY)
        except Exception as e:
            traceback.print_exc()
            Gimp.message(f"Mesh Warp UI init error: {e}")
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        try:
            dlg = MeshWarpDialog(image, drawables[0])
        except Exception as e:
            traceback.print_exc()
            Gimp.message(f"Mesh Warp Error:\n{e}\n\n{traceback.format_exc()}")
            return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)

        GimpUi.window_set_transient(dlg)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK:
            try:
                dlg.apply_warp_to_drawable()
            except Exception as e:
                traceback.print_exc()
                Gimp.message(f"Mesh Warp Apply Error: {e}")
            dlg.destroy()
            return procedure.new_return_values(Gimp.PDBStatusType.SUCCESS, None)
        dlg.destroy()
        return procedure.new_return_values(Gimp.PDBStatusType.CANCEL, None)


if __name__ == '__main__':
    Gimp.main(MeshWarpPlugin.__gtype__, sys.argv)
