"""Phone Gyro: rotate the camera, objects, bones or the viewport with an Android phone."""

import bpy
import math
import os
import socket
import time
from mathutils import Matrix, Quaternion, Vector

ADDON_ID = __package__ or __name__

_sock = None
_rx = {"q": None, "seq": -1, "rec": False, "recenter": None, "last": 0.0,
       "addr": "", "count": 0, "rate": 0, "t0": 0.0, "new": False, "since": 0.0}
_ctl = {"offset": None, "smooth": None, "need_recenter": True, "home": None,
        "home_key": None, "started_play": False, "ips": None,
        "t_apply": 0.0, "t_ui": 0.0, "t_err": 0.0, "t_ack": 0.0}


def _reset_tracking(self=None, context=None):
    """Forget Home and start again (target, hold or camera changed)."""
    _ctl.update(need_recenter=True, smooth=None, home=None, home_key=None)


# ================================================================ settings

class PG_Preferences(bpy.types.AddonPreferences):
    """Settings kept between sessions."""
    bl_idname = ADDON_ID

    port: bpy.props.IntProperty(
        name="Port", default=9876, min=1024, max=65535,
        description="UDP port the phone sends to (same as in the app)")
    hold: bpy.props.EnumProperty(
        name="Phone Held",
        items=[('PORTRAIT', "Portrait", "Upright, screen facing you"),
               ('LANDSCAPE_LEFT', "Landscape · Top Left", "Sideways, top of the phone to the left"),
               ('LANDSCAPE_RIGHT', "Landscape · Top Right", "Sideways, top of the phone to the right"),
               ('FLAT', "Flat · Screen Up",
                "Lying flat like a plane: tilt the top down/up to pitch, "
                "tilt sideways to roll, turn it to change heading")],
        default='LANDSCAPE_LEFT', update=_reset_tracking)
    smoothing: bpy.props.FloatProperty(
        name="Smoothing", default=0.3, min=0.0, max=0.95, subtype='FACTOR',
        description="Removes hand jitter. Higher = smoother but slower to follow")
    keep_horizon: bpy.props.BoolProperty(
        name="Keep Horizon", default=True,
        description="Camera/viewport tilt follows gravity; Recenter only changes the heading. "
                    "Off: Recenter matches the phone's whole pose to Home")
    lock_roll: bpy.props.BoolProperty(
        name="Lock Roll", default=False,
        description="Camera/viewport never rolls: only pan and tilt follow the phone")
    auto_play: bpy.props.BoolProperty(
        name="Phone Record Starts Playback", default=True,
        description="Record on the phone starts the playback, pressing it again stops it")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Connection", icon='URL')
        col = box.column()
        col.use_property_split = True
        col.prop(self, "port")
        box = layout.box()
        box.label(text="Control", icon='ORIENTATION_GIMBAL')
        col = box.column()
        col.use_property_split = True
        for p in ("hold", "smoothing", "keep_horizon", "lock_roll", "auto_play"):
            col.prop(self, p, slider=(p == "smoothing"))
        box = layout.box()
        box.label(text="How to use", icon='INFO')
        col = box.column(align=True)
        col.active = False
        col.label(text="3D Viewport sidebar > Phone Gyro > Start Listening")
        col.label(text="Type the PC address shown there into the phone app")
        col.label(text="Phone: Volume Down = Recenter, Volume Up = Record")


class PG_Session(bpy.types.PropertyGroup):
    """Settings for the current session only."""
    target: bpy.props.EnumProperty(
        name="Target",
        items=[('CAMERA', "Camera", "Rotate the scene's active camera", 'CAMERA_DATA', 0),
               ('VIEW', "Viewport", "Rotate the largest 3D Viewport", 'VIEW3D', 1),
               ('OBJECT', "Object", "Rotate the active object", 'OBJECT_DATA', 2),
               ('BONE', "Bone", "Rotate the active pose bone", 'BONE_DATA', 3)],
        default='CAMERA', update=_reset_tracking)
    record: bpy.props.BoolProperty(
        name="Record", default=False,
        description="Insert rotation keyframes on every frame while the animation plays")


