# DualSense Controller

Use a PS5 DualSense controller, over USB-C or Bluetooth, to fly the camera and the viewport, move objects and bones, record animation, and drive any Blender value with the adaptive triggers.

## Setup

1. Install the add-on.
2. Open **3D Viewport > Sidebar (N) > DualSense** and click **Install pygame and hidapi**. This happens once and needs internet access.
3. Connect the controller. The add-on starts paused: press **Create** on the controller, or **Resume** in the panel, to take control.

## Modes

Press **Options** on the controller, or use the buttons in the panel, to switch mode.

- **Object:** move, rotate and scale the selected objects, or the active bone in Pose Mode.
- **Camera:** fly the scene camera. L1 / R1 with the triggers change zoom, focus distance and f-stop.
- **Viewport:** fly the 3D viewport.
- **Custom:** L2 and R2 drive any number you assign. Right-click a value anywhere in Blender and choose **Assign to DualSense Triggers**. Both triggers stay at full stiffness for precise control.

## Main controls

| Control | Result |
|---|---|
| Left stick | Move / fly |
| Right stick | Rotate / look around |
| Cross | Play / stop. Double tap: back to the start. Hold: gyroscope on/off |
| Circle | Recording on/off |
| Triangle / Square | Rotation / location keyframe |
| L3 (click) | Orbit around the pivot |
| Touchpad | Swipe to rotate, click to cycle the pivot |
| Create | Pause / resume |
| Options | Change mode |

The full button map is in the **Controls** sub-panel, and every speed and sensitivity is in **Settings**.

## Requirements

Blender 4.3 or newer. The add-on downloads [pygame-ce](https://pypi.org/project/pygame-ce/) and [hidapi](https://pypi.org/project/hidapi/) into Blender's configuration folder on first use.

## Bugs and help

[Open an issue](https://github.com/bussiartifex/Blender_Addons/issues/new/choose) and mention "DualSense Controller". Please say whether the controller is connected over USB or Bluetooth.
