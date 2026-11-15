"""Materials: a click in the list adds a node group with the PBR values (ACEScg),
to be connected by hand to the Principled BSDF."""

import bpy
import hashlib
import json
import re
from numbers import Number

from .common import Database, ui_refresh, ui_status, ui_card, ui_kv

API_URLS = [
    "https://api.physicallybased.info/v2/materials",
    "https://raw.githubusercontent.com/AntonPalmqvist/physically-based-api/main/deploy/v2/materials.json",
]

_SUPPRESS = False    # blocks the selection callback while the list is rebuilt
_PARSED = []         # parsed records, built once per database load (not on every redraw)


# ---------- tolerant parsing (v1 / v2) ----------
def _vec3(v):
    """First vector of 3 numbers in v (list, list of lists, dict)."""
    if isinstance(v, (list, tuple)):
        if len(v) >= 3 and all(isinstance(x, Number) for x in v[:3]):
            return tuple(float(x) for x in v[:3])
        for x in v:
            r = _vec3(x)
            if r:
                return r
    elif isinstance(v, dict):
        keys = list(v)
        # if the data has several color spaces, prefer linear sRGB
        lin = [k for k in keys if "linear" in k.lower() and "p3" not in k.lower()]
        for k in lin + ["color", "value", "values"] + keys:
            if k in v:
                r = _vec3(v[k])
                if r:
                    return r
    return None


# Linear Rec.709 (linear sRGB, D65) -> ACEScg (AP1, D60), Bradford adaptation
_REC709_TO_ACESCG = (
    (0.613097, 0.339523, 0.047379),
    (0.070194, 0.916354, 0.013452),
    (0.020616, 0.109570, 0.869815),
)


def _space(entry):
    for k in ("colorSpace", "colorspace", "color_space", "space"):
        if isinstance(entry.get(k), str):
            return entry[k].lower().replace("_", "").replace("-", "").replace(" ", "")
    return ""


def _acescg(v):
    """Color in ACEScg: use the database's ACEScg value if present, otherwise convert."""
    # list of {"colorSpace": ..., "color": [...]} entries, one per color space
    if isinstance(v, (list, tuple)) and v and all(isinstance(x, dict) for x in v):
        for x in v:
            sp = _space(x)
            if "aces" in sp and ("cg" in sp or "ap1" in sp):
                r = _vec3(x)
                if r:
                    return r
        lin = next((x for x in v if "lin" in _space(x) and "p3" not in _space(x)), None)
        v = lin if lin is not None else v[0]
    if isinstance(v, dict):
        for k, x in v.items():
            kl = k.lower().replace("_", "").replace("-", "")
            if "aces" in kl and ("cg" in kl or "ap1" in kl):
                r = _vec3(x)
                if r:
                    return r
    rgb = _vec3(v)  # linear sRGB (the site's default)
    if rgb is None:
        return None
    return tuple(sum(m * c for m, c in zip(row, rgb)) for row in _REC709_TO_ACESCG)


def _num(v):
    """First scalar number in v."""
    if isinstance(v, (bool, Number)):
        return float(v)
    if isinstance(v, (list, tuple)):
        for x in v:
            r = _num(x)
            if r is not None:
                return r
    elif isinstance(v, dict):
        for k in ("value", "ior", "values"):
            if k in v:
                r = _num(v[k])
                if r is not None:
                    return r
        for x in v.values():
            r = _num(x)
            if r is not None:
                return r
    return None


_N_KEYS = ("n", "eta", "real")
_K_KEYS = ("k", "kappa", "imaginary", "extinction")


def _rgb(v):
    """Number -> (x, x, x); list/dict -> first vector of 3 numbers."""
    if isinstance(v, (bool, Number)):
        return (float(v),) * 3
    if isinstance(v, (list, tuple)) and len(v) == 1 and isinstance(v[0], Number):
        return (float(v[0]),) * 3
    return _vec3(v)


