# SPDX-License-Identifier: GPL-3.0-or-later
"""DualSense Controller.

Use a DualSense (PS5) controller, over USB-C or Bluetooth, to fly the camera and
the viewport, and to move objects and bones (Pose Mode) smoothly.
Choose the target with the buttons in the DualSense panel (N key), or cycle it
with the Options button on the controller.

CAMERA mode
-----------
Left stick     : fly, forward = where the camera points, X = strafe
D-pad up/down  : height (global Z).  D-pad left/right: previous/next keyframe
Right stick    : look around (hold R3: X = roll)
L3 (click)     : orbit around the pivot on/off, in every mode (the pivot is the
                 object of the active slot in the Pivot panel, otherwise the 3D cursor)
L1 + triggers  : zoom (R2 = zoom in, L2 = zoom out)
R1 + triggers  : focus distance (R2 = farther, L2 = closer)
L1 + R1 + trig.: f-stop (R2 = higher, L2 = lower)
Keyframes      : Triangle = rotation, Square = location, then
                 - Triangle: X Y Z
                 - two axis buttons (Square = X, Cross = Y, Circle = Z):
                   same twice = one axis, two different = a pair (XY, YZ, XZ)
Cross alone    : play / stop; double tap = playhead back to the start
Cross held     : gyroscope on / off (rotates the camera around itself)
                 (calibrate once with the panel button; the offset is saved)
Circle alone   : recording on / off (keyframes while you move)
Touchpad       : swipe a finger to rotate around itself (like the gyroscope)
Touchpad click : cycle the pivot (slots, then 3D cursor)
Create button  : pause / resume.  Options button: change mode.

VIEWPORT mode: same as the camera (fly, orbit, gyroscope, touchpad, Cross).
OBJECT mode: same buttons as the camera, applied to the selected objects (or the
active bone, Pose Mode included). Left stick moves (R1 held: Y = depth), right
stick rotates (R3 held: roll and scale), L2 = slow, R2 = fast. With the orbit on
(L3 click) or L1 held, rotation and scale happen around the pivot instead of the
object's own origin.
"""

import atexit
import importlib
import importlib.util
import math
import os
import re
import subprocess
import sys
import time
import traceback
import zlib
from types import SimpleNamespace

import blf
import bpy
import gpu
import threading
from gpu_extras.batch import batch_for_shader
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty)
from mathutils import Matrix, Quaternion, Vector

# pygame and hidapi are installed here (once) from the panel
LIBDIR = bpy.utils.user_resource('CONFIG', path="dualsense_libs", create=True)

VERSION = "1.4.3"
FRAME = 1.0 / 60.0
DOUBLE_TAP = 0.35     # max seconds between two Cross taps to rewind to the start
HOLD_X = 0.6          # seconds to hold Cross to toggle the gyroscope
SEQ_TIMEOUT = 3.0     # max seconds between two keys of a keyframe sequence

# SDL GameController axes and buttons
AX_LX, AX_LY, AX_RX, AX_RY, AX_L2, AX_R2 = 0, 1, 2, 3, 4, 5
BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE = 0, 1, 2, 3
BTN_L3, BTN_R3, BTN_L1, BTN_R1 = 7, 8, 9, 10
BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT = 11, 12, 13, 14
BTN_TOUCHPAD = 15                     # touchpad click
BTN_CREATE, BTN_OPTIONS = 4, 6        # "Create" (left) and "Options" (right) buttons
READ_BUTTONS = (BTN_CROSS, BTN_CIRCLE, BTN_SQUARE, BTN_TRIANGLE,
                BTN_L3, BTN_R3, BTN_L1, BTN_R1,
                BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_CREATE, BTN_OPTIONS,
                BTN_TOUCHPAD)

AXIS_OF = {BTN_SQUARE: 0, BTN_CROSS: 1, BTN_CIRCLE: 2}   # X, Y, Z

TARGETS = (
    ('SELECTION', "Object", "Selected objects, or the active bone in Pose Mode"),
    ('CAMERA', "Camera", "The scene's active camera"),
    ('VIEWPORT', "Viewport", "3D viewport"),
    ('CUSTOM', "Custom", "L2 + R2 drive the parameter you assigned (right-click any number); "
                         "sticks fly the viewport"),
)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class _State:
    running = False
    pygame = None
    sdlc = None
    ctrl = None
    name = ""
    target = 'CAMERA'
    last_t = 0.0
    last_scan = 0.0
    prev_btn = {}
    moving = False
    dirty = False
    error = ""
    error_where = ""
    msg_err = ""
    msg = ""
    msg_t = 0.0
    # bumpers, keyframe sequence, recording
    bumper = None
    seq_kind = None
    seq_args = []
    seq_t = 0.0
    x_down_t = None
    x_fired = False
    x_last_tap = -10.0
    rec = False
    orbit = False
    paused = False
    # gyroscope
    gyro_on = False
    hid = None
    gyro_k = (0.061, 0.061, 0.061)
    gyro_bias = (0.0, 0.0, 0.0)
    gyro_cal = False
    gyro_win = []                 # last raw gyro samples (still detection)
    gyro_best = None              # (peak-to-peak, mean) of the stillest window while calibrating
    gyro_t0 = 0.0
    gyro_seen = 0.0
    gyro_warned = False
    hid_try_t = -10.0
    touch_prev = None
    pad_click = False
    click_prev = False
    hud_handle = None
    hid_bt = None                 # True = Bluetooth reports, False = USB, None = not known yet
    trig_sent = None              # (left, right) trigger effects last written to the controller
    trig_t = 0.0
    trig_seq = 0
    trig_fail = 0
    trig_info = ""                # status shown in the panel
    trig_test_until = 0.0
    trig_q = (0, 0)               # L2 / R2 quantized (5%) to redraw the bars only when they change
    l2 = 0.0
    r2 = 0.0
    map_last_t = 0.0              # Custom mode: last trigger value written (Absolute)
    map_frac = 0.0                # Custom mode: leftover fraction for whole-number fields
    map_err = ""
    installing = False            # pip runs in a background thread
    install_result = ""


S = _State()


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _say(text):
    S.msg = text
    S.msg_t = time.perf_counter()
    _redraw_ui()


# --------------------------------------------------------------------------
# pygame
# --------------------------------------------------------------------------

def _libs_on_path():
    if LIBDIR not in sys.path:
        sys.path.insert(0, LIBDIR)


_AVAILABLE = set()     # modules already found (a missing one is looked up again)


def _module_available(name):
    if name in _AVAILABLE:
        return True
    _libs_on_path()
    if importlib.util.find_spec(name) is None:
        return False
    _AVAILABLE.add(name)
    return True


def _pygame_available():
    return _module_available("pygame")


def _hid_available():
    return _module_available("hid")


def _import_pygame():
    _libs_on_path()
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    import pygame
    from pygame._sdl2 import controller as sdlc
    return pygame, sdlc


def _open_controller():
    sdlc, pg = S.sdlc, S.pygame
    count = sdlc.get_count() if hasattr(sdlc, "get_count") else pg.joystick.get_count()
    for idx in range(count):
        try:
            if hasattr(sdlc, "is_controller") and not sdlc.is_controller(idx):
                continue
            c = sdlc.Controller(idx)
        except Exception:
            continue
        name = ""
        for getter in (lambda: sdlc.name_forindex(idx),
                       lambda: c.name,
                       lambda: pg.joystick.Joystick(idx).get_name()):
            try:
                name = getter()
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "ignore")
                if name:
                    break
            except Exception:
                pass
        S.ctrl = c
        S.name = name or "Controller"
        _redraw_ui()
        return


def _close_controller():
    if S.ctrl is not None:
        try:
            S.ctrl.quit()
        except Exception:
            pass
    S.ctrl = None
    S.name = ""


def _rumble(low, high, ms):
    if S.ctrl is None or not _prefs().rumble:
        return
    try:
        S.ctrl.rumble(low, high, ms)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Gyroscope and touchpad (direct HID reading of the DualSense)
# --------------------------------------------------------------------------

def _s16(lo, hi):
    v = lo | (hi << 8)
    return v - 65536 if v >= 32768 else v


def _gyro_open():
    import hid
    dev = None
    for pid in (0x0CE6, 0x0DF2):          # DualSense, DualSense Edge
        d = hid.device()
        try:
            d.open(0x054C, pid)
            dev = d
            break
        except Exception:
            try:
                d.close()
            except Exception:
                pass
    if dev is None:
        raise RuntimeError("DualSense not found over HID")
    dev.set_nonblocking(1)

    # scale from the calibration data (degrees/second per raw unit)
    k = [0.061, 0.061, 0.061]
    try:
        rep = dev.get_feature_report(0x05, 41)    # on Bluetooth this also enables the full reports
        if len(rep) >= 23:
            speed2x = _s16(rep[19], rep[20]) + _s16(rep[21], rep[22])
            pairs = ((7, 9), (11, 13), (15, 17))  # pitch, yaw, roll: (+, -)
            for n, (p, m) in enumerate(pairs):
                den = _s16(rep[p], rep[p + 1]) - _s16(rep[m], rep[m + 1])
                if den:
                    v = speed2x / den
                    if 0.03 < v < 0.12:
                        k[n] = v
    except Exception:
        pass
    return dev, tuple(k)


def _hid_close():
    if S.hid is not None:
        try:
            S.hid.close()
        except Exception:
            pass
    S.hid = None
    S.hid_bt = None
    S.trig_sent = None
    S.trig_fail = 0
    S.touch_prev = None
    S.pad_click = False


def _gyro_close():
    """Only turns the gyroscope off (the HID device stays open for the touchpad)."""
    S.gyro_on = False
    S.gyro_cal = False


def _hid_ensure(now):
    """Opens the HID device when needed (gyroscope or touchpad)."""
    if S.hid is not None:
        return True
    if now - S.hid_try_t < 2.0:           # do not retry (or re-scan) every tick
        return False
    S.hid_try_t = now
    if not _hid_available():
        return False
    try:
        S.hid, S.gyro_k = _gyro_open()
    except Exception:
        S.hid = None
        return False
    return True


def _gyro_set(on):
    if not on:
        _gyro_close()
        _say("Gyroscope off")
        _rumble(0.0, 0.3, 60)
        return
    if not _hid_available():
        _say("hidapi is missing: use the Install button")
        return
    now = time.perf_counter()
    S.hid_try_t = -10.0                   # try to open right away
    if not _hid_ensure(now):
        _say("Gyroscope: DualSense not found over HID")
        return
    S.gyro_on = True
    S.gyro_seen = 0.0
    S.gyro_warned = False
    S.gyro_t0 = now
    prefs = _prefs()
    if prefs.gyro_calibrated:             # calibrated once: reuse the saved offset
        S.gyro_bias = tuple(prefs.gyro_offset)
        S.gyro_cal = False
        S.gyro_win = []
        _say("Gyroscope on")
        _rumble(0.0, 0.5, 80)
    else:                                 # first use: calibrate automatically
        _gyro_calibrate()


def _gyro_calibrate():
    """Measures the gyroscope offset while the controller is still; saved in the preferences."""
    if not _hid_available():
        _say("hidapi is missing: use the Install button")
        return
    now = time.perf_counter()
    S.hid_try_t = -10.0
    if not _hid_ensure(now):
        _say("Gyroscope: DualSense not found over HID")
        return
    S.gyro_cal = True
    S.gyro_win = []
    S.gyro_best = None
    S.gyro_t0 = now
    S.gyro_seen = 0.0
    S.gyro_warned = False
    _say("Calibrating: put the controller down or keep it still...")
    _rumble(0.3, 0.0, 60)


