"""Shared database download / cache logic for materials, lights and cameras."""

import bpy
import json
import os
import threading
import urllib.request

USER_AGENT = "Blender-PhysicallyBased"


def extract(data):
    """Return the list of records from an API response, or None if it isn't valid.
    Handles both a plain list and a wrapper like {"$schema", "header", "data": [...]}."""
    if isinstance(data, dict):
        data = data.get("data") if isinstance(data.get("data"), list) else \
            next((v for v in data.values() if isinstance(v, list)), [])
    return data if isinstance(data, list) and data and isinstance(data[0], dict) else None


def redraw_all():
    wm = bpy.context.window_manager
    if wm:
        for win in wm.windows:
            for area in win.screen.areas:
                area.tag_redraw()


class Database:
    """One physicallybased.info database (materials, lights or cameras).

    Downloads in a background thread, caches to disk, falls back to the cache when
    offline, and calls `on_ready()` on the main thread when new data is available."""

    def __init__(self, filename, urls, label, on_ready):
        self.filename = filename
        self.urls = urls
        self.label = label            # e.g. "materials", used in status messages
        self.on_ready = on_ready
        self.items = []               # raw records currently in use
        self._path = None
        self.state = {"busy": False, "done": False, "msg": "", "data": None}

        # timers need the same function object to register / unregister
        def _poll():
            redraw_all()              # refresh the status message
            if not self.state["done"] or bpy.context.window_manager is None:
                return 0.3
            if self.state["data"]:
                self.items, self.state["data"] = self.state["data"], None
            self.on_ready()
            redraw_all()
            return None
        self._poll = _poll

    @property
    def busy(self):
        return self.state["busy"]

    @property
    def msg(self):
        return self.state["msg"]

    def cache_path(self):
        # computed on the main thread and stored: the download runs in a thread
        if self._path is None:
            try:   # the extension's user folder (Blender 4.2+)
                d = bpy.utils.extension_path_user(__package__, create=True)
            except Exception:
                d = bpy.utils.user_resource('CONFIG', path="physically_based", create=True)
            self._path = os.path.join(d, self.filename)
        return self._path

    def _read_cache(self):
        try:
            with open(self.cache_path(), encoding="utf-8") as f:
                return extract(json.load(f))
        except (OSError, ValueError):
            return None

    def _download(self):
        """Download and cache. Returns (data, error)."""
        err = "no source reachable"
        for url in self.urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = extract(json.loads(r.read().decode("utf-8")))
                if not data:
                    err = f"invalid response from {url}"
                    continue          # never overwrite the cache with empty data
                path = self.cache_path()
                tmp = path + ".tmp"   # atomic write: no corrupted cache
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp, path)
                return data, None
            except Exception as e:
                err = str(e)
        return None, err

    def _worker(self):
        data = None
        if getattr(bpy.app, "online_access", True):
            data, err = self._download()
            if data:
                msg = f"{len(data)} {self.label} updated"
            else:
                data = self._read_cache()
                msg = "Offline: using the cache" if data else f"Download failed: {err}"
        else:
            data = self._read_cache()
            msg = "Online access is disabled: using the cache" if data \
                else "Online access is disabled and there is no cache"
        # the swap into self.items happens on the main thread
        self.state.update(data=data, msg=msg, done=True, busy=False)

    def start_update(self):
        if self.state["busy"]:
            return
        self.cache_path()
        self.state.update(busy=True, done=False, msg="Updating…")
        threading.Thread(target=self._worker, daemon=True).start()
        self.schedule()

    def load_cache(self):
        """Use the cached copy without going online (when auto-update is off)."""
        data = self._read_cache()
        msg = "Using the cache" if data else "No cache yet: click Update Database"
        self.state.update(data=data, msg=msg, done=True, busy=False)
        self.schedule(0.1)

    def schedule(self, delay=0.3):
        if not bpy.app.timers.is_registered(self._poll):
            bpy.app.timers.register(self._poll, first_interval=delay)

    def reapply(self):
        """Rebuild the UI from data already loaded (e.g. after opening a .blend)."""
        if self.items and not self.state["busy"]:
            self.state["done"] = True
            self.schedule(0.1)

    def stop(self):
        if bpy.app.timers.is_registered(self._poll):
            bpy.app.timers.unregister(self._poll)


# ---------------------------------------------------------------- shared UI pieces

_PROBLEM = ("Offline", "Download failed", "Online access", "No cache")


def ui_refresh(layout, db, op_idname):
    """Small refresh button for a panel header (shows a clock while downloading)."""
    row = layout.row(align=True)
    row.enabled = not db.busy
    row.operator(op_idname, text="", icon='TIME' if db.busy else 'FILE_REFRESH', emboss=False)


def ui_status(layout, db):
    """One discreet status line; problems stand out, success stays quiet."""
    msg = db.msg
    if not msg:
        return
    row = layout.row()
    if db.busy:
        row.label(text=msg, icon='TIME')
    elif not db.items:
        row.alert = True
        row.label(text=msg, icon='ERROR')
    elif msg.startswith(_PROBLEM):
        row.label(text=msg, icon='INFO')
    else:
        row.active = False
        row.label(text=msg, icon='CHECKMARK')


def ui_card(layout, title, icon):
    box = layout.box()
    box.label(text=title, icon=icon)
    return box.column(align=True)


def ui_kv(layout, key, value):
    """Aligned 'key   value' row, key dimmed."""
    split = layout.split(factor=0.42, align=True)
    k = split.row()
    k.alignment = 'RIGHT'
    k.active = False
    k.label(text=key)
    split.label(text=value)
