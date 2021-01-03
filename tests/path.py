import taichi as ti
import numpy as np
import taichi_inject
import tina

ti.init(ti.gpu)

scene = tina.PTScene(smoothing=True, texturing=True)
#scene.load_gltf('assets/sphere.gltf')

material = tina.Phong(shineness=32.0, diffuse=[1, 0, 0])
mesh = tina.MeshModel('assets/sphere.obj')
scene.add_object(mesh, material)

if 0 and isinstance(scene, tina.PTScene):
    scene.lighting.set_lights(np.array([
        [0, 3, 0],
    ], dtype=np.float32))
    scene.lighting.set_light_radii(np.array([
        0.25,
    ], dtype=np.float32))
    scene.lighting.set_light_colors(np.array([
        [12.0, 12.0, 12.0],
    ], dtype=np.float32))

if isinstance(scene, tina.PTScene):
    scene.update()

gui = ti.GUI('path', scene.res)

while gui.running:
    scene.input(gui)
    if gui.frame <= 4000:
        scene.render(nsteps=5)
    gui.set_image(scene.img)
    gui.show()
