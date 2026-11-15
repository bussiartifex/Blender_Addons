import bpy
import blf
import gpu
import math
from gpu_extras.batch import batch_for_shader
from bpy.app.handlers import persistent

ADDON_ID = __package__ or __name__
TEXT_NAME = "VTimeline"      # a Text Editor showing this text = dedicated timeline area

TOP_MARGIN = 60              # overlay: keep clear of the header
BOTTOM_MARGIN = 20
PAD = 14                     # px between strip edge and first/last frame

_handles = []
_keymaps = []
_overlay_areas = set()       # 3D Viewports where the overlay is on (this session)
_pan_accum = [0.0]           # leftover trackpad distance between frame steps
_wipe_pending = [False]


# ================================================================ prefs

def _redraw_all(self=None, context=None):
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type in {'VIEW_3D', 'TEXT_EDITOR'}:
                area.tag_redraw()


class VTL_Preferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    side: bpy.props.EnumProperty(
        name="Overlay Side",
        items=[('LEFT', "Left", ""), ('RIGHT', "Right", "")],
        default='LEFT', update=_redraw_all,
        description="Side of the 3D Viewport where the overlay is drawn")
    width: bpy.props.IntProperty(
        name="Width", default=64, min=40, max=200, subtype='PIXEL', update=_redraw_all)
    opacity: bpy.props.FloatProperty(
        name="Overlay Opacity", default=0.85, min=0.0, max=1.0,
        subtype='FACTOR', update=_redraw_all)
    invert_trackpad: bpy.props.BoolProperty(
        name="Invert Trackpad Scroll", default=False,
        description="Flip the direction of two-finger trackpad scrolling")
    trackpad_speed: bpy.props.FloatProperty(
        name="Trackpad Speed", default=1.0, min=0.1, max=5.0,
        description="Frames per trackpad scroll distance")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Appearance", icon='TIME')
        col = box.column()
        col.use_property_split = True
        col.prop(self, "side")
        col.prop(self, "width")
        col.prop(self, "opacity", slider=True)
        box = layout.box()
        box.label(text="Trackpad", icon='MOUSE_MMB')
        col = box.column()
        col.use_property_split = True
        col.prop(self, "invert_trackpad")
        col.prop(self, "trackpad_speed")
        box = layout.box()
        box.label(text="How to use", icon='INFO')
        col = box.column(align=True)
        col.active = False
        col.label(text="Clock button in the 3D Viewport header: overlay on/off")
        col.label(text="View > Vertical Timeline Area: dedicated area on the left")
        col.label(text="Click/drag to scrub · Wheel to step · Space to play")
        col.label(text="In the area: arrows step / jump keys, Shift+arrows go to start / end")


def prefs():
    return bpy.context.preferences.addons[ADDON_ID].preferences


# ================================================================ helpers

def is_vt_area(area):
    if area is None or area.type != 'TEXT_EDITOR':
        return False
    sp = area.spaces.active
    return sp.text is not None and sp.text.name == TEXT_NAME


def overlay_on(area):
    return area is not None and area.type == 'VIEW_3D' and area.as_pointer() in _overlay_areas


def frame_range(scene):
    if scene.use_preview_range:
        return scene.frame_preview_start, scene.frame_preview_end
    return scene.frame_start, scene.frame_end


def strip_rect(context, area, region):
    """Strip rectangle (x0, y0, x1, y1) in region pixels, or None."""
    ui = context.preferences.system.ui_scale
    if is_vt_area(area):                       # dedicated area: use all of it
        return 0, 6 * ui, region.width, region.height - 6 * ui
    if not overlay_on(area):
        return None
    p = prefs()
    w = p.width * ui
    left_off = right_off = 0
    if context.preferences.system.use_region_overlap:
        for r in area.regions:
            if r.type == 'TOOLS' and r.width > 1:
                left_off = r.width
            elif r.type == 'UI' and r.width > 1:
                right_off = r.width
    if p.side == 'LEFT':
        x0 = left_off
        x1 = x0 + w
    else:
        x1 = region.width - right_off
        x0 = x1 - w
    return x0, BOTTOM_MARGIN * ui, x1, region.height - TOP_MARGIN * ui


