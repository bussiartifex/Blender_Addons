# Phone Gyro

Use your Android phone's gyroscope over Wi-Fi to rotate the camera, an object, a bone or the 3D viewport in Blender, and record the movement as keyframes.

## Setup

1. Install the add-on in Blender.
2. Build and install the companion Android app from the [`android`](android) folder (see below).
3. Connect the phone and the computer to the same Wi-Fi network.

## How to use

1. In Blender go to **3D Viewport > Sidebar (N) > Phone Gyro** and click **Start Listening**.
2. Type the address shown there into the phone app. Blender will show **Live** when it receives the phone.
3. Choose the target: **Camera**, **Viewport**, **Object** or **Bone** (Pose Mode).
4. Choose how you hold the phone: Portrait, Landscape or Flat.

| Control | Result |
|---|---|
| Volume Down on the phone, or **Recenter** | Point the target back to its Home direction |
| **Set Home** | Use the target's current rotation as the new Home |
| Volume Up on the phone, or **Record** | Record rotation keyframes while the animation plays |

If nothing arrives, check that both devices are on the same network and that your firewall allows UDP on the port shown in the panel (9876 by default).

## Android app

The [`android`](android) folder contains the app's source. To build it:

1. In Android Studio, create a new **Empty Views Activity** project in **Kotlin**, with package `com.example.gyro` and minimum SDK 24.
2. Replace `app/src/main/AndroidManifest.xml` and `app/src/main/java/com/example/gyro/MainActivity.kt` with the files from this folder.
3. Run it on your phone.

## Requirements

Blender 4.2 or newer, and an Android phone with a gyroscope.

## Bugs and help

[Open an issue](https://github.com/bussiartifex/Blender_Addons/issues/new/choose) and mention "Phone Gyro".