def _complex(v):
    """Find a complex IOR: dict with n/k, or [[n,k],[n,k],[n,k]] per channel."""
    if isinstance(v, dict):
        nk = next((v[k] for k in _N_KEYS if k in v), None)
        kk = next((v[k] for k in _K_KEYS if k in v), None)
        if nk is not None and kk is not None:
            n, k = _rgb(nk), _rgb(kk)
            if n and k:
                return n, k
        for x in v.values():
            r = _complex(x)
            if r:
                return r
    elif isinstance(v, (list, tuple)):
        if len(v) == 6 and all(isinstance(x, Number) for x in v):  # nR,kR,nG,kG,nB,kB
            if not any(v):
                return None
            return tuple(float(x) for x in v[0::2]), tuple(float(x) for x in v[1::2])
        if (len(v) == 3 and all(isinstance(c, (list, tuple)) and len(c) == 2
                                and all(isinstance(x, Number) for x in c) for c in v)):
            return tuple(float(c[0]) for c in v), tuple(float(c[1]) for c in v)
        for x in v:
            r = _complex(x)
            if r:
                return r
    return None


def _ior_from_nk(n, k):
    """Equivalent real IOR: same F0 reflectance (Rec.709 luminance average)."""
    w = (0.2126, 0.7152, 0.0722)
    f0 = sum(wi * ((ni - 1) ** 2 + ki ** 2) / ((ni + 1) ** 2 + ki ** 2)
             for wi, ni, ki in zip(w, n, k))
    s = min(f0, 0.9999) ** 0.5
    return (1 + s) / (1 - s)


def _get(m, *keys):
    for k in keys:
        if k in m and m[k] not in (None, "", []):
            return m[k]
    return None


def _num_by_keyword(v, keyword):
    """Recursively find a numeric value stored under any key containing `keyword`
    (case-insensitive), at any nesting depth — the site's "Dispersion Abbe" value
    isn't always a flat top-level field."""
    if isinstance(v, dict):
        for k, x in v.items():
            if keyword in k.lower().replace("_", "").replace("-", "").replace(" ", ""):
                n = _num(x)
                if n is not None:
                    return n
        for x in v.values():
            r = _num_by_keyword(x, keyword)
            if r is not None:
                return r
    elif isinstance(v, (list, tuple)):
        for x in v:
            r = _num_by_keyword(x, keyword)
            if r is not None:
                return r
    return None


def _num_by_exact_key(v, names):
    """Like _num_by_keyword, but matches a key only if it equals one of `names`
    exactly (after stripping separators) — for short optics symbols (Vd, nu_d...)
    that would false-positive as a substring."""
    if isinstance(v, dict):
        for k, x in v.items():
            kl = k.lower().replace("_", "").replace("-", "").replace(" ", "")
            if kl in names:
                n = _num(x)
                if n is not None:
                    return n
        for x in v.values():
            r = _num_by_exact_key(x, names)
            if r is not None:
                return r
    elif isinstance(v, (list, tuple)):
        for x in v:
            r = _num_by_exact_key(x, names)
            if r is not None:
                return r
    return None


# Common optics symbols for the Abbe number that don't contain the word "abbe":
# V-number / Vd / nu_d (the Greek letter nu, as "nu" or "v").
_ABBE_SYMBOLS = {"vd", "nud", "vnumber"}

# Last-resort fallback: some materials only mention the Abbe number in free text
# (the description / info blurb), e.g. "...has an Abbe number of 58.6...".
_ABBE_RE = re.compile(r"abbe[^0-9]{0,30}?(\d+(?:\.\d+)?)", re.I)


def _abbe_in_text(v):
    if isinstance(v, str):
        m = _ABBE_RE.search(v)
        return float(m.group(1)) if m else None
    if isinstance(v, dict):
        for x in v.values():
            r = _abbe_in_text(x)
            if r is not None:
                return r
    elif isinstance(v, (list, tuple)):
        for x in v:
            r = _abbe_in_text(x)
            if r is not None:
                return r
    return None