def hit_strip(context, event):
    """Strip rect if the mouse is over a visible strip, else None."""
    region = context.region
    if region is None or region.type != 'WINDOW':
        return None
    rect = strip_rect(context, context.area, region)
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    mx, my = event.mouse_region_x, event.mouse_region_y
    return rect if (x0 <= mx <= x1 and y0 <= my <= y1) else None


def frame_to_y(f, s, e, y0, y1):
    span = max(e - s, 1)
    return (y0 + PAD) + (f - s) / span * ((y1 - PAD) - (y0 + PAD))


def y_to_frame(y, s, e, y0, y1):
    span = max(e - s, 1)
    t = (y - (y0 + PAD)) / max((y1 - PAD) - (y0 + PAD), 1)
    return max(s, min(e, round(s + t * span)))


def tick_step(ppf):
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000):
        if step * ppf >= 26:
            return step
    return 10000


# ================================================================ drawing

def _rect(shader, x0, y0, x1, y1, color):
    batch = batch_for_shader(
        shader, 'TRIS', {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]},
        indices=[(0, 1, 2), (0, 2, 3)])
    shader.uniform_float("color", color)
    batch.draw(shader)


def _rrect(shader, x0, y0, x1, y1, r, color, seg=6):
    """Rounded rectangle (triangle fan from the centre)."""
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
    centre = ((x0 + x1) / 2, (y0 + y1) / 2)
    verts = [centre] + pts
    idx = [(0, i, i + 1) for i in range(1, len(pts))] + [(0, len(pts), 1)]
    batch = batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=idx)
    shader.uniform_float("color", color)
    batch.draw(shader)


def _theme_playhead():
    theme = bpy.context.preferences.themes[0]
    for path in (("common", "anim", "playhead"),            # Blender 5.0+
                 ("dopesheet_editor", "frame_current")):    # Blender 4.x
        try:
            c = theme
            for name in path:
                c = getattr(c, name)
            return (c[0], c[1], c[2], 1.0)
        except Exception:
            continue
    return (0.29, 0.55, 1.0, 1.0)


def _text(txt, x, y, size, color, align='LEFT'):
    blf.size(0, size)
    blf.color(0, *color)
    w, h = blf.dimensions(0, txt)
    if align == 'RIGHT':
        x -= w
    elif align == 'CENTER':
        x -= w / 2
    blf.position(0, x, y - h / 2, 0)
    blf.draw(0, txt)