def _hid_poll():
    """Reads the controller reports. Returns (raw gyro average or None, dx, dy)
    where dx, dy are the finger movements on the touchpad (pixels)."""
    d = S.hid
    acc = [0.0, 0.0, 0.0]
    n = 0
    dx = dy = 0.0
    for _ in range(64):
        data = d.read(96)
        if not data:
            break
        if data[0] == 0x01 and len(data) >= 28:      # USB
            off, toff, boff = 16, 33, 10
            S.hid_bt = False
        elif data[0] == 0x31 and len(data) >= 29:    # Bluetooth
            off, toff, boff = 17, 34, 11
            S.hid_bt = True
        else:
            continue
        for a in range(3):
            acc[a] += _s16(data[off + 2 * a], data[off + 2 * a + 1])
        n += 1
        if len(data) > boff:
            S.pad_click = bool(data[boff] & 0x02)    # touchpad click (also read over HID)
        if len(data) >= toff + 4:                    # first finger on the touchpad
            c = data[toff]
            if c & 0x80:                             # bit 7 set = not touching
                S.touch_prev = None
            else:
                x = ((data[toff + 2] & 0x0F) << 8) | data[toff + 1]
                y = (data[toff + 3] << 4) | (data[toff + 2] >> 4)
                tid = c & 0x7F
                if S.touch_prev is not None and S.touch_prev[0] == tid:
                    ddx = x - S.touch_prev[1]
                    ddy = y - S.touch_prev[2]
                    if abs(ddx) < TOUCH_JUMP_X and abs(ddy) < TOUCH_JUMP_Y:   # ignore glitches
                        dx += ddx
                        dy += ddy
                S.touch_prev = (tid, x, y)
    return ([v / n for v in acc] if n else None), dx, dy


def _hid_step(now, dt, prefs, use=True):
    """Gyroscope + touchpad: returns (pitch, yaw, roll) in radians."""
    g = (0.0, 0.0, 0.0)
    if not (S.gyro_on or S.gyro_cal or prefs.touchpad) or not _hid_ensure(now):
        return g
    try:
        raw, dx, dy = _hid_poll()
    except Exception:                     # controller unplugged while reading
        traceback.print_exc()
        _hid_close()
        _gyro_close()
        _say("Gyroscope/touchpad lost: turn the gyroscope back on by holding Cross")
        return g
    if not use:
        if S.gyro_cal:
            _gyro_step(now, dt, prefs, raw)          # let a running calibration finish
        return g
    if S.gyro_on or S.gyro_cal:
        gg = _gyro_step(now, dt, prefs, raw)
        if gg is not None:
            g = gg
    if prefs.touchpad and (dx or dy):
        # like the right stick: camera/viewport "look", objects follow the finger
        sgn = 1.0 if S.target == 'SELECTION' else -1.0
        k = math.radians(prefs.touch_sens)
        g = (g[0] + sgn * dy * k, g[1] + sgn * dx * k, g[2])
    return g


# --------------------------------------------------------------------------
# Custom mode: the trigger pair drives an assigned Blender parameter
# --------------------------------------------------------------------------

_TOK = re.compile(r'\.([A-Za-z]\w*)|\[(-?\d+)\]|\[(["\'])((?:\\.|(?!\3).)*)\3\]')


def _walk(path):
    """Safe resolver for 'bpy.data.xxx["Name"].attr[0]' paths (no eval, no calls)."""
    if not path.startswith("bpy.data"):
        return None
    obj, rest, pos = bpy.data, path[len("bpy.data"):], 0
    try:
        while pos < len(rest):
            m = _TOK.match(rest, pos)
            if m is None:
                return None
            if m.group(1):
                obj = getattr(obj, m.group(1))
            elif m.group(2) is not None:
                obj = obj[int(m.group(2))]
            else:
                key = m.group(4).replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
                obj = obj[key]
            pos = m.end()
    except Exception:
        return None
    return obj


def _map_resolve(m):
    """(owner, prop, index, is_int, rna) for a mapping, or None if it no longer exists."""
    owner = _walk(m.path)
    if owner is None or not hasattr(owner, "bl_rna"):
        return None
    rna = owner.bl_rna.properties.get(m.prop)
    if rna is None or rna.type not in {'FLOAT', 'INT'}:
        return None
    return owner, m.prop, m.index, rna.type == 'INT', rna


def _map_get(res):
    owner, prop, idx, _i, _r = res
    v = getattr(owner, prop)
    return float(v[idx] if idx >= 0 else v)


def _map_set(res, v):
    owner, prop, idx, is_int, _r = res
    v = int(round(v)) if is_int else v
    if idx >= 0:
        arr = getattr(owner, prop)
        arr[idx] = v
    else:
        setattr(owner, prop, v)


def _active_map():
    sc = bpy.context.scene
    i = sc.dualsense_map_index
    return sc.dualsense_maps[i] if 0 <= i < len(sc.dualsense_maps) else None


def _map_value_text(m):
    res = _map_resolve(m)
    if res is None:
        return None
    v = _map_get(res)
    return str(int(v)) if res[3] else "%.3g" % v


def _custom_step(dt, l2, r2):
    """Applies the triggers to the active mapping. Returns True if the value changed."""
    m = _active_map()
    if m is None:
        return False
    t = r2 - l2
    if abs(t) < 0.02:
        t = 0.0
    if m.invert:
        t = -t
    res = _map_resolve(m)
    if res is None:
        if S.map_err != m.name:
            S.map_err = m.name
            _say("'%s' no longer exists: remove or re-assign it" % m.name)
        return False
    S.map_err = ""
    lo, hi = min(m.vmin, m.vmax), max(m.vmin, m.vmax)
    if m.mode == 'ABS':
        if t == 0.0 and S.map_last_t == 0.0:
            return False                       # released and back at rest: hands off
        rest = min(max(m.rest, lo), hi)
        v = rest + t * ((hi - rest) if t > 0 else (rest - lo))
        S.map_last_t = t
    else:
        if t == 0.0:
            S.map_frac = 0.0
            return False
        step = t * m.speed * dt
        if res[3]:                             # whole numbers: add up the small steps
            S.map_frac += step
            step = int(S.map_frac)
            if not step:
                return False
            S.map_frac -= step
        v = _map_get(res) + step
    v = min(max(v, lo), hi)
    _map_set(res, v)
    if S.rec and m.record:
        try:
            res[0].keyframe_insert(res[1], index=res[2], frame=bpy.context.scene.frame_current)
        except Exception:
            pass
    return True


def _map_key_now():
    m = _active_map()
    res = _map_resolve(m) if m else None
    if res is None:
        _say("No parameter to key")
        return
    try:
        res[0].keyframe_insert(res[1], index=res[2], frame=bpy.context.scene.frame_current)
        _say("Keyframe %s (frame %d)" % (m.name, bpy.context.scene.frame_current))
        _rumble(0.3, 0.3, 70)
    except Exception as e:
        _say("Keyframe failed: %s" % e)


def _map_cycle(direction):
    sc = bpy.context.scene
    n = len(sc.dualsense_maps)
    if not n:
        _say("No parameter assigned: right-click any number > Assign to DualSense Triggers")
        return
    sc.dualsense_map_index = (sc.dualsense_map_index + direction) % n
    S.map_last_t = 0.0
    S.map_frac = 0.0
    m = _active_map()
    _say("Parameter: %s" % m.name)
    _rumble(0.0, 0.3, 50)
    _redraw_ui()


def _custom_buttons(btn, now):
    prev = S.prev_btn
    _x_button(btn, now)                       # Cross: play/stop, held = gyroscope
    if btn[BTN_CIRCLE] and not prev.get(BTN_CIRCLE, False):
        _toggle_rec()
    if btn[BTN_TRIANGLE] and not prev.get(BTN_TRIANGLE, False):
        _map_key_now()
    for b, d in ((BTN_LEFT, -1), (BTN_RIGHT, 1)):
        if btn[b] and not prev.get(b, False):
            _map_cycle(d)


# --------------------------------------------------------------------------
# Adaptive triggers (HID output report: resistance felt on L2 / R2)
# --------------------------------------------------------------------------

TRIG_OFF = (0x05,) + (0,) * 10          # neutral trigger
TRIG_IDLE = (TRIG_OFF, TRIG_OFF)        # (left, right)
TRIG_MIN_DT = 0.05                      # seconds between two different writes
TRIG_REFRESH = 1.0                      # seconds: resend active effects (SDL rumble may reset them)
SOFT_ZONE = 0.15                        # last part of a range (log scale) where the trigger stiffens


def _trig_feedback(strengths):
    """Feedback effect (0x21): resistance per zone along the trigger travel.
    strengths: 10 values 0..8 (0 = free)."""
    active = force = 0
    for z, st in enumerate(strengths[:10]):
        st = int(max(0, min(8, st)))
        if st:
            active |= 1 << z
            force |= ((st - 1) & 7) << (3 * z)
    if not active:
        return TRIG_OFF
    return ((0x21, active & 0xFF, (active >> 8) & 0xFF)
            + tuple((force >> (8 * k)) & 0xFF for k in range(4)) + (0, 0, 0, 0))


def _trig_rigid(force, start=0):
    """Continuous resistance (0x01): an almost immovable trigger. force 0..255."""
    force = int(max(0, min(255, force)))
    return (0x01, int(start), force) + (0,) * 8 if force else TRIG_OFF


def _trig_ramp(lo, hi):
    """Resistance growing along the travel from lo to hi (strengths 1..8)."""
    return _trig_feedback([round(lo + (hi - lo) * z / 9.0) for z in range(10)])


def _range_pos(value, lo, hi):
    """Position 0..1 of value inside [lo, hi] on a log scale."""
    value = min(max(value, lo), hi)
    return (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))


def _limit_pos(cam):
    """While L1 / R1 + triggers drive a camera value: its position in its range (None = n/a)."""
    if S.bumper == 'L':
        return _range_pos(cam.data.lens, 8.0, 300.0)
    if S.bumper == 'B':
        return _range_pos(cam.data.dof.aperture_fstop, 0.1, 128.0)
    if S.bumper == 'R' and cam.data.dof.focus_object is None:
        return _range_pos(cam.data.dof.focus_distance, 0.01, 10000.0)
    return None


def _trigger_wanted(prefs):
    """(left, right) effects for the current state."""
    if time.perf_counter() < S.trig_test_until:          # "Test triggers" button
        full = _trig_rigid(255)                          # as hard as the motor can go
        return full, full
    k = prefs.trigger_strength
    if S.paused or not prefs.adaptive_triggers or k < 0.05:
        return TRIG_IDLE
    if S.target == 'CAMERA' and S.bumper:
        # zoom / focus / f-stop: the trigger gets stiff when the value reaches its limit
        cam = bpy.context.scene.camera
        t = _limit_pos(cam) if (cam is not None and cam.type == 'CAMERA') else None
        if t is None:
            return TRIG_IDLE
        wall = 255 * (0.35 + 0.65 * k)                           # k = 1: fully rigid at the limit
        up = max(0.0, (t - (1.0 - SOFT_ZONE)) / SOFT_ZONE)       # R2 raises the value
        down = max(0.0, (SOFT_ZONE - t) / SOFT_ZONE)             # L2 lowers it
        step = lambda v: round(min(1.0, v) * wall / 16.0) * 16     # fewer, coarser updates
        return (_trig_rigid(step(down)) if down > 0.05 else TRIG_OFF,
                _trig_rigid(step(up)) if up > 0.05 else TRIG_OFF)
    if S.target == 'CUSTOM':
        # Custom mode: both triggers always at maximum stiffness, for fine control
        m = _active_map()
        if m is None or _map_resolve(m) is None:
            return TRIG_IDLE                             # nothing assigned: leave them free
        full = _trig_rigid(255)
        return full, full
    # triggers are speed modifiers: R2 (fast) pushes back more the further it goes, L2 (slow) is soft
    return (_trig_ramp(1 + k, 1 + 4 * k), _trig_ramp(1 + 2 * k, 1 + 7 * k))