def parse(m):
    cat = _get(m, "category", "categories") or []
    if isinstance(cat, str):
        cat = [cat]
    metal = _num(_get(m, "metalness", "metallic"))
    if metal is None:
        metal = 1.0 if any("metal" in str(c).lower() for c in cat) else 0.0
    raw_ior = _get(m, "ior")
    cplx = _complex(_get(m, "complexIor", "complexIOR", "complex_ior", "iorComplex")) \
        or _complex(raw_ior)
    n = k = None
    ior_src = "database"
    if cplx:
        n, k = cplx
    # scalar IOR: only if the data really is a number, not the real part of n/k
    ior = None if (raw_ior is not None and _complex(raw_ior)) else _num(raw_ior)
    if ior is None and cplx:
        ior, ior_src = _ior_from_nk(n, k), "equivalent from n, k"
    desc = m.get("description")
    # official schema key: "transmissionDispersion" (Abbe number, 0 = no data)
    abbe = _num(_get(m, "transmissionDispersion"))
    if not abbe:
        abbe = _num_by_keyword(m, "abbe")
    if abbe is None:
        abbe = _num_by_exact_key(m, _ABBE_SYMBOLS)
    abbe_src = "database"
    if abbe is None:
        abbe = _abbe_in_text(m)
        abbe_src = "from description"
    return {
        "name": m.get("name", "?"),
        "category": ", ".join(str(c) for c in cat),
        "description": desc if isinstance(desc, str) else "",
        "color": _acescg(_get(m, "color", "baseColor")),
        "specular": _acescg(_get(m, "specularColor", "specularColorF82", "f82")) or (1.0, 1.0, 1.0),
        "metal": metal,
        "spec_level": 0.5,  # physical value: reflection depends on the IOR
        "rough": _num(_get(m, "roughness")),
        "ior": ior,
        "ior_src": ior_src,
        "ior_n": n,
        "ior_k": k,
        "trans": _num(_get(m, "transmission")),
        "trans_color": _acescg(_get(m, "transmissionColor")),
        "density": _num(_get(m, "density")),
        # Abbe number: how much the IOR varies across the visible spectrum (lower = more
        # dispersion, i.e. more "rainbow" spread — diamond is around 55, crown glass ~60).
        "abbe": abbe,
        "abbe_src": abbe_src,
    }


# ---------- data ----------
def refresh_list():
    global _SUPPRESS
    s = bpy.context.window_manager.pb_state
    _SUPPRESS = True
    try:
        s.items.clear()
        s.active = -1
        _PARSED[:] = [parse(raw) for raw in DB.items]
        for i, p in enumerate(_PARSED):
            it = s.items.add()
            it.name = p["name"]
            it.category = p["category"]
            it.index = i
            c = p["color"] or (0.8, 0.8, 0.8)
            it.swatch = tuple(min(max(v, 0.0), 1.0) for v in c)
    finally:
        _SUPPRESS = False


DB = Database("materials.json", API_URLS, "materials", refresh_list)


# ---------- node group ----------
FIELDS = [
    ("Base Color", "color", "NodeSocketColor"),            # ACEScg
    ("Metallic", "metal", "NodeSocketFloat"),
    ("Specular IOR Level", "spec_level", "NodeSocketFloat"),
    ("Specular Tint", "specular", "NodeSocketColor"),      # ACEScg (F82 for metals)
    ("Roughness", "rough", "NodeSocketFloat"),
    ("IOR", "ior", "NodeSocketFloat"),
    ("Complex IOR n", "ior_n", "NodeSocketVector"),
    ("Complex IOR k", "ior_k", "NodeSocketVector"),
    ("Transmission", "trans", "NodeSocketFloat"),
    ("Transmission Color", "trans_color", "NodeSocketColor"),  # ACEScg
    ("Density kg/m3", "density", "NodeSocketFloat"),
    ("Dispersion (Abbe Number)", "abbe", "NodeSocketFloat"),
]


def _hash(p):
    return hashlib.md5(json.dumps(p, sort_keys=True).encode()).hexdigest()


def _fill_group(ng, p):
    """Create or update the values without removing sockets (existing links survive)."""
    out = next((n for n in ng.nodes if n.type == 'GROUP_OUTPUT'), None)
    if out is None:
        out = ng.nodes.new("NodeGroupOutput")
        out.location = (300, 0)
    sockets = {i.name for i in ng.interface.items_tree
               if i.item_type == 'SOCKET' and i.in_out == 'OUTPUT'}
    y = 0
    for label, key, stype in FIELDS:
        val = p[key]
        if val is None:
            continue
        if label not in sockets:
            ng.interface.new_socket(label, in_out='OUTPUT', socket_type=stype)
        n = ng.nodes.get(f"PB_{key}")
        if n is None:
            n = ng.nodes.new({"NodeSocketColor": "ShaderNodeRGB",
                              "NodeSocketVector": "ShaderNodeCombineXYZ"}.get(stype, "ShaderNodeValue"))
            n.name = f"PB_{key}"
            n.label = label
            n.location = (0, y)
        y -= 120 if stype != "NodeSocketVector" else 160
        if stype == "NodeSocketColor":
            n.outputs[0].default_value = (*val, 1.0)
        elif stype == "NodeSocketVector":
            for j in range(3):  # X, Y, Z = R, G, B
                n.inputs[j].default_value = val[j]
        else:
            n.outputs[0].default_value = val
        if not n.outputs[0].is_linked:
            ng.links.new(n.outputs[0], out.inputs[label])
    if hasattr(ng, "description"):  # group tooltip (Blender 4.1+)
        ng.description = p["description"] or f"physicallybased.info – {p['name']}"
    ng["pb_hash"] = _hash(p)


