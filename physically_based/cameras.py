"""Cameras: presets that set sensor size, focal length and render resolution."""

import bpy
import math
from numbers import Number

from .common import Database, ui_refresh, ui_status, ui_card, ui_kv

API_URLS = [
    "https://api.physicallybased.info/v2/cameras",
    "https://raw.githubusercontent.com/AntonPalmqvist/physically-based-api/main/deploy/v2/cameras.json",
]
FULL_FRAME_DIAG = math.hypot(36.0, 24.0)  # 43.27 mm, reference for the crop factor
SENSOR_RES_HEIGHT = 1080                  # fixed height for the proportional resolution

_PRESETS = []
_ENUM = []      # persistent references to the dynamic enum items
_MUTE = False   # True while a property is set from code: don't apply anything


# ---------- parsing ----------
def _nums(v):
    if isinstance(v, (list, tuple)):
        return [float(x) for x in v if isinstance(x, Number) and not isinstance(x, bool)]
    if isinstance(v, Number) and not isinstance(v, bool):
        return [float(v)]
    return []


def parse_all(m):
    """One preset for each sensor (e.g. main / wide / tele lens)."""
    make, model = m.get("make", ""), m.get("model", m.get("name", "?"))
    base = f"{make} {model}".strip()
    desc = m.get("description") if isinstance(m.get("description"), str) else ""
    sensors = m.get("sensorSize") or []
    if isinstance(sensors, dict):
        sensors = [sensors]
    out = []
    for s in sensors:
        if not isinstance(s, dict):
            continue
        size = _nums(s.get("size"))
        if len(size) < 2 or size[0] <= 0 or size[1] <= 0:
            continue
        fmt = s.get("format") or ""
        label = base if len(sensors) == 1 or not fmt else f"{base} – {fmt}"
        res = []
        for r in s.get("resolution") or []:
            if isinstance(r, dict):
                wh = _nums(r.get("size"))
                if len(wh) >= 2:
                    res.append((str(r.get("format") or f"{wh[0]:g}×{wh[1]:g}"), int(wh[0]), int(wh[1])))
        focal = _nums(s.get("focalLength"))
        out.append({
            "name": label,
            "description": desc,
            "width": size[0],
            "height": size[1],
            "focal": focal[0] if focal else None,
            "resolutions": res,
        })
    return out


def crop_factor(p):
    return FULL_FRAME_DIAG / math.hypot(p["width"], p["height"])


# ---------- applying ----------
def _fit(p, scene):
    """The frame must fit inside the sensor, as in the real camera:
    if the render is wider than the sensor, use the full width (crop top/bottom);
    if it's narrower, use the full height (crop the sides)."""
    sensor_aspect = p["width"] / p["height"]
    if scene is None:
        return 'HORIZONTAL' if sensor_aspect >= 1 else 'VERTICAL'
    return _fit_for(_render_aspect(scene), sensor_aspect)


def _render_aspect(scene):
    r = scene.render
    return (r.resolution_x * r.pixel_aspect_x) / max(r.resolution_y * r.pixel_aspect_y, 1e-6)


def _fit_for(render_aspect, sensor_aspect):
    return 'HORIZONTAL' if render_aspect >= sensor_aspect * 0.995 else 'VERTICAL'


def apply_resolution(scene, res):
    r = scene.render
    r.resolution_x, r.resolution_y = res[1], res[2]
    r.pixel_aspect_x = r.pixel_aspect_y = 1.0


def apply_preset(cam_obj, p, scene=None):
    cam = cam_obj.data
    cam.sensor_width = p["width"]
    cam.sensor_height = p["height"]
    cam.sensor_fit = _fit(p, scene)
    cam["pb_aspect"] = p["width"] / p["height"]  # used to re-fit automatically
    if scene is not None:
        cam["pb_render_aspect"] = _render_aspect(scene)
    if p["focal"]:
        cam.lens_unit = 'MILLIMETERS'
        cam.lens = p["focal"]
    cam["pb_preset"] = p["name"]


# ---------- data ----------
def _rebuild_enum():
    _ENUM.clear()
    _PRESETS.clear()
    _RES_CACHE.clear()
    _ENUM.append(("NONE", "— Choose a camera —", ""))
    for raw in DB.items:
        if isinstance(raw, dict):
            for p in parse_all(raw):
                p["make"] = str(raw.get("make") or "Other")
                _PRESETS.append(p)
    last = None
    for i, p in enumerate(_PRESETS):
        if p["make"] != last:  # empty identifier = heading in the menu
            _ENUM.append(("", p["make"], ""))
            last = p["make"]
        lab = f"{p['name']}   ({p['width']:g}×{p['height']:g} mm"
        lab += f", {p['focal']:g} mm)" if p["focal"] else ")"
        _ENUM.append((str(i), lab, p["description"]))