def _trig_write(left, right):
    """Writes the trigger effects. Returns False when the write failed."""
    d = S.hid
    if d is None or S.hid_bt is None:
        return False
    if S.hid_bt:                          # Bluetooth: report 0x31 + CRC32
        S.trig_seq = (S.trig_seq + 1) & 0x0F
        rep = bytearray(78)
        rep[0], rep[1], rep[2] = 0x31, S.trig_seq << 4, 0x10
        base = 3
    else:                                 # USB: report 0x02
        rep = bytearray(48)
        rep[0] = 0x02
        base = 1
    rep[base] = 0x0C                      # valid flags: right + left trigger effect
    rep[base + 1] = 0x40                  # valid flags 1: motor / trigger power scaling ...
    rep[base + 36] = 0x00                 # ... set to full power
    rep[base + 10:base + 21] = bytes(right)
    rep[base + 21:base + 32] = bytes(left)
    if S.hid_bt:
        rep[74:78] = zlib.crc32(b"\xa2" + bytes(rep[:74])).to_bytes(4, "little")
    return d.write(bytes(rep)) >= 0


def _trigger_step(now, prefs):
    """Keeps the controller triggers in line with what they do right now."""
    try:
        want = _trigger_wanted(prefs)
        sent = S.trig_sent or TRIG_IDLE
        if want == sent:
            if want == TRIG_IDLE or now - S.trig_t < TRIG_REFRESH:
                return
        elif now - S.trig_t < TRIG_MIN_DT:
            return
        if S.trig_fail >= 3:
            return
        if not _hid_available():
            S.trig_info = "hidapi is missing"
            return
        if not _hid_ensure(now):
            S.trig_info = "DualSense not found over HID"
            return
        if S.hid_bt is None:              # learn the transport from one input report
            data = S.hid.read(96)
            if data and data[0] in (0x01, 0x31):
                S.hid_bt = data[0] == 0x31
            else:
                S.trig_info = "waiting for controller data"
                return
        S.trig_t = now
        err = ""
        try:
            ok = _trig_write(*want)
        except Exception as e:
            ok, err = False, str(e)
        if ok:
            S.trig_sent = want
            S.trig_fail = 0
            S.trig_info = "OK (%s)" % ("Bluetooth" if S.hid_bt else "USB")
        else:
            S.trig_fail += 1
            S.trig_info = "write failed: %s" % (err or "the controller refused it")
            if S.trig_fail >= 3:
                _say("Adaptive triggers: %s" % S.trig_info)
        _redraw_ui()
    except Exception as e:
        S.trig_fail += 1
        S.trig_info = "error: %s" % e
        traceback.print_exc()


def _trigger_atexit():
    try:
        if S.hid is not None and S.trig_sent and S.trig_sent != TRIG_IDLE:
            _trig_write(*TRIG_IDLE)
    except Exception:
        pass


atexit.register(_trigger_atexit)


def _trigger_reset():
    """Back to neutral triggers (stop / pause)."""
    try:
        if S.trig_sent and S.trig_sent != TRIG_IDLE and S.hid is not None:
            _trig_write(*TRIG_IDLE)
    except Exception:
        pass
    S.trig_sent = None


GYRO_WIN = 30              # samples used to decide that the controller is still
GYRO_STILL_CAL = 40.0      # max peak-to-peak (raw units) to accept a calibration window
GYRO_STILL_RUN = 20.0      # max peak-to-peak to refresh the offset while running
GYRO_CAL_TIMEOUT = 6.0     # seconds before settling for the stillest window seen
TOUCH_JUMP_X = 400         # touchpad jumps bigger than this (pixels) per report are glitches
TOUCH_JUMP_Y = 300


def _gyro_window(raw):
    """Adds a raw sample; returns (peak-to-peak, mean) of the last GYRO_WIN samples."""
    w = S.gyro_win
    w.append(tuple(raw))
    if len(w) > GYRO_WIN:
        del w[0]
    if len(w) < GYRO_WIN:
        return None
    p2p = max(max(v[a] for v in w) - min(v[a] for v in w) for a in range(3))
    return p2p, tuple(sum(v[a] for v in w) / len(w) for a in range(3))


def _gyro_calibrated(bias, still):
    S.gyro_bias = bias
    S.gyro_cal = False
    try:
        prefs = _prefs()
        prefs.gyro_offset = bias
        prefs.gyro_calibrated = True
    except Exception:
        traceback.print_exc()
    what = "Gyroscope calibrated" if not S.gyro_on else "Gyroscope on"
    _say(what if still else what + " (it was not still: calibrate again on a table)")
    _rumble(0.0, 0.5, 80)


def _gyro_step(now, dt, prefs, raw):
    """Returns (pitch, yaw, roll) in radians for this tick, or None."""
    if raw is not None:
        S.gyro_seen = now
    elif S.gyro_seen == 0.0 and not S.gyro_warned and now - S.gyro_t0 > 2.0:
        S.gyro_warned = True
        _say("No gyroscope data: try the USB cable")

    if raw is not None:
        win = _gyro_window(raw)
    else:
        win = None

    if S.gyro_cal:                      # wait until the controller is really still
        if win is not None:
            if S.gyro_best is None or win[0] < S.gyro_best[0]:
                S.gyro_best = win
            if win[0] < GYRO_STILL_CAL:
                _gyro_calibrated(win[1], True)
            elif now - S.gyro_t0 > GYRO_CAL_TIMEOUT:
                _gyro_calibrated(S.gyro_best[1], False)
        return None
    if raw is None:
        return None
    if win is not None and win[0] < GYRO_STILL_RUN:
        S.gyro_bias = win[1]            # controller at rest: follow the drift

    inv = (prefs.gyro_invert_x, prefs.gyro_invert_y, prefs.gyro_invert_z)
    out = []
    for a in range(3):
        v = (raw[a] - S.gyro_bias[a]) * S.gyro_k[a]       # degrees/second
        if abs(v) < prefs.gyro_deadzone:
            v = 0.0
        if inv[a]:
            v = -v
        out.append(math.radians(v) * dt * prefs.gyro_sens)
    return tuple(out)


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

def _stick(x, y, prefs):
    """Radial deadzone + response curve. Input and output in [-1, 1]."""
    mag = math.hypot(x, y)
    dz = prefs.deadzone
    if mag <= dz:
        return 0.0, 0.0
    m = min(mag, 1.0)
    s = ((m - dz) / (1.0 - dz)) ** prefs.expo
    return x / mag * s, y / mag * s


def _read_inputs(c, prefs):
    def ax(a):
        v = c.get_axis(a)
        if not isinstance(v, float):      # raw SDL values: -32768..32767
            v = v / 32767.0
        return max(-1.0, min(1.0, v))

    lx, ly = _stick(ax(AX_LX), -ax(AX_LY), prefs)
    rx, ry = _stick(ax(AX_RX), -ax(AX_RY), prefs)
    if prefs.invert_lx:
        lx = -lx
    if prefs.invert_ly:
        ly = -ly
    if prefs.invert_rx:
        rx = -rx
    if prefs.invert_ry:
        ry = -ry
    l2 = max(0.0, ax(AX_L2))
    r2 = max(0.0, ax(AX_R2))
    btn = {b: bool(c.get_button(b)) for b in READ_BUTTONS}
    return lx, ly, rx, ry, l2, r2, btn


# --------------------------------------------------------------------------
# 3D views
# --------------------------------------------------------------------------

def _find_view3d():
    """Returns (area, space, region_3d) of the largest 3D view."""
    best, best_size = None, 0
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            size = area.width * area.height
            if size > best_size:
                space = area.spaces.active
                rv3d = getattr(space, "region_3d", None)
                if rv3d is not None:
                    best, best_size = (area, space, rv3d), size
    return best


def _redraw_ui():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _vr_is_view_to_world(rv3d, m):
    """Detects the view_rotation convention (view->world or the inverse)."""
    a = rv3d.view_rotation.to_matrix()
    d1 = sum(abs(a[i][j] - m[i][j]) for i in range(3) for j in range(3))
    d2 = sum(abs(a[i][j] - m[j][i]) for i in range(3) for j in range(3))
    return d1 <= d2


# --------------------------------------------------------------------------
# Movement
# --------------------------------------------------------------------------

def _apply_world(mat, d_loc, d_rot, s, pivot=None, g_rot=None):
    loc, rot, sca = mat.decompose()
    if pivot is not None:         # rotation (and scale) around a world-space point
        loc = pivot + d_rot @ ((loc - pivot) * s)
    new_rot = d_rot @ rot
    if g_rot is not None:         # gyroscope: always around the object's own origin
        new_rot = g_rot @ new_rot
    return Matrix.LocRotScale(loc + d_loc, new_rot, sca * s)


def _pivot_point():
    """Origin of the active slot's object, otherwise the 3D cursor."""
    sc = bpy.context.scene
    idx = sc.dualsense_pivot_index
    if 0 <= idx < len(sc.dualsense_pivots):
        o = sc.dualsense_pivots[idx].obj
        if o is not None:
            return o.matrix_world.translation.copy()
    return sc.cursor.location.copy()


def _drive_viewport(view, i, prefs):
    """Fly mode: forward = where the view points, rotation is around the eye."""
    if view is None:
        return
    rv3d = view[2]
    if rv3d.view_perspective == 'CAMERA':
        if S.msg != "Leave camera view (Numpad 0) to move the viewport":
            _say("Leave camera view (Numpad 0) to move the viewport")
        return
    ortho = rv3d.view_perspective == 'ORTHO'
    m = rv3d.view_matrix.inverted().to_3x3()
    right, up, fwd = m.col[0].copy(), m.col[1].copy(), -m.col[2]
    dist = max(rv3d.view_distance, 0.001)
    k = i.mul * i.dt

    # Move: X = strafe, Y = forward/back
    dy = 0.0
    dz = i.ly
    gz = i.height                                 # D-pad up/down: height (global Z)
    if ortho and dz:                  # no depth in orthographic: zoom instead
        rv3d.view_distance = min(
            max(dist * (1.0 - dz * prefs.zoom_speed * k), 0.001), 1.0e5)
        dz = 0.0
    if i.lx or dy or dz or gz:
        rv3d.view_location = rv3d.view_location + (
            (right * i.lx + up * dy + fwd * dz + Vector((0.0, 0.0, gz))) * dist * prefs.pan_speed * k)

    # Look around (like the camera) / roll: orbits when enabled
    ang = math.radians(prefs.rot_speed) * k
    yaw = pitch = roll = 0.0
    if i.r3:
        roll = -i.rx * ang
    else:
        yaw = -i.rx * ang
        pitch = i.ry * ang
    gyro = bool(i.gpitch or i.gyaw or i.groll)
    if yaw or pitch or roll or gyro:
        R = m.to_quaternion()
        R2 = (Quaternion((0.0, 0.0, 1.0), yaw) @ R
              @ Quaternion((1.0, 0.0, 0.0), pitch)
              @ Quaternion((0.0, 0.0, 1.0), roll))
        R2.normalize()
        eye = rv3d.view_location + R @ Vector((0.0, 0.0, dist))
        if S.orbit and not ortho and (yaw or pitch or roll):
            p = _pivot_point()
            eye = p + (R2 @ R.inverted()) @ (eye - p)
        if gyro:                      # gyroscope: always around the eye
            R2 = (Quaternion((0.0, 0.0, 1.0), i.gyaw) @ R2
                  @ Quaternion((1.0, 0.0, 0.0), i.gpitch)
                  @ Quaternion((0.0, 0.0, 1.0), i.groll))
            R2.normalize()
        if not ortho:                 # the eye stays put, the looked-at point rotates
            rv3d.view_location = eye - R2 @ Vector((0.0, 0.0, dist))
        rv3d.view_rotation = R2 if _vr_is_view_to_world(rv3d, m) else R2.inverted()


