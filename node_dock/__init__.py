import bpy
import blf
import gpu
import os
import re
import json
import time
import uuid
import colorsys
import math
from array import array
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent

ADDON_ID = __package__ or __name__
TREE_TYPE = 'GeometryNodeTree'
GROUP_NODE = 'GeometryNodeGroup'
ID_COLLECTIONS = ("node_groups", "materials", "objects", "collections", "images", "texts")
IO_NODES = {'NodeGroupInput', 'NodeGroupOutput'}

_handles = []
_keymaps = []
_dock_areas = set()     # Node Editors where the dock is shown (this session)
_scroll = {}            # area ptr -> horizontal scroll in px
_drag = {}              # area ptr -> drag-from-dock state (for the ghost tile)
_flash = {}             # area ptr -> time of last successful store
_state = {}             # (area ptr, tree) -> drop-detection state
_index_cache = {"path": None, "mtime": None, "items": [], "checked": 0.0}
_dir_cache = {"key": None, "dir": None}


# ================================================================ preferences

def _redraw_all(self=None, context=None):
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()


class NDOCK_Preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    position: bpy.props.EnumProperty(
        name="Position",
        items=[('BOTTOM', "Bottom", ""), ('TOP', "Top", "")],
        default='BOTTOM', update=_redraw_all)
    height: bpy.props.IntProperty(
        name="Height", default=64, min=40, max=160, subtype='PIXEL', update=_redraw_all)
    library_path: bpy.props.StringProperty(
        name="Library Folder", subtype='DIR_PATH',
        description="Where stored nodes are saved. Leave empty for the default folder",
        update=_redraw_all)
    reuse_existing: bpy.props.BoolProperty(
        name="Reuse Existing Data", default=True,
        description="When placing, reuse node groups, materials, objects etc. already in the "
                    "file with the same name instead of creating .001 copies")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Dock", icon='BOOKMARKS')
        col = box.column()
        col.use_property_split = True
        col.prop(self, "position")
        col.prop(self, "height")

        box = layout.box()
        try:
            n = len(get_items())
            box.label(text=f"Library  ·  {n} item{'s' if n != 1 else ''}", icon='FILE_FOLDER')
        except Exception:
            box.label(text="Library", icon='FILE_FOLDER')
        col = box.column()
        col.use_property_split = True
        col.prop(self, "library_path")
        col.prop(self, "reuse_existing")
        row = box.row()
        row.operator("ndock.open_folder", icon='FOLDER_REDIRECT')

        box = layout.box()
        box.label(text="How to use", icon='INFO')
        col = box.column(align=True)
        col.active = False
        col.label(text="Bookmark button in the Geometry Node Editor header: dock on/off")
        col.label(text="Drag nodes onto the dock to store them, drag a tile out to reuse")
        col.label(text="Double-click a tile to rename, right-click for more")


def prefs():
    return bpy.context.preferences.addons[ADDON_ID].preferences


def ui_scale():
    return bpy.context.preferences.system.ui_scale


# ================================================================ library on disk

def lib_dir():
    p = prefs().library_path.strip()
    c = _dir_cache
    if c["key"] == p and c["dir"] and os.path.isdir(c["dir"]):
        return c["dir"]
    if p:
        d = bpy.path.abspath(p)
    else:
        try:
            d = bpy.utils.extension_path_user(ADDON_ID, path="library", create=True)
        except Exception:
            d = os.path.join(bpy.utils.user_resource('CONFIG'), "node_dock_library")
    os.makedirs(d, exist_ok=True)
    c.update(key=p, dir=d)
    return d


def _index_path():
    return os.path.join(lib_dir(), "index.json")


def get_items():
    """Dock items, re-read from disk at most once a second (or when the folder changes)."""
    path = _index_path()
    c = _index_cache
    now = time.time()
    if c["path"] == path and now - c["checked"] < 1.0:
        return c["items"]
    c["checked"] = now
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    if c["path"] != path or c["mtime"] != mtime:
        items = []
        if mtime is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    items = json.load(f).get("items", [])
            except Exception:
                items = []
        c.update(path=path, mtime=mtime, items=items)
    return c["items"]


def save_items(items):
    path = _index_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "items": items}, f, indent=1)
    os.replace(tmp, path)
    _index_cache.update(path=path, mtime=os.path.getmtime(path), items=items,
                        checked=time.time())
    _redraw_all()