DB = Database("cameras.json", API_URLS, "cameras", _rebuild_enum)


# ---------- properties ----------
def _current(s):
    key = s.preset
    if key.isdigit() and int(key) < len(_PRESETS):
        return _PRESETS[int(key)]
    return None


def _enum_items(self, context):
    return _ENUM or [("NONE", "Database not loaded", "")]


_RES_CACHE = {}
_RES_NONE = [
    ("SENSOR", f"Proportional to the sensor ({SENSOR_RES_HEIGHT} px high)",
     "Fixed height, width from the sensor's aspect ratio"),
    ("KEEP", "Don't change the resolution", ""),
]


def _res_items(self, context):
    # Blender keeps pointers to the strings: each preset's list stays in memory
    p = _current(self)
    if p is None:
        return _RES_NONE
    key = self.preset
    if key not in _RES_CACHE:
        items = list(_RES_NONE)
        for i, (fmt, w, h) in enumerate(p["resolutions"]):
            items.append((str(i), f"{fmt}  ({w}×{h})", ""))
        _RES_CACHE[key] = items
    return _RES_CACHE[key]


def _targets(context):
    cams = [o for o in context.selected_objects if o.type == 'CAMERA']
    ao = context.active_object
    if ao and ao.type == 'CAMERA' and ao not in cams:
        cams.append(ao)
    if not cams and context.scene.camera:
        cams = [context.scene.camera]  # nothing selected: the scene's active camera
    return cams


def _apply_impl(self, context, push_undo):
    p = _current(self)
    if p is None:
        return
    res = None
    if self.resolution == "SENSOR":
        w = round(SENSOR_RES_HEIGHT * p["width"] / p["height"] / 2) * 2  # even: many codecs need it
        res = ("sensor", int(w), SENSOR_RES_HEIGHT)
    elif self.resolution.isdigit() and int(self.resolution) < len(p["resolutions"]):
        res = p["resolutions"][int(self.resolution)]
    if res is not None:
        apply_resolution(context.scene, res)
    cams = [o for o in _targets(context) if not o.data.library]
    for o in cams:
        apply_preset(o, p, context.scene)
    if push_undo and (cams or res is not None):
        try:
            bpy.ops.ed.undo_push(message=f"Camera preset: {p['name']}")
        except Exception:
            pass


def _apply(self, context):
    # Blender update callback: must take exactly 2 arguments
    if not _MUTE:
        _apply_impl(self, context, True)


def _set_muted(state, name, value):
    """Set a property without running its update callback."""
    global _MUTE
    _MUTE = True
    try:
        setattr(state, name, value)
    except Exception:
        pass
    finally:
        _MUTE = False


def _on_preset(self, context):
    if _MUTE:
        return
    # "Proportional" and "Don't change" are kept; a database format depends on the
    # previous camera, so go back to "Proportional"
    try:
        keep = self.resolution == "KEEP"
    except Exception:
        keep = False
    _set_muted(self, "resolution", "KEEP" if keep else "SENSOR")
    _apply(self, context)


class PBC_State(bpy.types.PropertyGroup):
    preset: bpy.props.EnumProperty(name="Camera", items=_enum_items, update=_on_preset,
                                   description="Apply sensor and focal length to the selected cameras")
    resolution: bpy.props.EnumProperty(name="Resolution", items=_res_items, update=_apply,
                                       description="Set the scene's render resolution")


# ---------- operators / panels ----------
class PBC_OT_fetch(bpy.types.Operator):
    bl_idname = "pbc.fetch"
    bl_label = "Update Database"
    bl_description = "Download the cameras from physicallybased.info"

    def execute(self, context):
        DB.start_update()
        return {'FINISHED'}


class PBC_OT_reapply(bpy.types.Operator):
    bl_idname = "pbc.reapply"
    bl_label = "Reapply"
    bl_description = "Reapply the preset to the selected cameras"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _current(context.window_manager.pbc_state) is not None

    def execute(self, context):
        _apply_impl(context.window_manager.pbc_state, context, False)
        return {'FINISHED'}