def draw():
    ctx = bpy.context
    area, region, scene = ctx.area, ctx.region, ctx.scene
    if area is None or region is None or region.type != 'WINDOW':
        return
    rect = strip_rect(ctx, area, region)
    if rect is None:
        return
    x0, y0, x1, y1 = rect
    if y1 - y0 < 60:
        return

    dedicated = is_vt_area(area)
    if dedicated and not _wipe_pending[0] and area.spaces.active.text.as_string():
        _wipe_pending[0] = True                 # anything typed in the hidden text: clear it
        bpy.app.timers.register(_wipe_text, first_interval=0.0)

    s, e = frame_range(scene)
    cur = scene.frame_current
    ui = ctx.preferences.system.ui_scale
    tx = x1 - 14 * ui

    # shrink the font if the longest label doesn't fit in the strip
    fs = 11 * ui
    blf.size(0, fs)
    lw = max(blf.dimensions(0, str(s))[0], blf.dimensions(0, str(e))[0])
    avail = (tx - 10 * ui) - x0 - 3 * ui
    if 0 < avail < lw:
        fs = max(6 * ui, fs * avail / lw)

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    accent = _theme_playhead()
    inside = s <= cur <= e
    col = accent if inside else (0.90, 0.35, 0.30, 1)
    yc = frame_to_y(max(s, min(e, cur)), s, e, y0, y1)
    ys, ye = frame_to_y(s, s, e, y0, y1), frame_to_y(e, s, e, y0, y1)

    if dedicated:
        _rect(shader, 0, 0, region.width, region.height, (0.13, 0.13, 0.14, 1))
    else:                                       # floating card with a soft border
        op = prefs().opacity
        _rrect(shader, x0 - 1, y0 - 1, x1 + 1, y1 + 1, 9 * ui, (0.3, 0.3, 0.32, op * 0.6))
        _rrect(shader, x0, y0, x1, y1, 8 * ui, (0.09, 0.09, 0.10, op))

    # track, and the played part filled with the accent colour
    _rrect(shader, tx - 2.5 * ui, ys, tx + 2.5 * ui, ye, 2.5 * ui, (0.26, 0.26, 0.28, 1))
    if inside:
        _rrect(shader, tx - 2.5 * ui, ys, tx + 2.5 * ui, yc, 2.5 * ui,
               (accent[0], accent[1], accent[2], 0.55))

    if scene.use_preview_range:                 # same orange Blender uses for the preview range
        _text("PREVIEW", (x0 + x1) / 2, y1 - 7 * ui, 8 * ui, (0.95, 0.6, 0.25, 1), 'CENTER')

    # ticks: start, end, and round numbers that don't crowd them
    ppf = ((y1 - PAD) - (y0 + PAD)) / max(e - s, 1)
    step = tick_step(ppf)
    frames = [s, e]
    f = ((s + step - 1) // step) * step
    while f <= e:
        if abs(f - s) * ppf > 16 and abs(e - f) * ppf > 16:
            frames.append(f)
        f += step
    for f in frames:
        y = frame_to_y(f, s, e, y0, y1)
        bold = f in (s, e)
        _rect(shader, tx - 6 * ui, y - (1 if bold else 0.5),
              tx + 6 * ui, y + (1 if bold else 0.5), (0.6, 0.6, 0.6, 1))
        _text(str(f), tx - 10 * ui, y, fs,
              (0.9, 0.9, 0.9, 1) if bold else (0.6, 0.6, 0.6, 1), 'RIGHT')

    # playhead: thin line + rounded pill with the frame number
    _rect(shader, x0 + 2 * ui, yc - 0.75 * ui, x1 - 2 * ui, yc + 0.75 * ui, col)
    blf.size(0, fs * 1.1)
    tw, th = blf.dimensions(0, str(cur))
    cx = (x0 + tx) / 2
    ph = th / 2 + 5 * ui
    _rrect(shader, cx - tw / 2 - 7 * ui, yc - ph, cx + tw / 2 + 7 * ui, yc + ph, ph, col)
    _text(str(cur), cx, yc, fs * 1.1, (1, 1, 1, 1), 'CENTER')

    gpu.state.blend_set('NONE')


# ================================================================ operators

class VTL_OT_scrub(bpy.types.Operator):
    """Click or drag on the vertical timeline to change frame"""
    bl_idname = "vtl.scrub"
    bl_label = "Scrub Vertical Timeline"
    bl_options = {'INTERNAL'}

    def _set(self, context, event):
        scene = context.scene
        s, e = frame_range(scene)
        f = y_to_frame(event.mouse_region_y, s, e, self.y0, self.y1)
        if f != scene.frame_current:
            scene.frame_current = f
        context.area.tag_redraw()

    def invoke(self, context, event):
        rect = hit_strip(context, event)
        if rect is None:
            return {'PASS_THROUGH'}
        self.y0, self.y1 = rect[1], rect[3]
        self.start_frame = context.scene.frame_current
        self._set(context, event)
        context.window.cursor_modal_set('MOVE_Y')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._set(context, event)
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            context.window.cursor_modal_restore()
            return {'FINISHED'}
        elif event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            context.scene.frame_current = self.start_frame   # cancel: restore frame
            context.window.cursor_modal_restore()
            if context.area:
                context.area.tag_redraw()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class VTL_OT_step(bpy.types.Operator):
    """Step one frame with the mouse wheel over the vertical timeline"""
    bl_idname = "vtl.step"
    bl_label = "Step Vertical Timeline"
    bl_options = {'INTERNAL'}

    delta: bpy.props.IntProperty(default=1)

    def invoke(self, context, event):
        if hit_strip(context, event) is None:
            return {'PASS_THROUGH'}
        scene = context.scene
        s, e = frame_range(scene)
        scene.frame_current = max(s, min(e, scene.frame_current + self.delta))
        context.area.tag_redraw()
        return {'FINISHED'}


class VTL_OT_trackpad(bpy.types.Operator):
    """Two-finger trackpad scroll over the vertical timeline"""
    bl_idname = "vtl.trackpad"
    bl_label = "Trackpad Scroll Vertical Timeline"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        if hit_strip(context, event) is None:
            return {'PASS_THROUGH'}
        p = prefs()
        dy = event.mouse_y - event.mouse_prev_y
        if getattr(event, "is_direction_inverted", False):   # not present in all versions
            dy = -dy
        if p.invert_trackpad:
            dy = -dy
        _pan_accum[0] += dy
        step_px = 12 * context.preferences.system.ui_scale / p.trackpad_speed
        n = int(_pan_accum[0] / step_px)
        if n:
            _pan_accum[0] -= n * step_px
            scene = context.scene
            s, e = frame_range(scene)
            scene.frame_current = max(s, min(e, scene.frame_current + n))
            context.area.tag_redraw()
        return {'FINISHED'}


class VTL_OT_key(bpy.types.Operator):
    """Playback and frame keys inside a vertical timeline area"""
    bl_idname = "vtl.key"
    bl_label = "Vertical Timeline Key"
    bl_options = {'INTERNAL'}

    action: bpy.props.EnumProperty(items=[
        ('PLAY', "Play", ""), ('PLAY_REVERSE', "Play Reverse", ""),
        ('NEXT_KEY', "Next Keyframe", ""), ('PREV_KEY', "Previous Keyframe", ""),
        ('NEXT_FRAME', "Next Frame", ""), ('PREV_FRAME', "Previous Frame", ""),
        ('START', "Jump to Start", ""), ('END', "Jump to End", "")])

    def invoke(self, context, event):
        if not is_vt_area(context.area):
            return {'PASS_THROUGH'}
        a = self.action
        try:
            if a == 'PLAY':
                bpy.ops.screen.animation_play()
            elif a == 'PLAY_REVERSE':
                bpy.ops.screen.animation_play(reverse=True)
            elif a in {'NEXT_KEY', 'PREV_KEY'}:
                bpy.ops.screen.keyframe_jump(next=(a == 'NEXT_KEY'))
            elif a in {'START', 'END'}:
                bpy.ops.screen.frame_jump(end=(a == 'END'))
            else:
                bpy.ops.screen.frame_offset(delta=1 if a == 'NEXT_FRAME' else -1)
        except RuntimeError as e:
            self.report({'INFO'}, str(e).split("\n")[-1] or "Nothing to jump to")
        context.area.tag_redraw()
        return {'FINISHED'}


class VTL_OT_block(bpy.types.Operator):
    """Swallow typing inside a vertical timeline area"""
    bl_idname = "vtl.block"
    bl_label = "Block Typing"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        if not is_vt_area(context.area):
            return {'PASS_THROUGH'}
        # Space also counts as typed text, so it can land here: play instead of swallowing it
        if event.type == 'SPACE' and event.value == 'PRESS':
            try:
                bpy.ops.screen.animation_play(reverse=event.ctrl and event.shift)
            except RuntimeError:
                pass
        return {'FINISHED'}


class VTL_OT_toggle_overlay(bpy.types.Operator):
    """Show/hide the vertical timeline in this 3D Viewport"""
    bl_idname = "vtl.toggle_overlay"
    bl_label = "Toggle Vertical Timeline"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def execute(self, context):
        key = context.area.as_pointer()
        # forget areas that were closed: their address can be reused by a new one
        alive = {a.as_pointer() for w in context.window_manager.windows for a in w.screen.areas}
        _overlay_areas.intersection_update(alive)
        if key in _overlay_areas:
            _overlay_areas.discard(key)
        else:
            _overlay_areas.add(key)
        context.area.tag_redraw()
        return {'FINISHED'}


def _make_vt_area(area):
    area.type = 'TEXT_EDITOR'
    sp = area.spaces.active
    txt = bpy.data.texts.get(TEXT_NAME) or bpy.data.texts.new(TEXT_NAME)
    txt.use_fake_user = True
    sp.text = txt
    for attr in ("show_line_numbers", "show_syntax_highlight", "show_word_wrap",
                 "show_region_header", "show_region_footer", "show_region_ui"):
        if hasattr(sp, attr):
            try:
                setattr(sp, attr, False)
            except Exception:
                pass
    area.tag_redraw()


class VTL_OT_open_area(bpy.types.Operator):
    """Open a vertical timeline area on the left of this editor, or close it if already open"""
    bl_idname = "vtl.open_area"
    bl_label = "Toggle Vertical Timeline Area"

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.screen is not None

    def execute(self, context):
        area = context.area
        screen = context.screen
        win = context.window

        # Already open: just close it (closing and splitting are never done in one call).
        existing = [a for a in screen.areas if is_vt_area(a)]
        if existing:
            for vt in existing:
                vt_region = next((r for r in vt.regions if r.type == 'WINDOW'), None)
                with context.temp_override(window=win, screen=screen, area=vt, region=vt_region):
                    bpy.ops.screen.area_close()
            return {'FINISHED'}

        # Split: with factor < 0.5 Blender creates the new (narrow) area on the left.
        w = (prefs().width + 20) * context.preferences.system.ui_scale
        factor = min(0.4, w / max(area.width, 1))
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
        before = {a.as_pointer() for a in screen.areas}
        with context.temp_override(window=win, screen=screen, area=area, region=region):
            res = bpy.ops.screen.area_split(direction='VERTICAL', factor=factor)
        if 'FINISHED' not in res:
            self.report({'ERROR'}, "Area too small to split")
            return {'CANCELLED'}
        new = next((a for a in screen.areas if a.as_pointer() not in before), None)
        if new is None:
            return {'CANCELLED'}
        _make_vt_area(new)
        return {'FINISHED'}


class VTL_OT_make_area(bpy.types.Operator):
    """Turn the area under the mouse into a vertical timeline"""
    bl_idname = "vtl.make_area"
    bl_label = "Make This Area a Vertical Timeline"

    @classmethod
    def poll(cls, context):
        return context.area is not None

    def execute(self, context):
        _make_vt_area(context.area)
        return {'FINISHED'}


class VTL_OT_area_menu(bpy.types.Operator):
    """Right-click menu inside a vertical timeline area"""
    bl_idname = "vtl.area_menu"
    bl_label = "Vertical Timeline Menu"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        if not is_vt_area(context.area):
            return {'PASS_THROUGH'}

        def draw_menu(menu, ctx):
            menu.layout.operator("screen.area_close", text="Close Area", icon='X')
        context.window_manager.popup_menu(draw_menu, title="Vertical Timeline", icon='TIME')
        return {'FINISHED'}


# ================================================================ UI

def header_button(self, context):
    on = overlay_on(context.area)
    self.layout.operator(VTL_OT_toggle_overlay.bl_idname, text="", icon='TIME', depress=on)


def view_menu(self, context):
    layout = self.layout
    layout.separator()
    layout.operator(VTL_OT_toggle_overlay.bl_idname, text="Vertical Timeline Overlay",
                    icon='CHECKBOX_HLT' if overlay_on(context.area) else 'CHECKBOX_DEHLT')
    layout.operator(VTL_OT_open_area.bl_idname, text="Vertical Timeline Area", icon='TIME')


# ================================================================ redraw during playback

@persistent
def _redraw_areas(*_args):
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if is_vt_area(area) or overlay_on(area):
                area.tag_redraw()


def _wipe_text():
    _wipe_pending[0] = False
    t = bpy.data.texts.get(TEXT_NAME)
    if t is not None and t.as_string():
        t.clear()
    return None


@persistent
def _on_load(*_args):
    _overlay_areas.clear()   # area pointers from the previous file are invalid


# ================================================================ register

classes = (VTL_Preferences, VTL_OT_scrub, VTL_OT_step, VTL_OT_trackpad, VTL_OT_key, VTL_OT_block,
           VTL_OT_toggle_overlay, VTL_OT_open_area, VTL_OT_make_area, VTL_OT_area_menu)


def register():
    for c in classes:
        bpy.utils.register_class(c)

    _handles.append((bpy.types.SpaceView3D,
                     bpy.types.SpaceView3D.draw_handler_add(draw, (), 'WINDOW', 'POST_PIXEL')))
    _handles.append((bpy.types.SpaceTextEditor,
                     bpy.types.SpaceTextEditor.draw_handler_add(draw, (), 'WINDOW', 'POST_PIXEL')))

    bpy.types.VIEW3D_HT_header.append(header_button)
    bpy.types.VIEW3D_MT_view.append(view_menu)
    bpy.app.handlers.frame_change_post.append(_redraw_areas)
    bpy.app.handlers.depsgraph_update_post.append(_redraw_areas)
    bpy.app.handlers.load_post.append(_on_load)

    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        def add(km, idname, key, value='PRESS', **kw):
            kmi = km.keymap_items.new(idname, key, value, **kw)
            _keymaps.append((km, kmi))
            return kmi

        for name, space in (('3D View', 'VIEW_3D'), ('Text', 'TEXT_EDITOR')):
            km = kc.keymaps.new(name=name, space_type=space)
            add(km, VTL_OT_scrub.bl_idname, 'LEFTMOUSE')
            add(km, VTL_OT_step.bl_idname, 'WHEELUPMOUSE').properties.delta = 1
            add(km, VTL_OT_step.bl_idname, 'WHEELDOWNMOUSE').properties.delta = -1
            add(km, VTL_OT_trackpad.bl_idname, 'TRACKPADPAN', 'ANY')

        km = kc.keymaps.new(name='Text', space_type='TEXT_EDITOR')
        add(km, VTL_OT_area_menu.bl_idname, 'RIGHTMOUSE')
        # playback / frame keys must come before the typing blocker
        for key, action, kw in (
                ('SPACE', 'PLAY', {}),
                ('SPACE', 'PLAY_REVERSE', {'ctrl': True, 'shift': True}),
                ('UP_ARROW', 'NEXT_KEY', {}),
                ('DOWN_ARROW', 'PREV_KEY', {}),
                ('RIGHT_ARROW', 'NEXT_FRAME', {}),
                ('LEFT_ARROW', 'PREV_FRAME', {}),
                ('RIGHT_ARROW', 'END', {'shift': True}),
                ('LEFT_ARROW', 'START', {'shift': True})):
            add(km, VTL_OT_key.bl_idname, key, 'PRESS', **kw).properties.action = action
        for key in ('TEXTINPUT', 'RET', 'NUMPAD_ENTER', 'BACK_SPACE', 'DEL', 'TAB'):
            add(km, VTL_OT_block.bl_idname, key, 'ANY', any=True)
        add(km, VTL_OT_block.bl_idname, 'V', ctrl=True)
        add(km, VTL_OT_block.bl_idname, 'X', ctrl=True)


def unregister():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()
    for space, h in _handles:
        space.draw_handler_remove(h, 'WINDOW')
    _handles.clear()
    for lst in (bpy.app.handlers.frame_change_post, bpy.app.handlers.depsgraph_update_post):
        if _redraw_areas in lst:
            lst.remove(_redraw_areas)
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if bpy.app.timers.is_registered(_wipe_text):
        bpy.app.timers.unregister(_wipe_text)
    _wipe_pending[0] = False
    bpy.types.VIEW3D_MT_view.remove(view_menu)
    bpy.types.VIEW3D_HT_header.remove(header_button)
    _overlay_areas.clear()
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