def find_item(item_id):
    return next((it for it in get_items() if it.get("id") == item_id), None)


def item_file(it):
    return os.path.join(lib_dir(), it["id"] + ".blend")


# ================================================================ storing nodes

def _node_title(n):
    if n.label:
        return n.label
    nt = getattr(n, "node_tree", None)
    if nt is not None and n.bl_idname.endswith("Group"):
        return nt.name
    return getattr(n, "bl_label", "") or n.name


def _auto_name(nodes, primary):
    frames = [n for n in nodes if n.bl_idname == 'NodeFrame' and n.label]
    if frames:
        return frames[0].label
    real = [n for n in nodes if n.bl_idname not in {'NodeFrame', 'NodeReroute'}] or nodes
    if len(real) == 1:
        return _node_title(real[0])
    main = next((n for n in primary if n in real), real[0])
    return "%s +%d" % (_node_title(main), len(real) - 1)


def store_nodes(tree, nodes):
    """Save the given nodes (plus zone partners and frame children) as a dock item."""
    keep = {n.name for n in nodes}
    for n in tree.nodes:                       # keep both halves of zones
        po = getattr(n, "paired_output", None)
        if po is not None and (n.name in keep or po.name in keep):
            keep.update((n.name, po.name))
    grew = True
    while grew:                                # keep everything inside selected frames
        grew = False
        for n in tree.nodes:
            if n.parent is not None and n.parent.name in keep and n.name not in keep:
                keep.add(n.name)
                grew = True
    keep -= {n.name for n in tree.nodes if n.bl_idname in IO_NODES}
    if not keep:
        return None

    kept = [tree.nodes[k] for k in keep]
    name = _auto_name(kept, list(nodes))
    count = len([n for n in kept if n.bl_idname not in {'NodeFrame', 'NodeReroute'}]) or len(kept)

    iid = uuid.uuid4().hex[:12]
    group_name = "NodeDock_" + iid
    cp = tree.copy()
    try:
        for n in list(cp.nodes):
            if n.name not in keep:
                cp.nodes.remove(n)
        try:
            cp.interface.clear()
        except Exception:
            pass
        cp.name = group_name
        bpy.data.libraries.write(os.path.join(lib_dir(), iid + ".blend"), {cp},
                                 path_remap='ABSOLUTE', fake_user=True, compress=True)
    finally:
        bpy.data.node_groups.remove(cp)

    items = list(get_items())
    items.append({"id": iid, "name": name, "tree": tree.bl_idname,
                  "group": group_name, "count": count, "created": int(time.time())})
    save_items(items)
    return name


# ================================================================ placing nodes

def _contains_tree(group, tree):
    """True if `group` is `tree` or uses it somewhere inside (placing it would recurse)."""
    if group == tree:
        return True
    try:
        return group.contains_tree(tree)
    except Exception:
        pass
    seen, todo = set(), [group]
    while todo:
        g = todo.pop()
        if g.name_full in seen:
            continue
        seen.add(g.name_full)
        for n in g.nodes:
            sub = getattr(n, "node_tree", None)
            if sub is None:
                continue
            if sub == tree:
                return True
            todo.append(sub)
    return False


def _reuse_existing(new, keep_ptr, tree):
    new_ptrs = {i.as_pointer() for ids in new.values() for i in ids}
    for c in ID_COLLECTIONS:
        coll = getattr(bpy.data, c)
        for idb in new[c]:
            if idb.as_pointer() == keep_ptr:
                continue
            m = re.match(r"^(.*)\.\d{3,}$", idb.name)
            if not m:
                continue
            base = coll.get(m.group(1))
            if base is None or base.as_pointer() in new_ptrs or base.library is not None:
                continue
            if c == "node_groups" and (base.bl_idname != idb.bl_idname
                                       or _contains_tree(base, tree)):
                continue                           # never put a group inside itself
            idb.user_remap(base)
            try:
                coll.remove(idb)
            except Exception:
                pass


def _recenter(nodes, target):
    s = ui_scale()
    top = [n for n in nodes if n.parent is None or not n.parent.select]
    if not top:
        return
    xs, ys = [], []
    for n in top:
        w = (n.dimensions.x / s) or 140
        h = (n.dimensions.y / s) or 100
        xs += [n.location.x, n.location.x + w]
        ys += [n.location.y, n.location.y - h]
    dx = target[0] - (min(xs) + max(xs)) / 2
    dy = target[1] - (min(ys) + max(ys)) / 2
    for n in top:
        n.location = (n.location.x + dx, n.location.y + dy)


