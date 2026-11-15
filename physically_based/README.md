# Physically Based

Real-world measured values for materials, lights and cameras from [physicallybased.info](https://physicallybased.info), brought into Blender. Colors are in ACEScg.

## Materials

**Shader Editor > Sidebar (N) > PhysBased**

Search the list and click a material. The add-on adds a node group with one output per value: Base Color, Metallic, Roughness, IOR, complex IOR (n, k) for metals, Transmission, Transmission Color, Dispersion (Abbe number), Density and more. Outputs only appear when the database has that value.

The group is not wired to your shader, so you decide what to connect.

## Lights

**Light Properties > Physically Based**, or **3D Viewport > Sidebar (N) > PhysBased**

Pick a real light source (sun, sky, screens, lamps and bulbs). The add-on converts its photometric units (lux, cd/m², lumens) and color temperature into Blender light settings. Sky presets set up the World background.

## Cameras

**Camera Properties > Physically Based**, or **3D Viewport > Sidebar (N) > PhysBased**

Pick a real camera body. The add-on sets the sensor size and the render resolution from its formats, and keeps the sensor fit correct if you change the resolution by hand.

## Data and network

On startup the add-on downloads the latest databases from physicallybased.info and keeps a copy for offline use. You can turn this off in **Preferences > Add-ons > Physically Based** and update by hand.

## Requirements

Blender 4.2 or newer. Internet access is needed for the first download.

## Bugs and help

[Open an issue](https://github.com/bussiartifex/Blender_Addons/issues/new/choose) and mention "Physically Based".