def _rec_key(cam, lens, focus, fstop=False):
    """Recording: location/rotation keyframes (and optics if changed)."""
    f = bpy.context.scene.frame_current
    cam.keyframe_insert("location", frame=f)
    mode = cam.rotation_mode
    path = ("rotation_quaternion" if mode == 'QUATERNION'
            else "rotation_axis_angle" if mode == 'AXIS_ANGLE'
            else "rotation_euler")
    cam.keyframe_insert(path, frame=f)
    if lens:
        cam.data.keyframe_insert("lens", frame=f)
    if focus:
        cam.data.dof.keyframe_insert("focus_distance", frame=f)
    if fstop:
        cam.data.dof.keyframe_insert("aperture_fstop", frame=f)


def _drive_camera(view, i, prefs):
    cam = bpy.context.scene.camera
    if cam is None:
        return
    loc, rot, sca = cam.matrix_world.decompose()
    rm = rot.to_matrix()
    right, up, fwd = rm.col[0].copy(), rm.col[1].copy(), -rm.col[2]
    k = i.mul * i.dt

    # Fly mode: X = strafe, Y = forward/back along the camera direction
    dy = 0.0
    dz = i.ly
    gz = i.height                                 # D-pad up/down: height (global Z)
    loc = loc + (right * i.lx + up * dy + fwd * dz
                 + Vector((0.0, 0.0, gz))) * prefs.move_speed * k

    # Look around / roll
    ang = math.radians(prefs.rot_speed) * k
    yaw = pitch = roll = 0.0
    if i.r3:
        roll = -i.rx * ang
    else:
        yaw = -i.rx * ang
        pitch = i.ry * ang

    # Bumpers + triggers: zoom (L1), focus distance (R1), f-stop (L1+R1)
    lens_changed = focus_changed = fstop_changed = False
    tr = i.r2 - i.l2
    if i.bumper and tr and cam.type == 'CAMERA':
        if i.bumper == 'L':
            lens = cam.data.lens * (1.0 + tr * prefs.zoom_speed * i.dt)
            cam.data.lens = min(max(lens, 8.0), 300.0)
            lens_changed = True
        elif i.bumper == 'B':                     # both bumpers: aperture
            dof = cam.data.dof
            dof.use_dof = True
            f = max(dof.aperture_fstop, 0.1) * (1.0 + tr * prefs.focus_speed * i.dt)
            dof.aperture_fstop = min(max(f, 0.1), 128.0)
            fstop_changed = True
        else:
            dof = cam.data.dof
            if dof.focus_object is None:
                dof.use_dof = True
                d = max(dof.focus_distance, 0.01) * (1.0 + tr * prefs.focus_speed * i.dt)
                dof.focus_distance = min(max(d, 0.01), 10000.0)
                focus_changed = True
            elif S.msg != "Remove the focus object from the camera":
                _say("Remove the focus object from the camera")

    new_rot = (Quaternion((0.0, 0.0, 1.0), yaw) @ rot
               @ Quaternion((1.0, 0.0, 0.0), pitch)
               @ Quaternion((0.0, 0.0, 1.0), roll))
    new_rot.normalize()
    if S.orbit and (yaw or pitch or roll):        # orbit around the pivot
        p = _pivot_point()
        loc = p + (new_rot @ rot.inverted()) @ (loc - p)
    if i.gpitch or i.gyaw or i.groll:             # gyroscope: around the camera itself
        new_rot = (Quaternion((0.0, 0.0, 1.0), i.gyaw) @ new_rot
                   @ Quaternion((1.0, 0.0, 0.0), i.gpitch)
                   @ Quaternion((0.0, 0.0, 1.0), i.groll))
        new_rot.normalize()
    cam.matrix_world = Matrix.LocRotScale(loc, new_rot, sca)

    if S.rec:
        try:
            _rec_key(cam, lens_changed, focus_changed, fstop_changed)
        except Exception:
            traceback.print_exc()


def _drive_selection_inner(view, i, prefs):
    vl = bpy.context.view_layer
    active = vl.objects.active
    if active is not None and active.mode not in {'OBJECT', 'POSE'}:
        return

    if view is not None:
        m = view[2].view_matrix.inverted().to_3x3()
        right, up, fwd = m.col[0].copy(), m.col[1].copy(), -m.col[2]
    else:
        right, up, fwd = Vector((1, 0, 0)), Vector((0, 0, 1)), Vector((0, 1, 0))

    k = i.mul * i.dt
    dy = 0.0 if i.depth else i.ly           # R1 held: left stick Y = depth
    dz = i.ly if i.depth else 0.0
    d_loc = ((right * i.lx + up * dy + fwd * dz + Vector((0.0, 0.0, i.height)))
             * prefs.move_speed * k)

    ang = math.radians(prefs.rot_speed) * k
    yaw = pitch = roll = 0.0
    s = 1.0
    # the object follows the stick (stick right = the front face goes right)
    if i.r3:
        roll = i.rx * ang
        s = max(0.01, 1.0 + i.ry * prefs.scale_speed * k)
    else:
        yaw = i.rx * ang
        pitch = -i.ry * ang
    d_rot = (Quaternion(up, yaw) @ Quaternion(right, pitch) @ Quaternion(fwd, roll))
    g_rot = None
    if i.gpitch or i.gyaw or i.groll:       # gyroscope: the object mimics the controller
        g_rot = (Quaternion(up, i.gyaw) @ Quaternion(right, i.gpitch)
                 @ Quaternion(fwd, -i.groll))
    pivot = _pivot_point() if i.pivot else None   # orbit on (L3 click) or L1 held

    # Armature in Pose Mode: active bone
    if active is not None and active.type == 'ARMATURE' and active.mode == 'POSE':
        bone = active.data.bones.active
        pb = active.pose.bones.get(bone.name) if bone else None
        if pb is not None:
            mw = active.matrix_world
            new = _apply_world(mw @ pb.matrix, d_loc, d_rot, s, pivot, g_rot)
            pb.matrix = mw.inverted_safe() @ new
        return

    for obj in vl.objects.selected:
        obj.matrix_world = _apply_world(obj.matrix_world, d_loc, d_rot, s, pivot, g_rot)


def _drive_selection(view, i, prefs):
    _drive_selection_inner(view, i, prefs)
    if S.rec:
        try:
            for t in _key_targets():
                _rec_key(t, False, False)
        except Exception:
            traceback.print_exc()


# --------------------------------------------------------------------------
# Buttons: keyframes, play, recording, gyroscope
# --------------------------------------------------------------------------

def _seq_reset():
    S.seq_kind = None
    S.seq_args = []


def _seq_cancel(text=None):
    _seq_reset()
    if text:
        _say(text)


def _seq_start(kind, now):
    S.seq_kind = kind
    S.seq_args = []
    S.seq_t = now
    nome = "rotation" if kind == 'rot' else "location"
    _say("Keyframe %s: Triangle = XYZ, or 2 axes "
         "(Square = X, Cross = Y, Circle = Z)" % nome)


def _key_targets():
    """What receives keyframes: the camera, or the active bone / selected objects."""
    sc = bpy.context.scene
    if S.target == 'CAMERA':
        return [sc.camera] if sc.camera else []
    vl = bpy.context.view_layer
    a = vl.objects.active
    if a is not None and a.type == 'ARMATURE' and a.mode == 'POSE':
        b = a.data.bones.active
        pb = a.pose.bones.get(b.name) if b else None
        return [pb] if pb else []
    return list(vl.objects.selected)


def _commit(kind, axes):
    targets = _key_targets()
    _seq_reset()
    if not targets:
        _say("No target for the keyframe")
        return
    frame = bpy.context.scene.frame_current
    try:
        for t in targets:
            if kind == 'loc':
                path, idx = "location", axes
            else:
                mode = t.rotation_mode
                if mode == 'QUATERNION':
                    path, idx = "rotation_quaternion", (0, 1, 2, 3)
                elif mode == 'AXIS_ANGLE':
                    path, idx = "rotation_axis_angle", (0, 1, 2, 3)
                else:
                    path, idx = "rotation_euler", axes
            for a in idx:
                t.keyframe_insert(data_path=path, index=a, frame=frame)
    except Exception as e:
        traceback.print_exc()
        _say("Keyframe failed: %s" % e)
        return
    nome = "location" if kind == 'loc' else "rotation"
    _say("Keyframe %s %s (frame %d)" % (nome, "".join("XYZ"[a] for a in axes), frame))
    _rumble(0.3, 0.3, 70)
    _redraw_ui()


def _seq_press(b, now):
    """A button pressed while a sequence is in progress."""
    S.seq_t = now
    if b == BTN_TRIANGLE and not S.seq_args:
        _commit(S.seq_kind, (0, 1, 2))
    elif b in AXIS_OF:
        S.seq_args.append(AXIS_OF[b])
        if len(S.seq_args) == 2:
            _commit(S.seq_kind, tuple(sorted(set(S.seq_args))))
        else:
            _say("Now the second axis (same button = single axis)")
    else:
        _seq_cancel("Sequence cancelled")


def _toggle_play():
    wm = bpy.context.window_manager
    if not wm.windows:
        return
    win = wm.windows[0]
    try:
        with bpy.context.temp_override(window=win, screen=win.screen):
            bpy.ops.screen.animation_play()
    except Exception:
        traceback.print_exc()


def _is_playing():
    wm = bpy.context.window_manager
    return bool(wm.windows) and wm.windows[0].screen.is_animation_playing


def _rewind():
    if _is_playing():
        _toggle_play()
    sc = bpy.context.scene
    sc.frame_current = sc.frame_start
    _say("Playhead at the start (frame %d)" % sc.frame_start)
    _rumble(0.2, 0.2, 50)


def _x_tap(now):
    """Cross tap: play/stop; two quick taps: back to the start."""
    if now - S.x_last_tap < DOUBLE_TAP:
        S.x_last_tap = -10.0
        _rewind()
    else:
        S.x_last_tap = now
        _toggle_play()


def _fcurves(idb):
    ad = getattr(idb, "animation_data", None)
    act = ad.action if ad else None
    if act is None:
        return []
    try:
        return list(act.fcurves)                 # Blender < 5.0
    except Exception:
        pass
    out = []
    try:
        for layer in act.layers:                 # layered actions
            for strip in layer.strips:
                cb = strip.channelbag(ad.action_slot)
                if cb:
                    out += list(cb.fcurves)
    except Exception:
        pass
    return out