def place_item(context, it, mx, my):
    """Insert a copy of a dock item at region coords (mx, my). Returns an error or None."""
    sp = context.space_data
    tree = sp.edit_tree
    if tree is None:
        return "No node tree to place into"
    if tree.library is not None:
        return "This node tree is linked from another file and can't be edited"
    if it.get("tree", TREE_TYPE) != tree.bl_idname:
        return "This item belongs to a different node editor"
    path = item_file(it)
    if not os.path.exists(path):
        return "Stored file is missing: " + path

    before = {c: {i.as_pointer() for i in getattr(bpy.data, c)} for c in ID_COLLECTIONS}
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.node_groups = [g for g in src.node_groups if g == it["group"]]
    if not dst.node_groups or dst.node_groups[0] is None:
        return "Stored nodes not found in " + path
    grp = dst.node_groups[0]
    grp.use_fake_user = False
    new = {c: [i for i in getattr(bpy.data, c) if i.as_pointer() not in before[c]]
           for c in ID_COLLECTIONS}
    if prefs().reuse_existing:
        _reuse_existing(new, grp.as_pointer(), tree)

    for n in tree.nodes:
        n.select = False
    sp.cursor_location_from_region(int(mx), int(my))
    target = tuple(sp.cursor_location)
    gn = tree.nodes.new(GROUP_NODE)
    gn.node_tree = grp
    gn.location = target
    gn.select = True
    tree.nodes.active = gn
    try:
        bpy.ops.node.group_ungroup()
    except RuntimeError as e:
        if gn.name in tree.nodes:
            tree.nodes.remove(gn)
        return "Could not place nodes: %s" % e
    _recenter([n for n in tree.nodes if n.select], target)
    if grp.users == 0:
        bpy.data.node_groups.remove(grp)
    return None


# ================================================================ geometry

def dock_on(area):
    return area is not None and area.type == 'NODE_EDITOR' and area.as_pointer() in _dock_areas


def valid_space(area):
    sp = area.spaces.active
    return sp.type == 'NODE_EDITOR' and sp.tree_type == TREE_TYPE


def dock_rect(area, region):
    if not dock_on(area) or not valid_space(area):
        return None
    s = ui_scale()
    p = prefs()
    left, right = 0, region.width
    for r in area.regions:                     # skip toolbar / sidebar when they overlap
        if r.type in {'TOOLS', 'UI'} and r.width > 1 and \
                r.x < region.x + region.width and r.x + r.width > region.x:
            if r.x <= region.x + 1:
                left = max(left, r.x + r.width - region.x)
            else:
                right = min(right, r.x - region.x)
    m = 6 * s
    h = p.height * s
    if right - left < 120 * s or region.height < h + 2 * m:
        return None
    if p.position == 'BOTTOM':
        y0, y1 = m, m + h
    else:
        y1 = region.height - m
        y0 = y1 - h
    return (left + m, y0, right - m, y1)


def layout(area, region):
    """(dock rect, tiles, content width). Tiles: (kind, item, (x0, y0, x1, y1))."""
    rect = dock_rect(area, region)
    if rect is None:
        return None, [], 0
    s = ui_scale()
    x0, y0, x1, y1 = rect
    pad = 6 * s
    ty0, ty1 = y0 + pad, y1 - pad
    tw = 150 * s
    items = get_items()
    content = pad + (ty1 - ty0) + pad + len(items) * (tw + pad)
    ptr = area.as_pointer()
    off = max(0, min(_scroll.get(ptr, 0), content - (x1 - x0)))   # stays valid after deletes
    _scroll[ptr] = off
    x = x0 + pad - off
    tiles = [('ADD', None, (x, ty0, x + (ty1 - ty0), ty1))]
    x += (ty1 - ty0) + pad
    for it in items:
        tiles.append(('ITEM', it, (x, ty0, x + tw, ty1)))
        x += tw + pad
    return rect, tiles, content


def inside(rect, x, y):
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def tile_at(rect, tiles, mx, my):
    if rect is None or not inside(rect, mx, my):
        return None
    for t in tiles:
        if inside(t[2], mx, my):
            return t
    return ('DOCK', None, rect)