def build_group(p):
    gname = f"PB {p['name']}"
    ng = bpy.data.node_groups.get(gname)
    if ng and ng.library:  # group linked from another library: can't be edited
        return ng
    if ng is None:
        ng = bpy.data.node_groups.new(gname, "ShaderNodeTree")
    if ng.get("pb_hash") != _hash(p):  # new, or the database has changed
        _fill_group(ng, p)
    return ng


def _target_tree(context):
    sp = getattr(context, "space_data", None)
    if sp and sp.type == 'NODE_EDITOR' and sp.tree_type == 'ShaderNodeTree' and sp.edit_tree:
        tree = sp.edit_tree
        return (tree, sp) if tree.library is None else (None, None)
    obj = context.active_object
    mat = obj.active_material if obj else None
    if mat and mat.library is None:
        if not getattr(mat, "use_nodes", True):   # deprecated in Blender 5.0 (always on)
            mat.use_nodes = True
        return mat.node_tree, None
    return None, None


def add_group_node(context, item_index):
    tree, sp = _target_tree(context)
    if tree is None:
        return None
    p = _PARSED[item_index]
    ng = build_group(p)
    try:                         # editing inside a "PB ..." group: it can't contain itself
        if ng == tree or ng.contains_tree(tree):
            return None
    except AttributeError:
        if ng == tree:
            return None
    for n in tree.nodes:
        n.select = False
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = ng
    node.label = p["name"]
    node.select = True
    tree.nodes.active = node
    # center of the view, like Blender does when you add a node from the sidebar
    c = tree.view_center
    node.location = (c[0] - node.width / 2, c[1] + 100)
    return node


def _selected_index(s):
    if 0 <= s.active < len(s.items):
        i = s.items[s.active].index
        if i < len(_PARSED):
            return i
    return None


def _on_select(self, context):
    if _SUPPRESS:
        return
    i = _selected_index(self)
    if i is None:
        return
    if add_group_node(context, i):
        try:  # changing a UI property doesn't create an undo step by itself
            bpy.ops.ed.undo_push(message="Add Physically Based group")
        except Exception:
            pass


# ---------- properties ----------
class PB_Item(bpy.types.PropertyGroup):
    category: bpy.props.StringProperty()
    index: bpy.props.IntProperty()
    swatch: bpy.props.FloatVectorProperty(subtype='COLOR', size=3, min=0.0, max=1.0,
                                          description="Base color preview")


class PB_State(bpy.types.PropertyGroup):
    items: bpy.props.CollectionProperty(type=PB_Item)
    active: bpy.props.IntProperty(default=-1, update=_on_select)
    search: bpy.props.StringProperty(name="Search", default="",
                                      options={'TEXTEDIT_UPDATE'},   # filter while typing
                                      description="Filter by name or category")