def _target_frames():
    frames = set()
    for t in _key_targets():
        owner = t.id_data
        prefix = None if owner is t else 'pose.bones["%s"]' % t.name
        idbs = [owner]
        if owner.type == 'CAMERA':
            idbs.append(owner.data)
        for idb in idbs:
            for fc in _fcurves(idb):
                if prefix and idb is owner and not fc.data_path.startswith(prefix):
                    continue
                for kp in fc.keyframe_points:
                    frames.add(round(kp.co[0]))
    return frames


def _jump_keyframe(direction):
    """Moves the playhead to the target's previous/next keyframe."""
    sc = bpy.context.scene
    frames = _target_frames()
    cur = sc.frame_current
    cand = [f for f in frames if (f > cur if direction > 0 else f < cur)]
    if not cand:
        _say("No keyframe " + ("after" if direction > 0 else "before"))
        return
    sc.frame_current = min(cand) if direction > 0 else max(cand)
    _say("Keyframe: frame %d" % sc.frame_current)
    _rumble(0.0, 0.2, 40)


def _toggle_orbit():
    S.orbit = not S.orbit
    _say("Orbit around pivot: " + ("on" if S.orbit else "off"))
    _rumble(0.0, 0.5 if S.orbit else 0.2, 80)


def _toggle_rec():
    S.rec = not S.rec
    _say("Recording " + ("on" if S.rec else "off"))
    _rumble(0.0, 0.5 if S.rec else 0.2, 80)


def _x_button(btn, now):
    """Cross only (viewport mode): play/stop, double tap = start, held = gyroscope."""
    x = btn[BTN_CROSS]
    was = S.prev_btn.get(BTN_CROSS, False)
    if x and not was:
        S.x_down_t = now
        S.x_fired = False
    if S.x_down_t is not None:
        if x and not S.x_fired and now - S.x_down_t > HOLD_X:
            S.x_fired = True
            _gyro_set(not S.gyro_on)
        if was and not x:
            if not S.x_fired:
                _x_tap(now)
            S.x_down_t = None


def _camera_buttons(btn, now):
    prev = S.prev_btn
    if S.seq_kind is not None and now - S.seq_t > SEQ_TIMEOUT:
        _seq_cancel("Sequence timed out")

    for b in (BTN_TRIANGLE, BTN_SQUARE, BTN_CROSS, BTN_CIRCLE):
        was = prev.get(b, False)
        pressed = btn[b] and not was
        released = was and not btn[b]

        if pressed:
            if S.seq_kind is not None:
                _seq_press(b, now)
                continue
            if b == BTN_TRIANGLE:
                _seq_start('rot', now)
            elif b == BTN_SQUARE:
                _seq_start('loc', now)
            elif b == BTN_CIRCLE:
                _toggle_rec()
            elif b == BTN_CROSS:
                S.x_down_t = now
                S.x_fired = False

        if b == BTN_CROSS and S.x_down_t is not None:
            if btn[b] and not S.x_fired and now - S.x_down_t > HOLD_X:
                S.x_fired = True
                _gyro_set(not S.gyro_on)
            if released:
                if not S.x_fired:
                    _x_tap(now)
                S.x_down_t = None

    # D-pad left/right: previous/next keyframe
    for b, d in ((BTN_LEFT, -1), (BTN_RIGHT, 1)):
        if btn[b] and not prev.get(b, False):
            _jump_keyframe(d)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _camera_view(on):
    """Switch the main 3D view to the active camera (or back to a normal view)."""
    view = _find_view3d()
    if view is None:
        return
    rv3d = view[2]
    try:
        if on and bpy.context.scene.camera is not None:
            rv3d.view_perspective = 'CAMERA'
        elif not on and rv3d.view_perspective == 'CAMERA':
            rv3d.view_perspective = 'PERSP'
    except Exception:
        traceback.print_exc()


def _set_target(target):
    if target == 'CAMERA' and S.target != 'CAMERA':
        _camera_view(True)                # entering camera mode: look through the camera
    elif target == 'VIEWPORT' and S.target == 'CAMERA':
        _camera_view(False)               # viewport mode needs a normal view
    S.target = target
    _seq_reset()
    S.x_down_t = None
    S.bumper = None
    _redraw_ui()


def _toggle_pause():
    S.paused = not S.paused
    S.bumper = None
    S.x_down_t = None
    _seq_reset()
    _say("Control paused" if S.paused else "Control active")
    _rumble(0.0, 0.2 if S.paused else 0.5, 80)


def _next_mode():
    keys = [k for k, _n, _d in TARGETS]
    nxt = keys[(keys.index(S.target) + 1) % len(keys)]
    _set_target(nxt)
    _say("Mode: " + next(n for k, n, _d in TARGETS if k == nxt))
    _rumble(0.0, 0.4, 80)


def _cycle_pivot():
    """Touchpad click: next pivot slot, then the 3D cursor, then back to the first."""
    sc = bpy.context.scene
    slots = sc.dualsense_pivots
    order = [i for i in range(len(slots)) if slots[i].obj is not None] + [-1]
    cur = sc.dualsense_pivot_index
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
    sc.dualsense_pivot_index = nxt
    _say("Pivot: " + (slots[nxt].obj.name if nxt >= 0 else "3D cursor"))
    _rumble(0.0, 0.3, 60)
    _redraw_ui()


def _tick_body():
    prefs = _prefs()
    now = time.perf_counter()
    dt = min(now - S.last_t, 0.1)
    S.last_t = now
    S.pygame.event.pump()
    S.pygame.event.clear()      # clear the queue: we only need the direct state

    if S.msg and now - S.msg_t > 3.5:
        S.msg = ""
        _redraw_ui()

    if S.ctrl is not None and not S.ctrl.attached():
        _close_controller()
        _hid_close()                      # stale handle: reopened when the pad is back
        S.l2 = S.r2 = 0.0
        S.trig_q = (0, 0)
        S.prev_btn = {}
        S.bumper = None
        S.x_down_t = None
        _redraw_ui()
    if S.ctrl is None:
        if now - S.last_scan > 1.0:
            S.last_scan = now
            _open_controller()
        return 0.1

    lx, ly, rx, ry, l2, r2, btn = _read_inputs(S.ctrl, prefs)
    q = (round(l2 * 20), round(r2 * 20))
    if q != S.trig_q:                     # trigger bars in the panel
        S.trig_q = q
        S.l2, S.r2 = l2, r2
        _redraw_ui()

    # Create button = pause/resume, Options button = change mode
    if btn[BTN_CREATE] and not S.prev_btn.get(BTN_CREATE, False):
        _toggle_pause()
    if btn[BTN_OPTIONS] and not S.prev_btn.get(BTN_OPTIONS, False):
        _next_mode()
    click = btn[BTN_TOUCHPAD] or S.pad_click     # SDL button or raw HID bit
    if click and not S.click_prev and not S.paused:
        _cycle_pivot()
    S.click_prev = click
    if S.paused:
        S.prev_btn = btn
        _hid_step(now, dt, prefs, use=False)      # drain the data without using it
        _trigger_step(now, prefs)
        return FRAME
    cam_mode = S.target == 'CAMERA'

    # bumpers: L1 = zoom, R1 = focus, both = f-stop
    l1, r1 = btn[BTN_L1], btn[BTN_R1]
    S.bumper = 'B' if (l1 and r1) else ('L' if l1 else ('R' if r1 else None))
    bump = S.bumper if cam_mode else None

    if btn[BTN_L3] and not S.prev_btn.get(BTN_L3, False):
        _toggle_orbit()                   # left stick click (all modes)
    if S.target in ('CAMERA', 'SELECTION'):
        _camera_buttons(btn, now)
    elif S.target == 'VIEWPORT':
        _x_button(btn, now)
    elif S.target == 'CUSTOM':
        _custom_buttons(btn, now)
    S.prev_btn = btn

    g = _hid_step(now, dt, prefs)
    custom = S.target == 'CUSTOM'
    custom_changed = False
    if custom:
        try:
            custom_changed = _custom_step(dt, l2, r2)
        except Exception as e:
            traceback.print_exc()
            _say("Parameter update failed: %s" % e)

    height = float(btn[BTN_UP]) - float(btn[BTN_DOWN])

    tr = (r2 - l2) if bump else 0.0
    if abs(tr) < 0.03:
        tr = 0.0
    moved = (any(abs(v) > 1e-4 for v in (lx, ly, rx, ry))
             or tr != 0.0 or height != 0.0 or any(g))
    if moved:
        i = SimpleNamespace(
            lx=lx, ly=ly, rx=rx, ry=ry,
            l3=btn[BTN_L3], r3=btn[BTN_R3],
            height=height, depth=(S.target == 'SELECTION' and S.bumper == 'R'),
            pivot=(S.target == 'SELECTION' and (S.orbit or S.bumper == 'L')),
            bumper=bump, l2=l2 if tr else 0.0, r2=r2 if tr else 0.0,
            gpitch=g[0], gyaw=g[1], groll=g[2],
            mul=1.0 if (bump or custom) else (1.0 + 2.0 * r2) * (1.0 - 0.85 * l2), dt=dt)
        view = _find_view3d()
        try:
            if S.target in ('VIEWPORT', 'CUSTOM'):
                _drive_viewport(view, i, prefs)
            elif cam_mode:
                _drive_camera(view, i, prefs)
            else:
                _drive_selection(view, i, prefs)
            S.msg_err = ""
        except Exception as e:            # e.g. locked or linked object: do not stop everything
            if S.msg_err != str(e):
                S.msg_err = str(e)
                traceback.print_exc()
                _say("Movement failed: %s" % e)
        _redraw_ui()
        S.moving = True
        S.dirty = S.dirty or S.target not in ('VIEWPORT', 'CUSTOM')
    if custom_changed:
        S.moving = True
        S.dirty = True
        _redraw_ui()
    elif not moved and S.moving:
        S.moving = False
        if S.dirty:       # one undo step (Ctrl+Z) per movement
            S.dirty = False
            try:
                bpy.ops.ed.undo_push(message="DualSense")
            except Exception:
                pass
    _trigger_step(now, prefs)
    return FRAME


def _tick():
    if not S.running:
        return None
    try:
        return _tick_body()
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        last = tb.strip().splitlines()
        where = next((l.strip() for l in reversed(last) if l.strip().startswith("File ")), "")
        S.error = "%s: %s" % (type(e).__name__, e)
        S.error_where = where[-90:]
        _stop()
        _redraw_ui()
        return None


def _start(paused=False):
    S.error = ""
    pygame, sdlc = _import_pygame()
    pygame.display.init()      # "dummy" driver: needed for event.pump()
    pygame.joystick.init()
    sdlc.init()
    S.pygame, S.sdlc = pygame, sdlc
    S.ctrl = None
    S.name = ""
    S.prev_btn = {}
    S.moving = False
    S.dirty = False
    S.bumper = None
    S.rec = False
    S.paused = paused
    S.x_down_t = None
    _seq_reset()
    S.last_t = time.perf_counter()
    S.last_scan = 0.0
    S.running = True
    if not bpy.app.timers.is_registered(_tick):
        # persistent: stays active even if you open another .blend file
        bpy.app.timers.register(_tick, first_interval=0.0, persistent=True)