def prefs():
    return bpy.context.preferences.addons[ADDON_ID].preferences


def session(context=None):
    return (context or bpy.context).window_manager.phone_gyro


# ================================================================ network

def local_ips():
    """This PC's IPv4 addresses, the one used for the default route first."""
    if _ctl["ips"] is None:
        ips = []
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))     # no packet is sent
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip not in ips and not ip.startswith("127."):
                    ips.append(ip)
        except OSError:
            pass
        _ctl["ips"] = ips or ["127.0.0.1"]
    return _ctl["ips"]


def is_listening():
    return _sock is not None


def start_listening(port):
    global _sock
    stop_listening()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if os.name != 'nt':
        # not on Windows: there it would let a second Blender share the port silently,
        # and only one of the two would receive the phone
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        s.close()
        raise
    s.setblocking(False)
    if hasattr(socket, "SIO_UDP_CONNRESET"):      # Windows: phone closed -> don't break reads
        try:
            s.ioctl(socket.SIO_UDP_CONNRESET, False)
        except OSError:
            pass
    _sock = s
    now = time.time()
    _rx.update(q=None, seq=-1, rec=False, recenter=None, last=0.0, count=0, rate=0,
               t0=now, since=now)
    _ctl["ips"] = None
    _reset_tracking()
    if not bpy.app.timers.is_registered(_tick):
        # persistent: keeps listening after opening another .blend file
        bpy.app.timers.register(_tick, first_interval=0.01, persistent=True)


def stop_listening():
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
    _sock = None
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _ctl["started_play"]:                      # stop the playback we started
        try:
            _playback(False)
        except Exception:
            pass
        _ctl["started_play"] = False
    _rx["rec"] = False


def _read_packets():
    """Read everything waiting; keep the newest orientation."""
    while True:
        try:
            data, addr = _sock.recvfrom(256)
        except (BlockingIOError, OSError):
            return
        parts = data.decode("ascii", "ignore").split()
        if len(parts) < 8 or parts[0] != "GY1":
            continue
        try:
            seq = int(parts[1])
            w, x, y, z = (float(v) for v in parts[2:6])
            rec = parts[6] == "1"
            rc = int(parts[7])
        except ValueError:
            continue
        now = time.time()
        # drop late packets; after a pause (app restarted / reconnected) accept anything
        if seq <= _rx["seq"] and now - _rx["last"] < 0.5:
            continue
        q = Quaternion((w, x, y, z))
        if q.magnitude < 1e-6:
            continue
        _rx["seq"] = seq
        _rx["q"] = q.normalized()
        _rx["addr"] = addr[0]
        _rx["last"] = now
        _rx["count"] += 1
        _rx["new"] = True
        if _rx["recenter"] is not None and rc != _rx["recenter"]:
            _ctl["need_recenter"] = True
        _rx["recenter"] = rc
        if rec != _rx["rec"]:
            _rx["rec"] = rec
            _on_phone_record(rec)
        if now - _ctl["t_ack"] > 0.5:
            _ctl["t_ack"] = now
            _send_ack(addr)


_TARGET_LABEL = {'CAMERA': "Camera", 'VIEW': "Viewport", 'OBJECT': "Object", 'BONE': "Bone"}


def _send_ack(addr):
    try:
        label = _TARGET_LABEL.get(session().target, "")
        _sock.sendto(f"GYACK {label}".encode("ascii"), addr)
    except (OSError, AttributeError):
        pass


def connected():
    return _sock is not None and time.time() - _rx["last"] < 1.0


def status():
    if _sock is None:
        return 'OFF'
    return 'LIVE' if connected() else 'WAIT'


# ================================================================ orientation

_HOLD = {
    'PORTRAIT': Quaternion(),
    'LANDSCAPE_LEFT': Quaternion((0, 0, 1), math.radians(90)),
    'LANDSCAPE_RIGHT': Quaternion((0, 0, 1), math.radians(-90)),
    # screen up, top forward: camera looks along the phone's top edge, up = out of the screen
    'FLAT': Quaternion((1, 0, 0), math.radians(90)),
}
_FWD = Vector((0, 0, -1))
_UP = Vector((0, 1, 0))
_RIGHT = Vector((1, 0, 0))


