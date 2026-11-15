"""Lights: presets that set color (from temperature, in ACEScg), intensity, type and size."""

import bpy
import math
from numbers import Number

from .common import Database, ui_refresh, ui_status, ui_card, ui_kv

API_URLS = [
    "https://api.physicallybased.info/v2/lightsources",
    "https://raw.githubusercontent.com/AntonPalmqvist/physically-based-api/main/deploy/v2/lightsources.json",
]
LM_PER_W = 683.0            # photometric convention (lumens per watt) used for Blender
DEFAULT_SPOT_ANGLE = 120.0  # degrees, used when the database has no angle
SUN_ANGLE = 0.53            # angular diameter of the Sun and the Moon (degrees)
_Y_ACESCG = (0.2722287, 0.6740818, 0.0536895)  # luminance in ACEScg

_PRESETS = []
_ENUM = []  # persistent reference to the items (avoids a Blender bug with dynamic enums)
_MUTE = False   # True while the preset is reset from code: don't apply anything


# ---------- tolerant parsing ----------
def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, Number):
        return float(v)
    if isinstance(v, (list, tuple)):
        for x in v:
            r = _num(x)
            if r is not None:
                return r
    elif isinstance(v, dict):
        for k in ("value", "values"):
            if k in v:
                r = _num(v[k])
                if r is not None:
                    return r
        for x in v.values():
            r = _num(x)
            if r is not None:
                return r
    return None


def _vec3(v):
    if isinstance(v, (list, tuple)):
        if len(v) >= 3 and all(isinstance(x, Number) for x in v[:3]):
            return tuple(float(x) for x in v[:3])
        for x in v:
            r = _vec3(x)
            if r:
                return r
    elif isinstance(v, dict):
        keys = list(v)
        lin = [k for k in keys if "linear" in k.lower() and "p3" not in k.lower()]
        for k in lin + ["color", "value", "values"] + keys:
            if k in v:
                r = _vec3(v[k])
                if r:
                    return r
    return None


def _pick(v):
    """Database list: [value] or [min, max, default] -> value to use."""
    if isinstance(v, (list, tuple)):
        nums = [float(x) for x in v if isinstance(x, Number) and not isinstance(x, bool)]
        if not nums:
            return None
        if len(nums) == 3:
            return nums[2]          # default
        if len(nums) == 2:
            return sum(nums) / 2    # range only: middle value
        return nums[0]
    return _num(v)


def _first_str(v):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return str(v).lower() if v is not None else ""


def _unit(u):
    u = _first_str(u).replace(" ", "").replace("²", "2")
    if "cd" in u or "nit" in u:
        return "cd/m2"
    if u in ("lx", "lux"):
        return "lx"
    if u in ("w", "watt", "watts"):
        return "W"
    return "lm"


def parse_all(m):
    """One preset for each variant of the light source."""
    name = m.get("name", "?")
    desc = m.get("description") if isinstance(m.get("description"), str) else ""
    unit = _unit(m.get("unit"))
    ltype = _first_str(m.get("type"))
    variants = m.get("variants") or [m]
    out = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        fmt = v.get("format") or ""
        label = name if len(variants) == 1 or not fmt or fmt == name else f"{name} – {fmt}"
        size = v.get("sourceSize")
        size = [float(x) for x in size if isinstance(x, Number)] if isinstance(size, list) else []
        out.append({
            "name": label,
            "base": name,
            "description": desc,
            "unit": unit,
            "ltype": ltype,
            "value": _pick(v.get("intensity")),
            "temperature": _pick(v.get("temperature")),
            "color": _vec3(v.get("color")),
            "angle": _num(v.get("angle")),
            "size_cm": size,
        })
    return out


# ---------- color: black body -> ACEScg ----------
_XYZ_TO_ACESCG = (
    (1.6410234, -0.3248033, -0.2364247),
    (-0.6636629, 1.6153316, 0.0167563),
    (0.0117219, -0.0082844, 0.9883949),
)
_REC709_TO_ACESCG = (
    (0.613097, 0.339523, 0.047379),
    (0.070194, 0.916354, 0.013452),
    (0.020616, 0.109570, 0.869815),
)


def _mul(mtx, v):
    return tuple(sum(a * b for a, b in zip(row, v)) for row in mtx)


def _norm(rgb):
    rgb = tuple(max(c, 0.0) for c in rgb)
    mx = max(rgb) or 1.0
    return tuple(c / mx for c in rgb)