def _stop():
    S.running = False
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _trigger_reset()
    _gyro_close()
    _hid_close()
    _close_controller()
    if S.sdlc is not None:
        try:
            S.sdlc.quit()
        except Exception:
            pass
    S.moving = False
    S.rec = False
    S.orbit = False
    S.bumper = None
    _seq_reset()


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------

class DS_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    deadzone: FloatProperty(
        name="Deadzone", default=0.12, min=0.0, max=0.5,
        description="Area at the center of the stick that is ignored")
    expo: FloatProperty(
        name="Curve", default=2.0, min=1.0, max=3.0,
        description="1 = linear, higher values = more precision for small movements")
    move_speed: FloatProperty(
        name="Move speed", default=2.0, min=0.01, max=100.0,
        description="Blender units per second (objects and camera)")
    rot_speed: FloatProperty(
        name="Rotation speed", default=90.0, min=1.0, max=720.0,
        description="Degrees per second")
    scale_speed: FloatProperty(
        name="Scale speed", default=0.5, min=0.01, max=5.0,
        description="Scale change per second")
    pan_speed: FloatProperty(
        name="Viewport move speed", default=0.6, min=0.01, max=5.0,
        description="Fraction of the view distance per second")
    zoom_speed: FloatProperty(
        name="Zoom speed", default=1.0, min=0.01, max=5.0,
        description="Viewport zoom and camera focal length")
    focus_speed: FloatProperty(
        name="Focus / f-stop speed", default=1.0, min=0.01, max=5.0,
        description="Change of focus distance and f-stop per second")
    gyro_sens: FloatProperty(
        name="Gyroscope sensitivity", default=1.0, min=0.1, max=5.0,
        description="1 = the camera rotates like the controller")
    touchpad: BoolProperty(
        name="Touchpad", default=True,
        description="Swiping a finger on the touchpad rotates camera/view/object around themselves")
    touch_sens: FloatProperty(
        name="Touchpad sensitivity", default=0.1, min=0.01, max=1.0,
        description="Degrees per pixel (the touchpad is 1920 pixels wide)")
    gyro_deadzone: FloatProperty(
        name="Gyroscope deadzone", default=1.0, min=0.0, max=10.0,
        description="Degrees per second ignored (jitter)")
    invert_lx: BoolProperty(name="Invert left stick X", default=False)
    invert_ly: BoolProperty(name="Invert left stick Y", default=False)
    invert_rx: BoolProperty(name="Invert right stick X", default=False)
    invert_ry: BoolProperty(name="Invert right stick Y", default=False)
    gyro_invert_x: BoolProperty(name="Invert gyroscope up/down", default=False)
    gyro_invert_y: BoolProperty(name="Invert gyroscope left/right", default=False)
    gyro_invert_z: BoolProperty(name="Invert gyroscope roll", default=False)
    gyro_offset: FloatVectorProperty(name="Gyroscope offset", size=3, default=(0.0, 0.0, 0.0),
                                     options={'HIDDEN'})
    gyro_calibrated: BoolProperty(name="Gyroscope calibrated", default=False,
                                  options={'HIDDEN'})
    adaptive_triggers: BoolProperty(
        name="Adaptive triggers", default=True,
        description="Resistance on L2/R2: stiff at the zoom, focus and f-stop limits, "
                    "and a speed-dependent push-back when the triggers control speed")
    trigger_strength: FloatProperty(
        name="Trigger strength", default=1.0, min=0.0, max=1.0,
        description="How strong the trigger resistance feels")
    rumble: BoolProperty(
        name="Confirmation rumble", default=True)
    hud: BoolProperty(
        name="Viewport HUD", default=True,
        description="Show mode, recording, gyroscope and orbit status in the 3D viewport")
    autostart: BoolProperty(
        name="Auto start", default=True,
        description="Start reading the controller when Blender opens (paused: "
                    "press Create on the pad or Resume in the panel)")

    def draw(self, context):
        layout = self.layout
        if _pygame_available() and _hid_available():
            box = layout.box()
            box.label(text="pygame and hidapi installed", icon='CHECKMARK')
        else:
            _draw_install(layout)
        box = layout.box()
        box.label(text="General", icon='PREFERENCES')
        col = box.column()
        col.use_property_split = True
        for name in ("autostart", "hud", "rumble"):
            col.prop(self, name)
        box = layout.box()
        box.label(text="Where to find it", icon='INFO')
        col = box.column(align=True)
        col.active = False
        col.label(text="3D Viewport sidebar (N) > DualSense: start, modes, pivot, settings")
        col.label(text="Open 'Controls' there for the full button map")


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

def _pip_install(pkg):
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "--target", LIBDIR, pkg],
        capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout)[-300:]


def _install_worker(wanted):
    """Runs pip in a thread, so Blender stays responsive while it downloads."""
    try:
        subprocess.run([sys.executable, "-m", "ensurepip", "--upgrade"],
                       capture_output=True, text=True)
        for candidates in wanted:
            err = ""
            for pkg in candidates:
                ok, err = _pip_install(pkg)
                if ok:
                    break
            else:
                S.install_result = "Install failed: " + err.strip().splitlines()[-1] if err else "Install failed"
                return
        S.install_result = "OK"
    except Exception as e:
        S.install_result = "Install failed: %s" % e
    finally:
        S.installing = False


def _install_poll():
    _redraw_ui()
    if S.installing:
        return 0.5
    importlib.invalidate_caches()
    if S.install_result == "OK":
        _say("pygame and hidapi installed: press Start")
        try:
            if _prefs().autostart and not S.running:
                _start()
        except Exception as e:
            S.error = "Start failed: %s" % e
    _redraw_ui()
    return None


class DS_OT_install_pygame(bpy.types.Operator):
    bl_idname = "dualsense.install_pygame"
    bl_label = "Install pygame and hidapi"
    bl_description = "Download pygame and hidapi (needs internet, once)"

    @classmethod
    def poll(cls, context):
        return not S.installing

    def execute(self, context):
        if not getattr(bpy.app, "online_access", True):
            self.report({'ERROR'}, "Online access is disabled: enable it in "
                                   "Preferences > System > Network")
            return {'CANCELLED'}
        wanted = []
        if not _pygame_available():
            wanted.append(("pygame-ce", "pygame"))
        if not _hid_available():
            wanted.append(("hidapi",))
        if not wanted:
            return {'CANCELLED'}
        S.installing = True
        S.install_result = ""
        threading.Thread(target=_install_worker, args=(wanted,), daemon=True).start()
        if not bpy.app.timers.is_registered(_install_poll):
            bpy.app.timers.register(_install_poll, first_interval=0.5)
        self.report({'INFO'}, "Installing in the background...")
        return {'FINISHED'}


def _component_items(self, context):
    names = "XYZW" if self.size <= 4 else None
    return [(str(i), names[i] if names else str(i), "") for i in range(max(self.size, 1))]


def _make_map(context, path, prop, index):
    """Creates a mapping for path.prop[index] with sensible defaults. Returns it or an error."""
    owner = _walk(path)
    if owner is None or not hasattr(owner, "bl_rna"):
        return "This field can't be assigned (its path can't be resolved)"
    rna = owner.bl_rna.properties.get(prop)
    if rna is None or rna.type not in {'FLOAT', 'INT'}:
        return "Only number fields can be assigned"
    if rna.is_readonly:
        return "This field is read-only"
    sc = context.scene
    m = sc.dualsense_maps.add()
    m.path, m.prop, m.index = path, prop, index
    res = _map_resolve(m)
    cur = _map_get(res)
    lo, hi = float(rna.soft_min), float(rna.soft_max)
    if not (-1e6 < lo < hi < 1e6):            # unbounded fields: a range around the value
        span = max(abs(cur), 1.0)
        lo, hi = cur - span, cur + span
    m.vmin, m.vmax, m.rest = lo, hi, cur
    m.speed = (hi - lo) / 2.0
    owner_name = getattr(owner, "name", "") or getattr(owner.id_data, "name", "")
    comp = ""
    if index >= 0:
        comp = " " + ("XYZW"[index] if rna.array_length <= 4 else str(index))
    m.name = ("%s · %s%s" % (owner_name, rna.name, comp)) if owner_name else rna.name + comp
    sc.dualsense_map_index = len(sc.dualsense_maps) - 1
    S.map_last_t = 0.0
    S.map_frac = 0.0
    return m


class DS_OT_map_assign(bpy.types.Operator):
    bl_idname = "dualsense.map_assign"
    bl_label = "Assign to DualSense Triggers"
    bl_description = "L2 + R2 will drive this value in Custom mode"
    bl_options = {'UNDO'}

    path: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    prop: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    size: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    component: EnumProperty(name="Component", items=_component_items)

    @classmethod
    def poll(cls, context):
        ptr = getattr(context, "button_pointer", None)
        prop = getattr(context, "button_prop", None)
        return (ptr is not None and prop is not None and prop.type in {'FLOAT', 'INT'}
                and not prop.is_readonly)

    def invoke(self, context, event):
        ptr, prop = context.button_pointer, context.button_prop
        self.path = repr(ptr)
        self.prop = prop.identifier
        self.size = prop.array_length if getattr(prop, "is_array", False) else 0
        if self.size > 1:
            return context.window_manager.invoke_props_dialog(self, width=240)
        return self.execute(context)

    def draw(self, context):
        self.layout.label(text="Which component?")
        self.layout.row().prop(self, "component", expand=True)

    def execute(self, context):
        idx = int(self.component) if self.size > 1 else -1
        m = _make_map(context, self.path, self.prop, idx)
        if isinstance(m, str):
            self.report({'ERROR'}, m)
            return {'CANCELLED'}
        self.report({'INFO'}, "L2 + R2 now drive '%s' in Custom mode" % m.name)
        _say("Assigned: %s (Custom mode)" % m.name)
        return {'FINISHED'}


class DS_OT_map_paste(bpy.types.Operator):
    bl_idname = "dualsense.map_paste"
    bl_label = "Paste Data Path"
    bl_description = ("Assign from a full data path in the clipboard "
                      "(right-click a field > Copy Full Data Path)")
    bl_options = {'UNDO'}

    def execute(self, context):
        text = (context.window_manager.clipboard or "").strip()
        m = re.match(r'^(bpy\.data.*)\.([A-Za-z]\w*)(?:\[(\d+)\])?$', text)
        if not m:
            self.report({'ERROR'}, "The clipboard doesn't contain a full data path")
            return {'CANCELLED'}
        res = _make_map(context, m.group(1), m.group(2), int(m.group(3)) if m.group(3) else -1)
        if isinstance(res, str):
            self.report({'ERROR'}, res)
            return {'CANCELLED'}
        self.report({'INFO'}, "Assigned '%s'" % res.name)
        return {'FINISHED'}


class DS_OT_map_remove(bpy.types.Operator):
    bl_idname = "dualsense.map_remove"
    bl_label = "Remove Parameter"
    bl_description = "Remove the selected assignment"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_map() is not None

    def execute(self, context):
        sc = context.scene
        i = sc.dualsense_map_index
        sc.dualsense_maps.remove(i)
        sc.dualsense_map_index = min(i, len(sc.dualsense_maps) - 1)
        return {'FINISHED'}


class DS_OT_map_rest(bpy.types.Operator):
    bl_idname = "dualsense.map_rest"
    bl_label = "Rest = Current"
    bl_description = "Absolute mode: the value with the triggers released becomes the current one"
    bl_options = {'UNDO'}

    @classmethod
    def poll(cls, context):
        m = _active_map()
        return m is not None and _map_resolve(m) is not None

    def execute(self, context):
        m = _active_map()
        m.rest = _map_get(_map_resolve(m))
        return {'FINISHED'}