def _draw(layout, context, show_info):
    s = context.window_manager.pbc_state
    col = layout.column()
    ui_status(col, DB)
    if not _targets(context):
        row = col.row()
        row.alert = True
        row.label(text="No camera in the scene", icon='ERROR')
    row = col.row(align=True)
    row.scale_y = 1.3
    row.prop(s, "preset", text="", icon='CAMERA_DATA')
    row.operator("pbc.reapply", text="", icon='LOOP_BACK')
    p = _current(s)
    if p:
        col.prop(s, "resolution", text="", icon='FULLSCREEN_ENTER')
    if p and show_info:
        card = ui_card(col, p["name"], 'CAMERA_DATA')
        cf = crop_factor(p)
        ui_kv(card, "Sensor", f"{p['width']:g} × {p['height']:g} mm")
        ui_kv(card, "Crop Factor", f"{cf:.2f}")
        ui_kv(card, "Aspect", f"{p['width'] / p['height']:.2f} : 1")
        if p["focal"]:
            ui_kv(card, "Focal Length", f"{p['focal']:g} mm")
            ui_kv(card, "35 mm Equiv.", f"{p['focal'] * cf:.0f} mm")
    cam = getattr(context, "camera", None) or (context.scene.camera.data if context.scene.camera else None)
    if cam is not None and "pb_aspect" in cam:
        ra, sa = _render_aspect(context.scene), cam["pb_aspect"]
        card = ui_card(col, "Framing", 'SELECT_SET')
        ui_kv(card, "Render", f"{ra:.2f} : 1")
        ui_kv(card, "Sensor", f"{sa:.2f} : 1")
        horiz = _fit_for(ra, sa) == 'HORIZONTAL'
        ui_kv(card, "Fit", "Horizontal" if horiz else "Vertical")
        hint = card.row()
        hint.active = False
        hint.label(text="Render is wider: top/bottom cropped" if horiz
                   else "Render is narrower: sides cropped")


class PBC_PT_camera(bpy.types.Panel):
    bl_label = "Physically Based"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return context.camera is not None

    def draw_header(self, context):
        self.layout.label(text="", icon='CAMERA_DATA')

    def draw_header_preset(self, context):
        ui_refresh(self.layout, DB, "pbc.fetch")

    def draw(self, context):
        _draw(self.layout, context, True)


class PBC_PT_view3d(bpy.types.Panel):
    bl_label = "Physically Based Cameras"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PhysBased"

    def draw_header(self, context):
        self.layout.label(text="", icon='CAMERA_DATA')

    def draw_header_preset(self, context):
        ui_refresh(self.layout, DB, "pbc.fetch")

    def draw(self, context):
        _draw(self.layout, context, True)


classes = (PBC_State, PBC_OT_fetch, PBC_OT_reapply, PBC_PT_camera, PBC_PT_view3d)


_LAST_ASPECT = {}


@bpy.app.handlers.persistent
def _sync_fit(scene, depsgraph=None):
    """If you change the resolution by hand, the sensor crop stays correct (no Reapply)."""
    try:
        aspect = _render_aspect(scene)
        key = scene.name_full
    except Exception:
        return
    # the handler runs on every scene change (even moving objects): if the resolution
    # hasn't changed, return right away without looping over the objects
    if abs(_LAST_ASPECT.get(key, -1.0) - aspect) < 1e-6:
        return
    _LAST_ASPECT[key] = aspect
    # only this scene's cameras: with several scenes at different resolutions sharing
    # a camera, looping over bpy.data.cameras would ping-pong forever
    seen = set()
    for ob in scene.objects:
        if ob.type != 'CAMERA' or ob.data is None or ob.data.name_full in seen:
            continue
        cam = ob.data
        seen.add(cam.name_full)
        sa = cam.get("pb_aspect")
        if sa is None or cam.library:
            continue
        # only if the resolution changed since last time: a fit chosen by hand is kept
        if abs(cam.get("pb_render_aspect", 0.0) - aspect) < 1e-4:
            continue
        cam["pb_render_aspect"] = aspect
        fit = _fit_for(aspect, sa)
        if cam.sensor_fit != fit:
            cam.sensor_fit = fit


@bpy.app.handlers.persistent
def _on_load_post(*_):
    _LAST_ASPECT.clear()
    # back to "Choose a camera" without applying anything
    try:
        st = bpy.context.window_manager.pbc_state
    except Exception:
        return
    _set_muted(st, "preset", "NONE")
    _set_muted(st, "resolution", "SENSOR")


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.pbc_state = bpy.props.PointerProperty(type=PBC_State)
    bpy.app.handlers.depsgraph_update_post.append(_sync_fit)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    for lst, fn in ((bpy.app.handlers.depsgraph_update_post, _sync_fit),
                    (bpy.app.handlers.load_post, _on_load_post)):
        if fn in lst:
            lst.remove(fn)
    DB.stop()
    del bpy.types.WindowManager.pbc_state
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