def _g(x, mu, s1, s2):
    return math.exp(-0.5 * ((x - mu) / (s1 if x < mu else s2)) ** 2)


def _cmf(l):
    """CIE 1931 2° (analytic fit by Wyman et al. 2013), l in nm."""
    x = 1.056 * _g(l, 599.8, 37.9, 31.0) + 0.362 * _g(l, 442.0, 16.0, 26.7) - 0.065 * _g(l, 501.1, 20.4, 26.2)
    y = 0.821 * _g(l, 568.8, 46.9, 40.5) + 0.286 * _g(l, 530.9, 16.3, 31.1)
    z = 1.217 * _g(l, 437.0, 11.8, 36.0) + 0.681 * _g(l, 459.0, 26.0, 13.8)
    return x, y, z


def kelvin_to_acescg(t):
    """Planck spectrum integrated with the CIE CMFs -> ACEScg, normalized to max = 1."""
    t = min(max(t, 500.0), 40000.0)
    X = Y = Z = 0.0
    for l in range(380, 781, 5):
        lm = l * 1e-9
        b = 1.0 / (lm ** 5 * (math.exp(1.4388e-2 / (lm * t)) - 1.0))
        cx, cy, cz = _cmf(l)
        X += b * cx
        Y += b * cy
        Z += b * cz
    return _norm(_mul(_XYZ_TO_ACESCG, (X / Y, 1.0, Z / Y)))


def _lum(rgb):
    """Luminance of the color: real lumens already include the light's color."""
    return max(sum(a * b for a, b in zip(_Y_ACESCG, rgb)), 1e-4)


def is_sky(p):
    # only the "Sky" preset (and its variants), not names like "Arri SkyPanel"
    base = p.get("base", p["name"]).strip().lower()
    return p["ltype"] in ("sky", "environment", "dome", "world") or base == "sky"


def preset_color(p):
    if p["temperature"]:
        return kelvin_to_acescg(p["temperature"])
    if p["color"]:
        return _norm(_mul(_REC709_TO_ACESCG, p["color"]))
    return (1.0, 1.0, 1.0)


def apply_world(scene, p):
    """Sky: horizontal illuminance E = π·L, so strength = E / (π · 683 · Y)."""
    world = scene.world
    if world is None or world.library is not None:   # linked worlds can't be edited
        world = bpy.data.worlds.new("World")
        scene.world = world
    if not getattr(world, "use_nodes", True):          # deprecated in Blender 5.0
        world.use_nodes = True
    bg = next((n for n in world.node_tree.nodes if n.type == 'BACKGROUND'), None)
    if bg is None:
        bg = world.node_tree.nodes.new("ShaderNodeBackground")
        out = next((n for n in world.node_tree.nodes if n.type == 'OUTPUT_WORLD'), None) \
            or world.node_tree.nodes.new("ShaderNodeOutputWorld")
        world.node_tree.links.new(bg.outputs[0], out.inputs["Surface"])
    col = preset_color(p)
    bg.inputs["Color"].default_value = (*col, 1.0)
    if p["value"] is not None:
        bg.inputs["Strength"].default_value = p["value"] / (math.pi * LM_PER_W * _lum(col))
    world["pb_preset"] = p["name"]
    world["pb_intensity"] = f"{p['value']:g} lx" if p["value"] is not None else ""