class DS_OT_toggle(bpy.types.Operator):
    bl_idname = "dualsense.toggle"
    bl_label = "Start / stop"
    bl_description = "Start or stop reading the controller"

    def execute(self, context):
        if S.running:
            _stop()
        else:
            try:
                _start()
            except Exception as e:
                traceback.print_exc()
                S.error = "Start failed: %s" % e
                self.report({'ERROR'}, S.error)
                _stop()
                return {'CANCELLED'}
        _redraw_ui()
        return {'FINISHED'}


class DS_OT_set_target(bpy.types.Operator):
    bl_idname = "dualsense.set_target"
    bl_label = "Target"
    bl_description = "Choose what the controller drives"

    target: EnumProperty(items=[(k, n, d) for k, n, d in TARGETS])

    def execute(self, context):
        _set_target(self.target)
        return {'FINISHED'}


class DS_OT_action(bpy.types.Operator):
    bl_idname = "dualsense.action"
    bl_label = "Action"
    bl_description = "Recording, gyroscope, gyroscope calibration or play"

    action: EnumProperty(items=[('ORBIT', "Orbit", ""),
                                ('REC', "Recording", ""),
                                ('GYRO', "Gyroscope", ""),
                                ('CALIBRATE', "Calibrate gyroscope", ""),
                                ('TRIGGER_TEST', "Test triggers", ""),
                                ('PAUSE', "Pause / Resume", ""),
                                ('PLAY', "Play / Stop", "")])

    @classmethod
    def poll(cls, context):
        return S.running

    def execute(self, context):
        if self.action == 'ORBIT':
            _toggle_orbit()
        elif self.action == 'REC':
            _toggle_rec()
        elif self.action == 'GYRO':
            _gyro_set(not S.gyro_on)
        elif self.action == 'CALIBRATE':
            _gyro_calibrate()
        elif self.action == 'PAUSE':
            _toggle_pause()
        elif self.action == 'TRIGGER_TEST':
            S.trig_fail = 0
            S.trig_info = "testing..."
            S.trig_test_until = time.perf_counter() + 3.0
            _say("Both triggers are stiff for 3 seconds")
        else:
            _toggle_play()
        return {'FINISHED'}


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

MODE_ICONS = {'SELECTION': 'OBJECT_DATAMODE', 'CAMERA': 'OUTLINER_OB_CAMERA',
              'VIEWPORT': 'VIEW3D', 'CUSTOM': 'DRIVER'}


def _draw_install(layout):
    box = layout.box()
    if S.installing:
        box.label(text="Installing pygame and hidapi...", icon='TIME')
        sub = box.row()
        sub.active = False
        sub.label(text="This can take a minute; Blender stays usable")
        return
    box.label(text="One-time setup needed", icon='ERROR')
    col = box.column(align=True)
    col.active = False
    col.label(text="The add-on uses pygame (sticks, buttons)")
    col.label(text="and hidapi (gyroscope, touchpad, triggers)")
    row = box.row()
    row.scale_y = 1.5
    row.operator("dualsense.install_pygame", icon='IMPORT')
    if S.install_result and S.install_result != "OK":
        r = box.row()
        r.alert = True
        r.label(text=S.install_result[:90], icon='CANCEL')


def _status():
    if not S.running:
        return 'OFF'
    if S.ctrl is None:
        return 'WAIT'
    return 'PAUSE' if S.paused else 'LIVE'


_STATUS_ICON = {'OFF': 'RADIOBUT_OFF', 'WAIT': 'SEQUENCE_COLOR_03',
                'LIVE': 'SEQUENCE_COLOR_04', 'PAUSE': 'PAUSE'}


class DS_PT_main(bpy.types.Panel):
    bl_label = "DualSense"
    bl_idname = "DS_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DualSense"

    def draw_header(self, context):
        self.layout.label(text="", icon=_STATUS_ICON[_status()])

    def draw(self, context):
        layout = self.layout
        if not (_pygame_available() and _hid_available()):
            _draw_install(layout)
            return

        st = _status()
        row = layout.row()
        row.scale_y = 1.6
        row.operator("dualsense.toggle", text="Stop" if S.running else "Start",
                     icon='PAUSE' if S.running else 'PLAY', depress=S.running)

        # status card
        box = layout.box()
        col = box.column(align=True)
        if st == 'OFF':
            col.label(text="Not running", icon='RADIOBUT_OFF')
        elif st == 'WAIT':
            col.label(text="Waiting for the controller...", icon='TIME')
            hint = col.row()
            hint.active = False
            hint.label(text="Connect it with USB-C or pair it over Bluetooth", icon='BLANK1')
        else:
            link = {True: "Bluetooth", False: "USB"}.get(S.hid_bt, "")
            col.label(text=S.name + ("  ·  " + link if link else ""),
                      icon='PAUSE' if st == 'PAUSE' else 'CHECKMARK')
            if st == 'PAUSE':
                hint = col.row()
                hint.active = False
                hint.label(text="Paused: press Create on the pad, or Resume", icon='BLANK1')
        if S.error:
            r = box.column(align=True)
            r.alert = True
            r.label(text=S.error, icon='ERROR')
            if S.error_where:
                r.label(text=S.error_where, icon='BLANK1')
        if S.running and S.ctrl is not None:      # how far each trigger is pressed
            row = box.row(align=True)
            row.active = not S.paused
            for name, v in (("L2", S.l2), ("R2", S.r2)):
                if hasattr(row, "progress"):
                    row.progress(factor=min(max(v, 0.0), 1.0), type='BAR',
                                 text="%s  %d%%" % (name, round(v * 100)))
                else:
                    row.label(text="%s %d%%" % (name, round(v * 100)))

        if S.running:
            row = layout.row()
            row.scale_y = 1.3
            row.operator("dualsense.action", text="Resume" if S.paused else "Pause",
                         icon='PLAY' if S.paused else 'PAUSE',
                         depress=S.paused).action = 'PAUSE'

        # mode selector
        row = layout.row(align=True)
        row.scale_y = 1.4
        for key, name, _desc in TARGETS:
            op = row.operator("dualsense.set_target", text=name,
                              icon=MODE_ICONS[key], depress=(S.target == key))
            op.target = key

        # live tools
        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.3
        if S.target != 'VIEWPORT':
            sub = row.row(align=True)
            sub.alert = S.rec                      # red while recording
            sub.operator("dualsense.action", text="Rec", icon='REC',
                         depress=S.rec).action = 'REC'
        row.operator("dualsense.action", text="Gyro", icon='ORIENTATION_GIMBAL',
                     depress=S.gyro_on).action = 'GYRO'
        row.operator("dualsense.action", text="", icon='PLAY').action = 'PLAY'
        col.operator("dualsense.action", text="Orbit around pivot (L3)",
                     icon='PIVOT_CURSOR', depress=S.orbit).action = 'ORBIT'
        calibrated = _prefs().gyro_calibrated
        col.operator("dualsense.action",
                     text="Calibrate gyroscope" if calibrated else "Calibrate gyroscope (not done yet)",
                     icon='CHECKMARK' if calibrated else 'FILE_REFRESH',
                     depress=S.gyro_cal).action = 'CALIBRATE'

        # adaptive triggers
        row = layout.row(align=True)
        row.prop(_prefs(), "adaptive_triggers", text="Adaptive triggers", toggle=True,
                 icon='FORCE_FORCE')
        row.operator("dualsense.action", text="Test").action = 'TRIGGER_TEST'
        if S.trig_info and not S.trig_info.startswith("OK"):
            r = layout.row()
            r.alert = S.trig_fail >= 3
            r.label(text="Triggers: " + S.trig_info, icon='INFO')

        if S.msg:
            r = layout.row()
            r.active = False
            r.label(text=S.msg, icon='INFO')


CONTROLS = (
    ("Camera", 'OUTLINER_OB_CAMERA', (
        "Left stick: fly",
        "D-pad up/down: height (global Z)",
        "D-pad left/right: prev/next keyframe",
        "Right stick: look (hold R3: roll)",
        "L1 + L2/R2: zoom",
        "R1 + L2/R2: focus distance",
        "L1 + R1 + L2/R2: f-stop (R2 = higher)",
        "Triangle / Square: rotation / location keyframe",
        "  then Triangle = XYZ, or 2 of Square=X Cross=Y Circle=Z",
        "Circle: recording on/off",
    )),
    ("Viewport", 'VIEW3D', (
        "Same as the camera: fly, look, orbit",
        "Cross and gyroscope work too",
    )),
    ("Object / Pose", 'OBJECT_DATAMODE', (
        "Same buttons as the camera",
        "Left stick: move (R1 held: depth)",
        "Right stick: rotate (R3 held: roll, scale)",
        "L1 held: rotate around the pivot",
        "L2: slow    R2: fast  (R2 pushes back more the further it goes)",
    )),
    ("Custom", 'DRIVER', (
        "L2 + R2: drive the assigned parameter (triggers at max stiffness)",
        "D-pad left/right: previous/next parameter",
        "Triangle: keyframe the parameter now",
        "Circle: recording on/off (keys while you press)",
        "Sticks: fly the viewport",
        "Assign: right-click a number > Assign to DualSense Triggers",
    )),
    ("Everywhere", 'PREFERENCES', (
        "L3 (click): orbit around pivot on/off",
        "Cross: play/stop - double tap: to start",
        "Cross held: gyroscope on/off",
        "Touchpad swipe: rotate around itself",
        "Touchpad click: cycle pivot",
        "Create: pause / resume",
        "Options: change mode",
    )),
)


class DS_PT_commands(bpy.types.Panel):
    bl_label = "Controls"
    bl_idname = "DS_PT_commands"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DualSense"
    bl_parent_id = "DS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        for title, icon, lines in CONTROLS:
            box = self.layout.box()
            box.label(text=title, icon=icon)
            col = box.column(align=True)
            for line in lines:
                col.label(text=line)


SETTINGS_GROUPS = (
    ("Sticks", 'MOUSE_MMB', ("deadzone", "expo", "invert_lx", "invert_ly",
                             "invert_rx", "invert_ry")),
    ("Speed", 'TIME', ("move_speed", "rot_speed", "scale_speed", "pan_speed",
                       "zoom_speed", "focus_speed")),
    ("Gyroscope", 'ORIENTATION_GIMBAL', ("gyro_sens", "gyro_deadzone", "gyro_invert_x",
                                         "gyro_invert_y", "gyro_invert_z")),
    ("Touchpad", 'GRID', ("touchpad", "touch_sens")),
    ("Triggers", 'FORCE_FORCE', ("adaptive_triggers", "trigger_strength")),
    ("General", 'PREFERENCES', ("hud", "rumble", "autostart")),
)


class DS_PT_settings(bpy.types.Panel):
    bl_label = "Settings"
    bl_idname = "DS_PT_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DualSense"
    bl_parent_id = "DS_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        p = _prefs()
        for title, icon, names in SETTINGS_GROUPS:
            box = layout.box()
            box.label(text=title, icon=icon)
            col = box.column(align=True)
            for name in names:
                col.prop(p, name)


# --------------------------------------------------------------------------
# Viewport HUD
# --------------------------------------------------------------------------