def phone_rotation(p):
    """Phone orientation with camera axes (-Z forward, Y up), in a Z-up world."""
    return _rx["q"] @ _HOLD[p.hold]


def _yaw(q):
    f = q @ _FWD
    if abs(f.z) > 0.98:                 # looking straight up/down: use the up vector
        f = q @ _UP
    return math.atan2(f.y, f.x)


def find_view(context):
    best, size = None, 0
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D' and area.width * area.height > size:
                best, size = area, area.width * area.height
    return best


def _target_object(context, t):
    obj = context.scene.camera if t == 'CAMERA' else context.view_layer.objects.active
    if obj is None or obj.library is not None:
        return None
    return obj


def _active_bone(context):
    arm = context.view_layer.objects.active
    if arm is None or arm.type != 'ARMATURE' or arm.library is not None \
            or arm.data.bones.active is None:
        return None, None
    return arm, arm.pose.bones.get(arm.data.bones.active.name)


def target_key(context, t):
    """Identity of what we're driving: when it changes, Home starts over."""
    if t == 'VIEW':
        area = find_view(context)
        return ('VIEW', area.as_pointer()) if area else None
    if t == 'BONE':
        arm, pb = _active_bone(context)
        return ('BONE', arm.name_full, pb.name) if pb else None
    obj = _target_object(context, t)
    return (t, obj.name_full) if obj else None


def target_rotation(context, t):
    """Current world rotation of the target, or None if there's nothing to drive."""
    if t == 'VIEW':
        area = find_view(context)
        return area.spaces.active.region_3d.view_rotation.copy() if area else None
    if t == 'BONE':
        arm, pb = _active_bone(context)
        if pb is None:
            return None
        return arm.matrix_world.to_quaternion() @ pb.matrix.to_quaternion()
    obj = _target_object(context, t)
    return obj.matrix_world.to_quaternion() if obj else None


def recenter(context, p, t):
    """Point the target back to its Home rotation, from the phone's current pose."""
    if _rx["q"] is None:
        return
    if _ctl["home"] is None:            # first time: the target's rotation becomes Home
        _ctl["home"] = target_rotation(context, t)
    home = _ctl["home"]
    if home is None:
        return
    pq = phone_rotation(p)
    if p.keep_horizon and t in {'CAMERA', 'VIEW'}:
        _ctl["offset"] = Quaternion((0, 0, 1), _yaw(home) - _yaw(pq))
    else:
        _ctl["offset"] = home @ pq.inverted()
    if _ctl["smooth"] is None:
        _ctl["smooth"] = home
    _ctl["need_recenter"] = False


def raw_target(p, t):
    q = _ctl["offset"] @ phone_rotation(p)
    if p.lock_roll and t in {'CAMERA', 'VIEW'}:
        f = q @ _FWD
        if abs(f.z) < 0.999:
            q = f.to_track_quat('-Z', 'Y')
    return q


def smoothed(p, q, now):
    """Time-based smoothing: same feel whatever the update rate."""
    prev = _ctl["smooth"]
    dt = min(max(now - _ctl["t_apply"], 0.0), 0.1)
    _ctl["t_apply"] = now
    if prev is None or p.smoothing <= 0.0:
        _ctl["smooth"] = q
        return q
    if prev.dot(q) < 0:
        q = -q
    alpha = 1.0 - p.smoothing ** (dt * 60.0)       # smoothing = what stays after 1/60 s
    q = prev.slerp(q, alpha)
    _ctl["smooth"] = q
    return q


# ================================================================ applying to the target

def _set_rotation(owner, rot):
    mode = owner.rotation_mode
    if mode == 'QUATERNION':
        if owner.rotation_quaternion.dot(rot) < 0:
            rot = -rot
        owner.rotation_quaternion = rot
    elif mode == 'AXIS_ANGLE':
        axis, angle = rot.to_axis_angle()
        owner.rotation_axis_angle = (angle, *axis)
    else:
        owner.rotation_euler = rot.to_euler(mode, owner.rotation_euler)   # no flips