# ---------- UI list ----------
class PB_UL_materials(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        sw = row.row()
        sw.ui_units_x = 1.2
        sw.prop(item, "swatch", text="")
        row.label(text=item.name)
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.active = False
        sub.label(text=item.category)

    def filter_items(self, context, data, prop):
        items = getattr(data, prop)
        flt = [self.bitflag_filter_item] * len(items)
        q = (getattr(data, "search", "") or self.filter_name).lower()
        if q:
            flt = [self.bitflag_filter_item if (q in i.name.lower() or q in i.category.lower()) else 0
                   for i in items]
        order = bpy.types.UI_UL_list.sort_items_by_name(items, "name") if self.use_filter_sort_alpha else []
        return flt, order


# ---------- operators ----------
class PB_OT_fetch(bpy.types.Operator):
    bl_idname = "pb.fetch"
    bl_label = "Update Database"
    bl_description = "Download the materials from physicallybased.info (in the background)"

    def execute(self, context):
        DB.start_update()
        return {'FINISHED'}


class PB_OT_search_clear(bpy.types.Operator):
    bl_idname = "pb.search_clear"
    bl_label = "Clear Search"
    bl_description = "Clear the search filter"

    def execute(self, context):
        context.window_manager.pb_state.search = ""
        return {'FINISHED'}


class PB_OT_add(bpy.types.Operator):
    bl_idname = "pb.add"
    bl_label = "Add Again"
    bl_description = "Add another instance of the selected group"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _selected_index(context.window_manager.pb_state) is not None

    def execute(self, context):
        i = _selected_index(context.window_manager.pb_state)
        if add_group_node(context, i) is None:
            self.report({'WARNING'}, "No editable shader node tree (open one, or select an object with a material)")
            return {'CANCELLED'}
        return {'FINISHED'}


# ---------- panel ----------
class PB_PT_panel(bpy.types.Panel):
    bl_label = "Physically Based Materials"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "PhysBased"

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'ShaderNodeTree'

    def draw_header(self, context):
        self.layout.label(text="", icon='MATERIAL')

    def draw_header_preset(self, context):
        ui_refresh(self.layout, DB, "pb.fetch")

    def draw(self, context):
        s = context.window_manager.pb_state
        col = self.layout.column()
        ui_status(col, DB)
        if not s.items:
            if not DB.busy:
                row = col.row()
                row.scale_y = 1.4
                row.operator("pb.fetch", icon='FILE_REFRESH')
            return

        row = col.row(align=True)
        row.prop(s, "search", text="", icon='VIEWZOOM')
        if s.search:
            row.operator("pb.search_clear", text="", icon='X')

        col.template_list("PB_UL_materials", "", s, "items", s, "active", rows=10)

        i = _selected_index(s)
        if i is None:
            hint = col.row()
            hint.active = False
            hint.label(text="Click a material to add its node group", icon='INFO')
            return
        row = col.row()
        row.scale_y = 1.3
        row.operator("pb.add", text="Add Another Instance", icon='ADD')

        p = _PARSED[i]
        card = ui_card(col, p["name"], 'MATERIAL')
        if p["category"]:
            ui_kv(card, "Category", p["category"])
        if p["color"]:
            ui_kv(card, "Color (ACEScg)", "%.3f  %.3f  %.3f" % p["color"])
        ui_kv(card, "Metallic", f"{p['metal']:.2f}")
        if p["rough"] is not None:
            ui_kv(card, "Roughness", f"{p['rough']:.2f}")
        if p["ior"] is not None:
            src = "" if p["ior_src"] == "database" else "  (from n, k)"
            ui_kv(card, "IOR", f"{p['ior']:.3f}{src}")
        if p["ior_n"]:
            ui_kv(card, "n (RGB)", "%.3f  %.3f  %.3f" % p["ior_n"])
            ui_kv(card, "k (RGB)", "%.3f  %.3f  %.3f" % p["ior_k"])
        if p["specular"] != (1.0, 1.0, 1.0):
            ui_kv(card, "Specular Tint", "%.3f  %.3f  %.3f" % p["specular"])
        if p["trans"] is not None:
            ui_kv(card, "Transmission", f"{p['trans']:.2f}")
        if p["trans_color"] and p["trans_color"] != (1.0, 1.0, 1.0):
            ui_kv(card, "Transmission Color", "%.3f  %.3f  %.3f" % p["trans_color"])
        if p["density"] is not None:
            ui_kv(card, "Density", f"{p['density']:.0f} kg/m³")
        if p["abbe"] is not None:
            src = "" if p["abbe_src"] == "database" else "  (from description)"
            ui_kv(card, "Abbe Number", f"{p['abbe']:.1f}{src}")


classes = (PB_Item, PB_State, PB_UL_materials, PB_OT_fetch, PB_OT_search_clear, PB_OT_add, PB_PT_panel)


@bpy.app.handlers.persistent
def _on_load_post(*_):
    # the list lives in the WindowManager: it must be rebuilt after opening a .blend
    DB.reapply()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.pb_state = bpy.props.PointerProperty(type=PB_State)
    bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    DB.stop()
    del bpy.types.WindowManager.pb_state
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