def node_abs_loc(n):
    la = getattr(n, "location_absolute", None)
    if la is not None:
        return la[0], la[1]
    x, y = n.location
    p = n.parent
    while p is not None:
        x += p.location[0]
        y += p.location[1]
        p = p.parent
    return x, y


def node_center_region(n, region, s):
    x, y = node_abs_loc(n)
    w, h = n.dimensions
    return region.view2d.view_to_region(x * s + w / 2, y * s - h / 2, clip=False)


def window_busy(win):
    """True if a node drag/transform is running. None if Blender can't tell us."""
    ops = getattr(win, "modal_operators", None)
    if ops is None:
        return None
    for op in ops:
        idn = (getattr(op, "bl_idname", "") or "").upper()
        if "TRANSFORM" in idn or "TRANSLATE" in idn or idn.startswith("NODE"):
            return True
    return False


# ================================================================ drop detection (timer)

def _loc_signature(tree):
    """Cheap fingerprint of all node locations (one C call instead of a Python loop)."""
    nodes = tree.nodes
    a = array('f', bytes(8 * len(nodes)))
    try:
        nodes.foreach_get("location", a)
    except Exception:
        a = array('f', [v for n in nodes for v in n.location])
    return len(nodes), a.tobytes()


def _positions(tree):
    return {n.name: tuple(n.location) for n in tree.nodes}


def _tick():
    try:
        _tick_inner()
    except Exception as e:
        print("Node Dock:", e)
    return 0.15 if _dock_areas else 0.5


def _tick_inner():
    if not _dock_areas:
        if _state:
            _state.clear()
        return
    wm = bpy.context.window_manager
    if wm is None:
        return
    s = ui_scale()
    seen = set()
    for win in wm.windows:
        busy = window_busy(win)
        for area in win.screen.areas:
            if not dock_on(area) or not valid_space(area):
                continue
            tree = area.spaces.active.edit_tree
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if tree is None or region is None or tree.library is not None:
                continue
            key = (area.as_pointer(), tree.name_full)
            seen.add(key)
            if busy:
                area.tag_redraw()              # live "release to add" highlight
                continue

            sig = _loc_signature(tree)
            st = _state.get(key)
            if st is None:
                _state[key] = {"sig": sig, "settled_sig": sig,
                               "settled": _positions(tree), "last_capture": None}
                continue
            if sig == st["sig"]:
                if sig == st["settled_sig"]:
                    continue                   # fast path: nothing moved
            else:
                st["sig"] = sig
                if busy is None:               # no modal info: wait until nodes stop moving
                    continue

            settled = st["settled"]
            cur = _positions(tree)
            moved = [n for n in tree.nodes
                     if n.select and n.name in settled and settled[n.name] != cur[n.name]]
            rect = dock_rect(area, region) if moved else None
            if rect and any(inside(rect, *node_center_region(n, region, s)) for n in moved):
                capture = tuple(sorted((n.name, cur[n.name]) for n in moved))
                if capture != st["last_capture"]:   # don't store twice on redo
                    try:
                        store_nodes(tree, [n for n in tree.nodes if n.select])
                        _flash[area.as_pointer()] = time.time()
                    except Exception as e:
                        print("Node Dock: could not store nodes:", e)
                    st["last_capture"] = capture
                for n in tree.nodes:           # nodes jump back where they were
                    if n.name in settled and cur[n.name] != settled[n.name]:
                        n.location = settled[n.name]
                cur = _positions(tree)
                sig = _loc_signature(tree)
                st["sig"] = sig
                area.tag_redraw()
            st["settled"] = cur
            st["settled_sig"] = sig
    for k in [k for k in _state if k not in seen]:     # forget closed editors / trees
        del _state[k]


# ================================================================ drawing