def _rotation_path(owner):
    return {'QUATERNION': "rotation_quaternion",
            'AXIS_ANGLE': "rotation_axis_angle"}.get(owner.rotation_mode, "rotation_euler")


def apply(context, t, q):
    if t == 'VIEW':
        area = find_view(context)
        if area:
            r3d = area.spaces.active.region_3d
            if r3d.view_perspective == 'CAMERA':
                r3d.view_perspective = 'PERSP'
            r3d.view_rotation = q
            area.tag_redraw()
        return
    if t == 'BONE':
        arm, pb = _active_bone(context)
        if pb is None:
            return
        local = arm.matrix_world.to_quaternion().inverted() @ q
        m = pb.matrix
        pb.matrix = Matrix.LocRotScale(m.to_translation(), local, m.to_scale())
        return
    obj = _target_object(context, t)
    if obj is None:
        return
    loc, _rot, scale = obj.matrix_world.decompose()
    world = Matrix.LocRotScale(loc, q, scale)
    if obj.parent:
        world = (obj.parent.matrix_world @ obj.matrix_parent_inverse).inverted() @ world
    _set_rotation(obj, world.to_quaternion())


def insert_key(context, t, frame):
    if t == 'BONE':
        arm, pb = _active_bone(context)
        if pb is not None:
            pb.keyframe_insert(_rotation_path(pb), frame=frame, group=pb.name)
    elif t in {'CAMERA', 'OBJECT'}:
        obj = _target_object(context, t)
        if obj is not None:
            obj.keyframe_insert(_rotation_path(obj), frame=frame)


def drive(context, now, force=False):
    """Recenter if needed, then move the target. Returns True if something moved."""
    p, t = prefs(), session(context).target
    key = target_key(context, t)
    if key is None:
        return False
    if key != _ctl["home_key"]:                     # new camera / object / bone
        _reset_tracking()
        _ctl["home_key"] = key
    if _ctl["need_recenter"] or _ctl["offset"] is None:
        recenter(context, p, t)
    if _ctl["offset"] is None:
        return False
    goal = raw_target(p, t)
    prev = _ctl["smooth"]
    # idle: phone still and smoothing settled -> don't touch the scene at all
    if not force and not _rx["new"] and prev is not None and abs(prev.dot(goal)) > 1 - 1e-9:
        _ctl["t_apply"] = now
        return False
    apply(context, t, smoothed(p, goal, now))
    return True


# ================================================================ loop

def _tick():
    if _sock is None:
        return None
    now = time.time()
    try:
        _read_packets()
        if now - _rx["t0"] >= 1.0:
            _rx["rate"], _rx["count"], _rx["t0"] = _rx["count"], 0, now
        if _rx["q"] is not None and now - _rx["last"] < 0.5:
            drive(bpy.context, now)
        elif _ctl["started_play"] and now - _rx["last"] > 1.0:
            _playback(False)                        # phone lost while recording
            _ctl["started_play"] = False
            _rx["rec"] = False
        _rx["new"] = False
        if now - _ctl["t_ui"] > 0.1:                # sidebar readout at 10 fps
            _ctl["t_ui"] = now
            _redraw_panels(bpy.context)
    except Exception as e:
        if now - _ctl["t_err"] > 2.0:              # don't flood the console
            _ctl["t_err"] = now
            print("Phone Gyro:", e)
    return 0.01


def _redraw_panels(context):
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                for r in area.regions:
                    if r.type == 'UI':
                        r.tag_redraw()


def recording(context):
    s = session(context)
    return (s.record or _rx["rec"]) and connected() and s.target != 'VIEW'


@bpy.app.handlers.persistent
def _on_frame(scene, depsgraph=None):
    context = bpy.context
    if not recording(context) or not context.screen or not context.screen.is_animation_playing:
        return
    # the frame change just evaluated old keys: put the phone's rotation back, then key it
    if _rx["q"] is not None:
        drive(context, time.time(), force=True)
    insert_key(context, session(context).target, scene.frame_current)


@bpy.app.handlers.persistent
def _on_load(*_args):
    _reset_tracking()
    _ctl["started_play"] = False