# ---------- applying to a light ----------
def apply_preset(obj, p):
    light = obj.data
    unit, val, lt = p["unit"], p["value"], p["ltype"]

    # light type: from the database, consistent with the unit
    if unit == "lx" or lt in ("sun", "directional"):
        new_type = 'SUN'
    elif unit == "cd/m2" or lt in ("surface", "area"):
        new_type = 'AREA'
    elif lt == "spot":
        new_type = 'SPOT'
    elif lt == "point" or light.type == 'SUN':
        new_type = 'POINT'
    else:
        new_type = light.type
    if light.type != new_type:
        light.type = new_type
        light = obj.data    # the Python type changes with the light type: fetch it again

    # source size (cm -> m)
    s = [x / 100.0 for x in p["size_cm"]]
    if s:
        if light.type == 'AREA':
            if len(s) >= 2:
                light.shape = 'RECTANGLE'
                light.size, light.size_y = s[0], s[1]
            else:
                light.shape = 'DISK'
                light.size = s[0]
        elif light.type in {'POINT', 'SPOT'}:
            light.shadow_soft_size = s[0] / 2  # radius
    if light.type == 'SPOT':
        # without an angle in the database: a typical wide beam (street lights, ceiling
        # lights) instead of reusing the current cone, which would squeeze all the
        # lumens into a narrow beam
        ang = p["angle"] or DEFAULT_SPOT_ANGLE
        light.spot_size = math.radians(min(max(ang, 1.0), 180.0))

    # color (ACEScg). Blender's native temperature isn't used: this way the color is
    # always in ACEScg and its luminance is known, to convert lumens correctly.
    if hasattr(light, "use_temperature"):
        light.use_temperature = False
    col = preset_color(p)
    light.color = col
    k = 1.0 / _lum(col)

    if light.type == 'SUN':
        light.angle = math.radians(p["angle"] if p["angle"] and p["angle"] < 10 else SUN_ANGLE)

    if hasattr(light, "exposure"):
        light.exposure = 0.0

    # intensity -> Blender units
    if val is not None:
        if unit == "lx":
            light.energy = val / LM_PER_W * k                   # W/m²
        elif unit == "cd/m2":
            if light.shape in {'SQUARE', 'RECTANGLE'}:
                sy = light.size if light.shape == 'SQUARE' else light.size_y
                area = light.size * sy
            else:
                sy = light.size if light.shape == 'DISK' else light.size_y
                area = math.pi * light.size * sy / 4
            area *= abs(obj.scale.x * obj.scale.y)
            light.energy = val * area * math.pi / LM_PER_W * k  # Lambertian emitter
        elif unit == "W":
            light.energy = val
        elif light.type == 'SPOT':
            # Blender defines spot power as if it were an omnidirectional point: the
            # real lumens are all inside the cone, so compensate with the solid angle
            half = light.spot_size / 2
            light.energy = val / LM_PER_W * k * 2.0 / max(1.0 - math.cos(half), 1e-4)
        else:
            light.energy = val / LM_PER_W * k                   # lm -> W

    light["pb_preset"] = p["name"]
    light["pb_intensity"] = f"{val:g} {unit}" if val is not None else ""
    light["pb_temperature"] = p["temperature"] or 0.0


# ---------- data ----------
def _rebuild_enum():
    _ENUM.clear()
    _PRESETS.clear()
    _ENUM.append(("NONE", "— Choose a preset —", ""))
    for raw in DB.items:
        if isinstance(raw, dict):
            _PRESETS.extend(parse_all(raw))
    # grouped in the menu (identifiers stay the preset index, so nothing else changes)
    def group(p):
        if is_sky(p) or p["unit"] == "lx":
            return 0, "Sun & Sky"
        if p["unit"] == "cd/m2":
            return 1, "Screens & Surfaces"
        if p["unit"] == "lm":
            return 2, "Lamps & Bulbs"
        return 3, "Other"
    order = sorted(range(len(_PRESETS)), key=lambda i: (group(_PRESETS[i])[0], _PRESETS[i]["name"].lower()))
    last = None
    for i in order:
        p = _PRESETS[i]
        g = group(p)
        if g != last:
            _ENUM.append(("", g[1], ""))      # empty identifier = heading in the menu
            last = g
        lab = p["name"]
        if p["value"] is not None:
            lab += f"   ({p['value']:g} {p['unit']}"
            lab += f", {p['temperature']:g} K)" if p["temperature"] else ")"
        _ENUM.append((str(i), lab, p["description"]))


DB = Database("lightsources.json", API_URLS, "light sources", _rebuild_enum)


# ---------- properties ----------
def _enum_items(self, context):
    return _ENUM or [("NONE", "Database not loaded", "")]


def _on_preset(self, context):
    if _MUTE:
        return
    key = self.preset
    if key == "NONE" or not key.isdigit() or int(key) >= len(_PRESETS):
        return
    p = _PRESETS[int(key)]
    if is_sky(p):
        apply_world(context.scene, p)
        try:
            bpy.ops.ed.undo_push(message=f"Sky preset: {p['name']}")
        except Exception:
            pass
        return
    lights = [o for o in context.selected_objects if o.type == 'LIGHT']
    ao = context.active_object
    if ao and ao.type == 'LIGHT' and ao not in lights:
        lights.append(ao)
    lights = [o for o in lights if o.data.library is None]   # linked lights can't be edited
    for o in lights:
        apply_preset(o, p)
    if lights:
        try:
            bpy.ops.ed.undo_push(message=f"Light preset: {p['name']}")
        except Exception:
            pass


