"""Physically Based: materials, lights and cameras from physicallybased.info."""

import bpy

from . import materials, lights, cameras

MODULES = (materials, lights, cameras)
NAMES = ("Materials", "Lights", "Cameras")
ICONS = ('MATERIAL', 'LIGHT', 'CAMERA_DATA')


class PB_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_update: bpy.props.BoolProperty(
        name="Update on Startup", default=True,
        description="Download the latest databases when Blender starts. "
                    "When off, the cached copies are used until you update by hand")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Databases", icon='WORLD_DATA')
        col = box.column(align=True)
        for name, icon, m in zip(NAMES, ICONS, MODULES):
            split = col.split(factor=0.3, align=True)
            split.label(text=name, icon=icon)
            n = len(m.DB.items)
            right = split.row()
            right.active = not m.DB.busy
            right.label(text=m.DB.msg or (f"{n} loaded" if n else "Not loaded"),
                        icon='TIME' if m.DB.busy else ('CHECKMARK' if n else 'ERROR'))
        row = box.row()
        row.scale_y = 1.3
        row.enabled = not any(m.DB.busy for m in MODULES)
        row.operator("pb.fetch_all", icon='FILE_REFRESH')
        box.prop(self, "auto_update")

        box = layout.box()
        box.label(text="Where to find it", icon='INFO')
        col = box.column(align=True)
        col.active = False
        col.label(text="Materials: Shader Editor sidebar > PhysBased")
        col.label(text="Lights & Cameras: 3D Viewport sidebar > PhysBased, and their Properties tabs")
        col.label(text="Colors are in ACEScg")


class PB_OT_fetch_all(bpy.types.Operator):
    bl_idname = "pb.fetch_all"
    bl_label = "Update All Databases"
    bl_description = "Download materials, lights and cameras from physicallybased.info"

    def execute(self, context):
        for m in MODULES:
            m.DB.start_update()
        return {'FINISHED'}


def _autoload():
    if bpy.app.background:
        return None
    try:
        auto = bpy.context.preferences.addons[__package__].preferences.auto_update
    except Exception:
        auto = True
    for m in MODULES:
        if auto:
            m.DB.start_update()
        else:
            m.DB.load_cache()
    return None


def register():
    bpy.utils.register_class(PB_Preferences)
    bpy.utils.register_class(PB_OT_fetch_all)
    for m in MODULES:
        m.register()
    bpy.app.timers.register(_autoload, first_interval=0.5)


def unregister():
    if bpy.app.timers.is_registered(_autoload):
        bpy.app.timers.unregister(_autoload)
    for m in reversed(MODULES):
        m.unregister()
    bpy.utils.unregister_class(PB_OT_fetch_all)
    bpy.utils.unregister_class(PB_Preferences)