def _playback(start):
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return False
    win = wm.windows[0]
    area = next((a for a in win.screen.areas if a.type == 'VIEW_3D'), win.screen.areas[0])
    with bpy.context.temp_override(window=win, screen=win.screen, area=area):
        playing = win.screen.is_animation_playing
        if start and not playing:
            bpy.ops.screen.animation_play()
            return True
        if not start and playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
    return False


def _on_phone_record(rec):
    if not prefs().auto_play or session().target == 'VIEW':
        return
    if rec:
        _ctl["started_play"] = _playback(True)
    elif _ctl["started_play"]:
        _playback(False)
        _ctl["started_play"] = False


# ================================================================ operators

class PG_OT_listen(bpy.types.Operator):
    """Start or stop receiving the phone's rotation"""
    bl_idname = "phone_gyro.listen"
    bl_label = "Start / Stop"

    def execute(self, context):
        if is_listening():
            stop_listening()
            return {'FINISHED'}
        port = prefs().port
        try:
            start_listening(port)
        except OSError as e:
            self.report({'ERROR'}, f"Can't open port {port}: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


class PG_OT_recenter(bpy.types.Operator):
    """Point the target back to Home from the phone's current pose (phone: Volume Down)"""
    bl_idname = "phone_gyro.recenter"
    bl_label = "Recenter"

    def execute(self, context):
        _ctl["need_recenter"] = True
        return {'FINISHED'}


class PG_OT_set_home(bpy.types.Operator):
    """Use the target's current rotation as the new Home (where Recenter points to)"""
    bl_idname = "phone_gyro.set_home"
    bl_label = "Set Home"

    def execute(self, context):
        t = session(context).target
        _ctl["home"] = target_rotation(context, t)
        _ctl["home_key"] = target_key(context, t)
        _ctl["need_recenter"] = True
        return {'FINISHED'}


class PG_OT_copy_ip(bpy.types.Operator):
    """Copy this address to the clipboard"""
    bl_idname = "phone_gyro.copy_ip"
    bl_label = "Copy Address"

    ip: bpy.props.StringProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def execute(self, context):
        context.window_manager.clipboard = self.ip
        self.report({'INFO'}, f"Copied {self.ip}")
        return {'FINISHED'}


# ================================================================ UI

_STATUS_ICON = {'OFF': 'RADIOBUT_OFF', 'WAIT': 'SEQUENCE_COLOR_03', 'LIVE': 'SEQUENCE_COLOR_04'}


def _wrap(deg):
    return (deg + 180.0) % 360.0 - 180.0


def attitude():
    """Tilt, roll and heading (relative to Home) of what's being driven, in degrees."""
    q = _ctl["smooth"]
    if q is None:
        return None
    f, up, right = q @ _FWD, q @ _UP, q @ _RIGHT
    tilt = math.degrees(math.asin(max(-1.0, min(1.0, f.z))))
    roll = -math.degrees(math.atan2(right.z, up.z))
    home = _ctl["home"]
    heading = _wrap(math.degrees(_yaw(q) - _yaw(home))) if home is not None else 0.0
    return tilt, roll, heading


class PG_PT_main(bpy.types.Panel):
    bl_label = "Phone Gyro"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone Gyro"

    def draw_header(self, context):
        self.layout.label(text="", icon=_STATUS_ICON[status()])

    def draw(self, context):
        p = prefs()
        layout = self.layout
        st = status()

        row = layout.row()
        row.scale_y = 1.6
        row.operator(PG_OT_listen.bl_idname,
                     text="Stop" if st != 'OFF' else "Start Listening",
                     icon='PAUSE' if st != 'OFF' else 'PLAY', depress=st != 'OFF')

        box = layout.box()
        col = box.column(align=True)
        if st == 'LIVE':
            col.label(text=f"Live  ·  {_rx['rate']} Hz", icon='CHECKMARK')
            col.label(text=f"Phone {_rx['addr']}", icon='BLANK1')
        elif st == 'WAIT':
            col.label(text="Waiting for the phone…", icon='TIME')
            if time.time() - _rx["since"] > 6 and _rx["last"] == 0.0:
                c = col.column(align=True)
                c.active = False
                c.label(text="Nothing yet? Check the address, the Wi-Fi", icon='INFO')
                c.label(text=f"and the firewall (UDP {p.port}).", icon='BLANK1')
        else:
            col.label(text="Not listening", icon='RADIOBUT_OFF')

        col = layout.column(align=True)
        col.label(text="Enter in the app:")
        for i, ip in enumerate(local_ips()):
            row = col.row(align=True)
            row.label(text=ip, icon='URL' if i == 0 else 'BLANK1')
            row.operator(PG_OT_copy_ip.bl_idname, text="", icon='COPYDOWN').ip = ip
        row = col.row(align=True)
        row.enabled = st == 'OFF'
        row.prop(p, "port")


class PG_PT_control(bpy.types.Panel):
    bl_label = "Control"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone Gyro"
    bl_parent_id = "PG_PT_main"

    def draw_header(self, context):
        self.layout.label(text="", icon='ORIENTATION_GIMBAL')

    def draw(self, context):
        p, s = prefs(), session(context)
        layout = self.layout

        row = layout.row(align=True)
        row.scale_y = 1.3
        row.prop(s, "target", expand=True)

        if s.target == 'CAMERA' and context.scene.camera is None:
            layout.label(text="The scene has no camera", icon='ERROR')
        elif s.target == 'BONE' and _active_bone(context)[1] is None:
            layout.label(text="Select a bone in Pose Mode", icon='ERROR')
        elif s.target == 'OBJECT' and _target_object(context, 'OBJECT') is None:
            layout.label(text="No active object", icon='ERROR')

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(p, "hold", text="Held")
        col.prop(p, "smoothing", slider=True)
        if s.target in {'CAMERA', 'VIEW'}:
            col.prop(p, "keep_horizon")
            col.prop(p, "lock_roll")

        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator(PG_OT_recenter.bl_idname, icon='PIVOT_CURSOR')
        row.operator(PG_OT_set_home.bl_idname, icon='HOME')

        att = attitude() if status() == 'LIVE' else None
        box = layout.box()
        grid = box.grid_flow(row_major=True, columns=3, even_columns=True, align=True)
        for name, i in (("Tilt", 0), ("Roll", 1), ("Heading", 2)):
            c = grid.column(align=True)
            c.alignment = 'CENTER'
            c.label(text=f"{att[i]:+.1f}°" if att else "—")
            sub = c.row()
            sub.active = False
            sub.label(text=name)


class PG_PT_record(bpy.types.Panel):
    bl_label = "Recording"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone Gyro"
    bl_parent_id = "PG_PT_main"

    @classmethod
    def poll(cls, context):
        return session(context).target != 'VIEW'

    def draw_header(self, context):
        self.layout.label(text="", icon='REC')

    def draw(self, context):
        p, s = prefs(), session(context)
        layout = self.layout
        rec = recording(context)

        row = layout.row()
        row.scale_y = 1.6
        row.alert = rec
        row.prop(s, "record", toggle=True, icon='REC',
                 text="Recording…" if rec else "Record")
        layout.prop(p, "auto_play")

        col = layout.column(align=True)
        col.active = False
        if rec and context.screen and context.screen.is_animation_playing:
            col.label(text=f"Keying frame {context.scene.frame_current}", icon='KEYFRAME_HLT')
        elif rec:
            col.label(text="Armed: keys are added while playing", icon='KEYFRAME')
        else:
            col.label(text="Phone: Volume Up = Record", icon='INFO')


# ================================================================ register

classes = (PG_Preferences, PG_Session, PG_OT_listen, PG_OT_recenter, PG_OT_set_home,
           PG_OT_copy_ip, PG_PT_main, PG_PT_control, PG_PT_record)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.phone_gyro = bpy.props.PointerProperty(type=PG_Session)
    bpy.app.handlers.frame_change_post.append(_on_frame)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    stop_listening()
    for lst, fn in ((bpy.app.handlers.frame_change_post, _on_frame),
                    (bpy.app.handlers.load_post, _on_load)):
        if fn in lst:
            lst.remove(fn)
    del bpy.types.WindowManager.phone_gyro
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