class PBL_State(bpy.types.PropertyGroup):
    preset: bpy.props.EnumProperty(name="Preset", items=_enum_items, update=_on_preset,
                                   description="Apply the preset to the selected lights")


# ---------- operators / panels ----------
class PBL_OT_fetch(bpy.types.Operator):
    bl_idname = "pbl.fetch"
    bl_label = "Update Database"
    bl_description = "Download the light sources from physicallybased.info"

    def execute(self, context):
        DB.start_update()
        return {'FINISHED'}


class PBL_OT_reapply(bpy.types.Operator):
    bl_idname = "pbl.reapply"
    bl_label = "Reapply"
    bl_description = "Reapply the current preset to the selected lights"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.window_manager.pbl_state.preset != "NONE"

    def execute(self, context):
        _on_preset(context.window_manager.pbl_state, context)
        return {'FINISHED'}


def _draw_picker(col, context):
    s = context.window_manager.pbl_state
    ui_status(col, DB)
    row = col.row(align=True)
    row.scale_y = 1.3
    row.prop(s, "preset", text="", icon='LIGHT')
    row.operator("pbl.reapply", text="", icon='LOOP_BACK')


def _draw_light_card(col, light):
    if light is None or "pb_preset" not in light:
        return
    card = ui_card(col, light["pb_preset"], 'LIGHT')
    if light.get("pb_intensity"):
        ui_kv(card, "Real", light["pb_intensity"])
    if light.get("pb_temperature"):
        ui_kv(card, "Temperature", f"{light['pb_temperature']:g} K")
    ui_kv(card, "Blender", f"{light.energy:.4g} " + ("W/m²" if light.type == 'SUN' else "W"))


class PBL_PT_panel(bpy.types.Panel):
    bl_label = "Physically Based"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "data"

    @classmethod
    def poll(cls, context):
        return context.light is not None

    def draw_header(self, context):
        self.layout.label(text="", icon='LIGHT_AREA')

    def draw_header_preset(self, context):
        ui_refresh(self.layout, DB, "pbl.fetch")

    def draw(self, context):
        col = self.layout.column()
        _draw_picker(col, context)
        _draw_light_card(col, context.light)


class PBL_PT_world(bpy.types.Panel):
    bl_label = "Physically Based"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "world"

    @classmethod
    def poll(cls, context):
        return context.world is not None and "pb_preset" in context.world

    def draw(self, context):
        w = context.world
        card = ui_card(self.layout, w["pb_preset"], 'WORLD')
        if w.get("pb_intensity"):
            ui_kv(card, "Real", w["pb_intensity"])
        bg = next((n for n in w.node_tree.nodes if n.type == 'BACKGROUND'), None) if w.node_tree else None
        if bg:
            ui_kv(card, "Blender Strength", f"{bg.inputs['Strength'].default_value:.4g}")


class PBL_PT_view3d(bpy.types.Panel):
    bl_label = "Physically Based Lights"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PhysBased"

    def draw_header(self, context):
        self.layout.label(text="", icon='LIGHT_AREA')

    def draw_header_preset(self, context):
        ui_refresh(self.layout, DB, "pbl.fetch")

    def draw(self, context):
        col = self.layout.column()
        _draw_picker(col, context)
        ao = context.active_object
        if ao is not None and ao.type == 'LIGHT':
            _draw_light_card(col, ao.data)
        elif not any(o.type == 'LIGHT' for o in context.selected_objects):
            hint = col.row()
            hint.active = False
            hint.label(text="Select one or more lights (Sky presets set the World)", icon='INFO')


classes = (PBL_State, PBL_OT_fetch, PBL_OT_reapply, PBL_PT_panel, PBL_PT_world, PBL_PT_view3d)


@bpy.app.handlers.persistent
def _on_load_post(*_):
    # back to "Choose a preset" without applying anything
    global _MUTE
    _MUTE = True
    try:
        bpy.context.window_manager.pbl_state.preset = "NONE"
    except Exception:
        pass
    finally:
        _MUTE = False


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.pbl_state = bpy.props.PointerProperty(type=PBL_State)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    DB.stop()
    del bpy.types.WindowManager.pbl_state
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