def _pivot_name():
    sc = bpy.context.scene
    i = sc.dualsense_pivot_index
    if 0 <= i < len(sc.dualsense_pivots):
        o = sc.dualsense_pivots[i].obj
        if o is not None:
            return o.name
    return "3D cursor"


def _hud_text(font, x, y, text, color):
    blf.color(font, *color)
    blf.position(font, x, y, 0)
    blf.draw(font, text)


def _rrect(shader, x0, y0, x1, y1, r, color, seg=6):
    """Rounded rectangle (triangle fan from the centre)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
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


def _draw_hud():
    """Status line at the top of every 3D viewport while the add-on is running."""
    if not S.running:
        return
    try:
        if not _prefs().hud:
            return
        region = bpy.context.region
        if region is None:
            return
        scale = bpy.context.preferences.system.ui_scale
        names = {k: n for k, n, _d in TARGETS}
        parts = [(names[S.target].upper(), (1.0, 1.0, 1.0, 0.95))]
        if S.paused:
            parts.append(("PAUSED", (1.0, 0.78, 0.2, 1.0)))
        if S.rec:
            parts.append(("REC", (1.0, 0.28, 0.28, 1.0)))
        if S.gyro_on:
            parts.append(("GYRO", (0.55, 0.85, 1.0, 1.0)))
        if S.orbit:
            parts.append(("ORBIT: " + _pivot_name(), (0.6, 1.0, 0.7, 1.0)))
        if S.target == 'CUSTOM':
            m = _active_map()
            if m is not None:
                v = _map_value_text(m)
                parts.append(("%s  %s" % (m.name, v if v is not None else "missing"),
                              (1.0, 0.85, 0.45, 1.0)))

        font = 0
        blf.size(font, 12 * scale)
        padx, h, gap = 10 * scale, 22 * scale, 6 * scale
        widths = [blf.dimensions(font, t)[0] + 2 * padx for t, _c in parts]
        x = (region.width - sum(widths) - gap * (len(parts) - 1)) / 2
        y = region.height - 78 * scale
        th = blf.dimensions(font, "AG")[1]
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        for (text, color), w in zip(parts, widths):   # one rounded chip per status
            _rrect(shader, x, y, x + w, y + h, h / 2, (0.05, 0.05, 0.06, 0.72))
            _rrect(shader, x + 1, y + 1, x + w - 1, y + h - 1, h / 2 - 1,
                   (color[0], color[1], color[2], 0.16))
            _hud_text(font, x + padx, y + (h - th) / 2, text, color)
            x += w + gap

        if S.msg:                       # last message in a wider chip under the status line
            blf.size(font, 11 * scale)
            mw = blf.dimensions(font, S.msg)[0] + 2 * padx
            mx, my = (region.width - mw) / 2, y - h - 6 * scale
            _rrect(shader, mx, my, mx + mw, my + h, h / 2, (0.05, 0.05, 0.06, 0.65))
            _hud_text(font, mx + padx, my + (h - blf.dimensions(font, "AG")[1]) / 2, S.msg,
                      (0.92, 0.92, 0.94, 0.95))
        gpu.state.blend_set('NONE')
    except Exception:
        pass


class DS_TriggerMap(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    path: bpy.props.StringProperty(name="Data Path")
    prop: bpy.props.StringProperty(name="Property")
    index: IntProperty(name="Index", default=-1)
    mode: EnumProperty(
        name="Mode", default='REL',
        items=[('REL', "Relative", "R2 raises, L2 lowers: pressure = speed; the value stays "
                                   "where you leave it"),
               ('ABS', "Absolute", "Released = Rest, R2 fully pressed = Max, "
                                   "L2 fully pressed = Min")])
    vmin: FloatProperty(name="Min")
    vmax: FloatProperty(name="Max")
    rest: FloatProperty(name="Rest", description="Absolute mode: value with the triggers released")
    speed: FloatProperty(name="Speed", min=0.0,
                         description="Relative mode: change per second at full press")
    invert: BoolProperty(name="Invert", description="Swap L2 and R2")
    record: BoolProperty(name="Record Keyframes", default=True,
                         description="Insert keyframes while recording (Circle)")


class DS_UL_maps(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        val = _map_value_text(item)
        row = layout.row(align=True)
        row.label(text=item.name or "Parameter", icon='DRIVER' if val is not None else 'ERROR')
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.active = False
        sub.label(text=("Abs" if item.mode == 'ABS' else "Rel") + "   " + (val or "missing"))


class DS_PT_maps(bpy.types.Panel):
    bl_label = "Trigger Parameters"
    bl_idname = "DS_PT_maps"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DualSense"
    bl_parent_id = "DS_PT_main"

    def draw_header(self, context):
        self.layout.label(text="", icon='DRIVER')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        if not sc.dualsense_maps:
            box = layout.box()
            col = box.column(align=True)
            col.label(text="Right-click any number in Blender", icon='INFO')
            col.label(text="> Assign to DualSense Triggers", icon='BLANK1')
            sub = col.row()
            sub.active = False
            sub.label(text="Then use Custom mode: L2 + R2 drive it", icon='BLANK1')
            layout.operator("dualsense.map_paste", icon='PASTEDOWN')
            return

        row = layout.row()
        row.template_list("DS_UL_maps", "", sc, "dualsense_maps", sc, "dualsense_map_index", rows=3)
        col = row.column(align=True)
        col.operator("dualsense.map_paste", icon='PASTEDOWN', text="")
        col.operator("dualsense.map_remove", icon='REMOVE', text="")

        m = _active_map()
        if m is None:
            return
        res = _map_resolve(m)
        box = layout.box()
        if res is None:
            r = box.row()
            r.alert = True
            r.label(text="This parameter no longer exists", icon='ERROR')
        else:
            hdr = box.row()
            hdr.label(text=m.name, icon='DRIVER')
            hdr.label(text=_map_value_text(m))
        r = box.row(align=True)
        r.prop(m, "mode", expand=True)
        col = box.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(m, "vmin")
        col.prop(m, "vmax")
        if m.mode == 'ABS':
            rr = col.row(align=True)
            rr.prop(m, "rest")
            rr.operator("dualsense.map_rest", text="", icon='EYEDROPPER')
        else:
            col.prop(m, "speed")
        col = box.column(align=True)
        col.prop(m, "invert")
        col.prop(m, "record")
        p = box.row()
        p.active = False
        p.label(text=m.path.replace("bpy.data.", "") + "." + m.prop
                + ("[%d]" % m.index if m.index >= 0 else ""))
        if S.target != 'CUSTOM':
            r = layout.row()
            r.scale_y = 1.2
            r.operator("dualsense.set_target", text="Switch to Custom Mode",
                       icon='DRIVER').target = 'CUSTOM'


def _button_context_menu(self, context):
    if DS_OT_map_assign.poll(context):
        self.layout.separator()
        self.layout.operator(DS_OT_map_assign.bl_idname, icon='DRIVER')


class DS_PivotSlot(bpy.types.PropertyGroup):
    obj: PointerProperty(type=bpy.types.Object, name="Object",
                         description="Empty or object to rotate around")


class DS_UL_pivots(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        on = index == data.dualsense_pivot_index
        layout.label(text="", icon='RADIOBUT_ON' if on else 'RADIOBUT_OFF')
        layout.prop(item, "obj", text="")


class DS_OT_pivot_add(bpy.types.Operator):
    bl_idname = "dualsense.pivot_add"
    bl_label = "Add slot"
    bl_description = "Adds a slot (with the active object, if any)"

    def execute(self, context):
        sc = context.scene
        slot = sc.dualsense_pivots.add()
        slot.obj = context.view_layer.objects.active
        sc.dualsense_pivot_index = len(sc.dualsense_pivots) - 1
        return {'FINISHED'}


class DS_OT_pivot_remove(bpy.types.Operator):
    bl_idname = "dualsense.pivot_remove"
    bl_label = "Remove slot"
    bl_description = "Removes the selected slot"

    def execute(self, context):
        sc = context.scene
        i = sc.dualsense_pivot_index
        if 0 <= i < len(sc.dualsense_pivots):
            sc.dualsense_pivots.remove(i)
            sc.dualsense_pivot_index = min(i, len(sc.dualsense_pivots) - 1)
        return {'FINISHED'}


class DS_OT_pivot_cursor(bpy.types.Operator):
    bl_idname = "dualsense.pivot_cursor"
    bl_label = "Use the 3D cursor"
    bl_description = "No active slot: rotation is around the 3D cursor"

    def execute(self, context):
        context.scene.dualsense_pivot_index = -1
        return {'FINISHED'}


class DS_PT_pivot(bpy.types.Panel):
    bl_label = "Pivot"
    bl_idname = "DS_PT_pivot"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "DualSense"
    bl_parent_id = "DS_PT_main"

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        row = layout.row()
        row.template_list("DS_UL_pivots", "", sc, "dualsense_pivots",
                          sc, "dualsense_pivot_index", rows=3)
        col = row.column(align=True)
        col.operator("dualsense.pivot_add", icon='ADD', text="")
        col.operator("dualsense.pivot_remove", icon='REMOVE', text="")
        i = sc.dualsense_pivot_index
        o = sc.dualsense_pivots[i].obj if 0 <= i < len(sc.dualsense_pivots) else None
        layout.operator("dualsense.pivot_cursor", icon='PIVOT_CURSOR',
                        text="Pivot: " + (o.name if o else "3D cursor"),
                        depress=(o is None))


CLASSES = (
    DS_TriggerMap,
    DS_UL_maps,
    DS_OT_map_assign,
    DS_OT_map_paste,
    DS_OT_map_remove,
    DS_OT_map_rest,
    DS_PivotSlot,
    DS_UL_pivots,
    DS_OT_pivot_add,
    DS_OT_pivot_remove,
    DS_OT_pivot_cursor,
    DS_Preferences,
    DS_OT_install_pygame,
    DS_OT_toggle,
    DS_OT_set_target,
    DS_OT_action,
    DS_PT_main,
    DS_PT_pivot,
    DS_PT_maps,
    DS_PT_commands,
    DS_PT_settings,
)


def _autostart():
    try:
        if not S.running and _prefs().autostart and _pygame_available():
            _start(paused=True)           # opens paused: nothing moves until you resume
            _redraw_ui()
    except Exception as e:
        S.error = "Start failed: %s" % e
        _stop()
    return None


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    S.hud_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_hud, (), 'WINDOW', 'POST_PIXEL')
    bpy.types.Scene.dualsense_pivots = CollectionProperty(type=DS_PivotSlot)
    bpy.types.Scene.dualsense_pivot_index = IntProperty(default=-1, min=-1)
    bpy.types.Scene.dualsense_maps = CollectionProperty(type=DS_TriggerMap)
    bpy.types.Scene.dualsense_map_index = IntProperty(default=-1, min=-1)
    menu = getattr(bpy.types, "UI_MT_button_context_menu", None)
    if menu is not None:
        menu.append(_button_context_menu)
    bpy.app.timers.register(_autostart, first_interval=1.5, persistent=True)


def unregister():
    for fn in (_autostart, _install_poll):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    _stop()
    if S.hud_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(S.hud_handle, 'WINDOW')
        S.hud_handle = None
    menu = getattr(bpy.types, "UI_MT_button_context_menu", None)
    if menu is not None:
        menu.remove(_button_context_menu)
    del bpy.types.Scene.dualsense_pivots
    del bpy.types.Scene.dualsense_pivot_index
    del bpy.types.Scene.dualsense_maps
    del bpy.types.Scene.dualsense_map_index
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