def _rect(shader, x0, y0, x1, y1, color):
    if x1 <= x0 or y1 <= y0:
        return
    batch = batch_for_shader(
        shader, 'TRIS', {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]},
        indices=[(0, 1, 2), (0, 2, 3)])
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rrect(shader, x0, y0, x1, y1, r, color, seg=6):
    """Rounded rectangle (triangle fan from the centre)."""
    if x1 <= x0 or y1 <= y0:
        return
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r < 1:
        _rect(shader, x0, y0, x1, y1, color)
        return
    pts = []
    for cx, cy, a0 in ((x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90),
                       (x0 + r, y0 + r, 180), (x1 - r, y0 + r, 270)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    verts = [((x0 + x1) / 2, (y0 + y1) / 2)] + pts
    idx = [(0, i, i + 1) for i in range(1, len(pts))] + [(0, len(pts), 1)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=idx)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rframe(shader, rect, r, color, t):
    """Rounded outline: a slightly bigger rounded rect under the fill."""
    x0, y0, x1, y1 = rect
    _rrect(shader, x0 - t, y0 - t, x1 + t, y1 + t, r + t, color)


def _text(txt, x, y, size, color, clip=None):
    blf.size(0, size)
    blf.color(0, *color)
    if clip:
        blf.enable(0, blf.CLIPPING)
        blf.clipping(0, *[int(v) for v in clip])
    h = blf.dimensions(0, "Ag")[1]
    blf.position(0, x, y - h / 2, 0)
    blf.draw(0, txt)
    if clip:
        blf.disable(0, blf.CLIPPING)


def _item_color(it):
    h = (sum(ord(c) for c in it.get("id", "")) % 360) / 360.0   # stable across renames
    r, g, b = colorsys.hsv_to_rgb(h, 0.45, 0.85)
    return (r, g, b, 1.0)


def _draw_item_tile(shader, it, a, b, c, d, clip_x0, clip_x1, s, alpha=1.0):
    ca, cc = max(a, clip_x0), min(c, clip_x1)
    if cc <= ca:
        return
    col = _item_color(it)
    r = 7 * s
    # tiles cut by the dock edge are drawn square on that side
    _rrect(shader, ca, b, cc, d, r if (ca == a and cc == c) else 0, (0.17, 0.17, 0.19, alpha))
    if ca == a:                                    # coloured accent bar on the left
        _rrect(shader, a + 5 * s, b + 8 * s, a + 8 * s, d - 8 * s, 1.5 * s,
               (col[0], col[1], col[2], alpha))
    clip = (ca + 6 * s, b, cc - 6 * s, d)
    mid = (b + d) / 2
    _text(it.get("name", "?"), a + 15 * s, mid + 7 * s, 11 * s, (0.94, 0.94, 0.95, alpha), clip)
    n = it.get("count", 0)
    _text("%d node%s" % (n, "" if n == 1 else "s"), a + 15 * s, mid - 9 * s, 9 * s,
          (0.55, 0.57, 0.62, alpha), clip)


def draw():
    ctx = bpy.context
    area, region = ctx.area, ctx.region
    if area is None or region is None or region.type != 'WINDOW':
        return
    rect, tiles, content = layout(area, region)
    if rect is None:
        return
    s = ui_scale()
    x0, y0, x1, y1 = rect
    ptr = area.as_pointer()
    tree = ctx.space_data.edit_tree

    hot = False
    if tree is not None and window_busy(ctx.window):
        hot = any(n.select and inside(rect, *node_center_region(n, region, s)) for n in tree.nodes)
    flash = time.time() - _flash.get(ptr, 0) < 0.8

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    R = 10 * s
    _rrect(shader, x0 + 2 * s, y0 - 3 * s, x1 + 2 * s, y1 - 3 * s, R, (0, 0, 0, 0.25))   # shadow
    if hot:
        _rframe(shader, rect, R, (0.30, 0.60, 1.00, 1), 2 * s)
    elif flash:
        _rframe(shader, rect, R, (0.35, 0.80, 0.45, 1), 2 * s)
    else:
        _rframe(shader, rect, R, (0.26, 0.26, 0.29, 1), 1)
    _rrect(shader, x0, y0, x1, y1, R, (0.10, 0.10, 0.115, 0.96) if not hot else (0.09, 0.12, 0.18, 0.97))

    items = 0
    for kind, it, (a, b, c, d) in tiles:
        if c < x0 or a > x1:
            continue
        if kind == 'ADD':
            ca, cc = max(a, x0 + 1), min(c, x1 - 1)
            whole = ca == a and cc == c
            _rrect(shader, ca, b, cc, d, 7 * s if whole else 0, (0.22, 0.24, 0.30, 1))
            cx, cy, k, t = (a + c) / 2, (b + d) / 2 + 5 * s, 8 * s, 1.3 * s
            if whole:
                _rrect(shader, cx - k, cy - t, cx + k, cy + t, t, (0.85, 0.87, 0.92, 1))
                _rrect(shader, cx - t, cy - k, cx + t, cy + k, t, (0.85, 0.87, 0.92, 1))
                _text("Add", cx - 9 * s, b + 10 * s, 9 * s, (0.7, 0.72, 0.78, 1))
        else:
            items += 1
            _draw_item_tile(shader, it, a, b, c, d, x0 + 1, x1 - 1, s)

    if hot:
        _text("Release to add to the dock", x1 - 180 * s, (y0 + y1) / 2, 11 * s,
              (0.6, 0.8, 1, 1), (x0, y0, x1, y1))
    elif not items:
        add_x1 = tiles[0][2][2]
        _text("Drag nodes here to store them  ·  drag them out to reuse", add_x1 + 12 * s,
              (y0 + y1) / 2, 11 * s, (0.55, 0.55, 0.55, 1), (x0, y0, x1, y1))

    view_w = x1 - x0
    if content > view_w + 1:                       # more items than fit: thin scrollbar
        track0, track1 = x0 + 10 * s, x1 - 10 * s
        span = track1 - track0
        thumb = max(24 * s, span * view_w / content)
        t = _scroll.get(ptr, 0) / max(content - view_w, 1)
        tx0 = track0 + (span - thumb) * t
        _rrect(shader, tx0, y0 + 1.5 * s, tx0 + thumb, y0 + 3.5 * s, s, (0.55, 0.57, 0.62, 0.7))

    dr = _drag.get(ptr)
    if dr and dr.get("active"):
        w, h = 150 * s, (y1 - y0) - 12 * s
        gx, gy = dr["x"] + 8 * s, dr["y"] - h / 2
        _draw_item_tile(shader, dr["item"], gx, gy, gx + w, gy + h, -1e9, 1e9, s, alpha=0.75)

    gpu.state.blend_set('NONE')


# ================================================================ operators

class NDOCK_OT_click(bpy.types.Operator):
    """Click the dock: drag an item out to place it, or click + to store the selection"""
    bl_idname = "ndock.click"
    bl_label = "Node Dock Click"
    bl_options = {'INTERNAL', 'UNDO'}

    def invoke(self, context, event):
        area, region = context.area, context.region
        if region is None or region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        rect, tiles, _ = layout(area, region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        t = tile_at(rect, tiles, mx, my)
        if t is None:
            return {'PASS_THROUGH'}
        if t[0] == 'ADD':
            if NDOCK_OT_store_selected.poll(context):
                bpy.ops.ndock.store_selected()
            else:
                self.report({'WARNING'}, "Open a node tree and select some nodes first")
            return {'CANCELLED'}               # nothing to undo in the scene
        if t[0] == 'DOCK':
            return {'CANCELLED'}               # swallow clicks on empty dock space
        self.item = t[1]
        self.start = (mx, my)
        _drag[area.as_pointer()] = {"item": t[1], "x": mx, "y": my, "active": False}
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _end(self, context):
        _drag.pop(context.area.as_pointer(), None)
        context.window.cursor_modal_restore()
        context.area.tag_redraw()

    def modal(self, context, event):
        ptr = context.area.as_pointer()
        mx, my = event.mouse_region_x, event.mouse_region_y
        dr = _drag.get(ptr)
        if dr is None:
            return {'CANCELLED'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            dr["x"], dr["y"] = mx, my
            if not dr["active"] and abs(mx - self.start[0]) + abs(my - self.start[1]) > 6:
                dr["active"] = True
                context.window.cursor_modal_set('HAND')
            context.area.tag_redraw()
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            active = dr["active"]
            self._end(context)
            rect = dock_rect(context.area, context.region)
            if not active or (rect and inside(rect, mx, my)):
                return {'CANCELLED'}
            err = place_item(context, self.item, mx, my)
            if err:
                self.report({'ERROR'}, err)
                return {'CANCELLED'}
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class NDOCK_OT_store_selected(bpy.types.Operator):
    """Store the selected nodes in the Node Dock"""
    bl_idname = "ndock.store_selected"
    bl_label = "Add Selected to Node Dock"

    @classmethod
    def poll(cls, context):
        sp = context.space_data
        return sp is not None and sp.type == 'NODE_EDITOR' and sp.edit_tree is not None \
            and sp.tree_type == TREE_TYPE

    def execute(self, context):
        tree = context.space_data.edit_tree
        sel = [n for n in tree.nodes if n.select]
        if not sel:
            self.report({'WARNING'}, "Select some nodes first")
            return {'CANCELLED'}
        try:
            name = store_nodes(tree, sel)
        except Exception as e:                 # e.g. library folder not writable
            self.report({'ERROR'}, "Could not store the nodes: %s" % e)
            return {'CANCELLED'}
        if name is None:
            self.report({'WARNING'}, "Group Input/Output nodes can't be stored")
            return {'CANCELLED'}
        if context.area:
            _flash[context.area.as_pointer()] = time.time()
        self.report({'INFO'}, "Added '%s' to the dock" % name)
        return {'FINISHED'}


class NDOCK_OT_scroll(bpy.types.Operator):
    """Scroll the Node Dock"""
    bl_idname = "ndock.scroll"
    bl_label = "Scroll Node Dock"
    bl_options = {'INTERNAL'}

    delta: bpy.props.IntProperty(default=1)

    def invoke(self, context, event):
        area, region = context.area, context.region
        if region is None or region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        rect, tiles, content = layout(area, region)
        mx, my = event.mouse_region_x, event.mouse_region_y
        if rect is None or not inside(rect, mx, my):
            return {'PASS_THROUGH'}
        if event.type == 'TRACKPADPAN':
            dx = event.mouse_x - event.mouse_prev_x
            dy = event.mouse_y - event.mouse_prev_y
            step = -dx if abs(dx) >= abs(dy) else dy
        else:
            step = self.delta * 60 * ui_scale()
        ptr = area.as_pointer()
        max_off = max(0, content - (rect[2] - rect[0]))
        _scroll[ptr] = max(0, min(max_off, _scroll.get(ptr, 0) + step))
        area.tag_redraw()
        return {'FINISHED'}


class NDOCK_OT_menu(bpy.types.Operator):
    """Right-click menu in the Node Dock"""
    bl_idname = "ndock.menu"
    bl_label = "Node Dock Menu"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        region = context.region
        if region is None or region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        rect, tiles, _ = layout(context.area, region)
        t = tile_at(rect, tiles, event.mouse_region_x, event.mouse_region_y)
        if t is None:
            return {'PASS_THROUGH'}
        if t[0] == 'ITEM':
            iid = t[1]["id"]

            def draw_menu(menu, ctx):
                lay = menu.layout
                lay.operator_context = 'INVOKE_DEFAULT'    # so Rename opens its dialog
                lay.operator(NDOCK_OT_rename.bl_idname, text="Rename", icon='GREASEPENCIL').item_id = iid
                lay.operator(NDOCK_OT_delete.bl_idname, text="Delete", icon='TRASH').item_id = iid
            context.window_manager.popup_menu(draw_menu, title=t[1].get("name", ""), icon='NODETREE')
        else:
            def draw_menu(menu, ctx):
                lay = menu.layout
                lay.operator_context = 'INVOKE_DEFAULT'
                lay.operator(NDOCK_OT_store_selected.bl_idname, icon='ADD')
                lay.operator(NDOCK_OT_open_folder.bl_idname, icon='FILE_FOLDER')
            context.window_manager.popup_menu(draw_menu, title="Node Dock", icon='BOOKMARKS')
        return {'FINISHED'}


class NDOCK_OT_rename(bpy.types.Operator):
    """Rename this dock item"""
    bl_idname = "ndock.rename"
    bl_label = "Rename Dock Item"
    bl_options = {'INTERNAL'}

    item_id: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})
    new_name: bpy.props.StringProperty(name="Name", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        it = find_item(self.item_id)
        if it is None:
            return {'CANCELLED'}
        self.new_name = it.get("name", "")
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.activate_init = True       # start typing right away
        self.layout.prop(self, "new_name", text="")

    def execute(self, context):
        name = self.new_name.strip()
        if not name:
            return {'CANCELLED'}
        items = [dict(i) for i in get_items()]
        for i in items:
            if i.get("id") == self.item_id:
                i["name"] = name
        save_items(items)
        return {'FINISHED'}


class NDOCK_OT_double_click(bpy.types.Operator):
    """Double-click a dock item to rename it"""
    bl_idname = "ndock.double_click"
    bl_label = "Node Dock Double Click"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        region = context.region
        if region is None or region.type != 'WINDOW':
            return {'PASS_THROUGH'}
        rect, tiles, _ = layout(context.area, region)
        t = tile_at(rect, tiles, event.mouse_region_x, event.mouse_region_y)
        if t is None:
            return {'PASS_THROUGH'}
        if t[0] == 'ITEM':
            bpy.ops.ndock.rename('INVOKE_DEFAULT', item_id=t[1]["id"])
        return {'CANCELLED'}


class NDOCK_OT_delete(bpy.types.Operator):
    """Delete this dock item from the library"""
    bl_idname = "ndock.delete"
    bl_label = "Delete Dock Item"
    bl_options = {'INTERNAL'}

    item_id: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        it = find_item(self.item_id)
        if it is None:
            return {'CANCELLED'}
        try:
            os.remove(item_file(it))
        except OSError:
            pass
        save_items([i for i in get_items() if i.get("id") != self.item_id])
        return {'FINISHED'}


class NDOCK_OT_open_folder(bpy.types.Operator):
    """Open the folder where dock items are saved"""
    bl_idname = "ndock.open_folder"
    bl_label = "Open Library Folder"

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=lib_dir())
        return {'FINISHED'}


class NDOCK_OT_toggle(bpy.types.Operator):
    """Show/hide the Node Dock in this editor"""
    bl_idname = "ndock.toggle"
    bl_label = "Toggle Node Dock"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'NODE_EDITOR'

    def execute(self, context):
        ptr = context.area.as_pointer()
        # forget editors that were closed: their address can be reused by a new one
        alive = {a.as_pointer() for w in context.window_manager.windows for a in w.screen.areas}
        _dock_areas.intersection_update(alive)
        for d in (_scroll, _drag, _flash):
            for k in [k for k in d if k not in alive]:
                del d[k]
        if ptr in _dock_areas:
            _dock_areas.discard(ptr)
        else:
            _dock_areas.add(ptr)
        context.area.tag_redraw()
        return {'FINISHED'}


# ================================================================ UI

def header_button(self, context):
    sp = context.space_data
    if sp is None or sp.tree_type != TREE_TYPE:
        return
    self.layout.operator(NDOCK_OT_toggle.bl_idname, text="", icon='BOOKMARKS',
                         depress=dock_on(context.area))


def node_menu(self, context):
    sp = context.space_data
    if sp is None or sp.tree_type != TREE_TYPE:
        return
    self.layout.separator()
    self.layout.operator(NDOCK_OT_store_selected.bl_idname, icon='BOOKMARKS')


@persistent
def _on_load(*_args):
    for d in (_dock_areas, _scroll, _drag, _flash, _state):
        d.clear()


# ================================================================ register

classes = (NDOCK_Preferences, NDOCK_OT_click, NDOCK_OT_store_selected, NDOCK_OT_scroll,
           NDOCK_OT_menu, NDOCK_OT_rename, NDOCK_OT_double_click, NDOCK_OT_delete,
           NDOCK_OT_open_folder, NDOCK_OT_toggle)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    _handles.append(bpy.types.SpaceNodeEditor.draw_handler_add(draw, (), 'WINDOW', 'POST_PIXEL'))
    bpy.types.NODE_HT_header.append(header_button)
    bpy.types.NODE_MT_node.append(node_menu)
    bpy.app.handlers.load_post.append(_on_load)
    bpy.app.timers.register(_tick, first_interval=0.5, persistent=True)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='Node Editor', space_type='NODE_EDITOR')

        def add(idname, key, value='PRESS'):
            kmi = km.keymap_items.new(idname, key, value)
            _keymaps.append((km, kmi))
            return kmi

        add(NDOCK_OT_double_click.bl_idname, 'LEFTMOUSE', 'DOUBLE_CLICK')
        add(NDOCK_OT_click.bl_idname, 'LEFTMOUSE')
        add(NDOCK_OT_menu.bl_idname, 'RIGHTMOUSE')
        add(NDOCK_OT_scroll.bl_idname, 'WHEELUPMOUSE').properties.delta = -1
        add(NDOCK_OT_scroll.bl_idname, 'WHEELDOWNMOUSE').properties.delta = 1
        add(NDOCK_OT_scroll.bl_idname, 'TRACKPADPAN', 'ANY')


def unregister():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for h in _handles:
        bpy.types.SpaceNodeEditor.draw_handler_remove(h, 'WINDOW')
    _handles.clear()
    bpy.types.NODE_MT_node.remove(node_menu)
    bpy.types.NODE_HT_header.remove(header_button)
    _on_load()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
