# Runs inside Blender after the render, with no .blend loaded:
#   blender -b --factory-startup -P blender_composite.py -- <options.json> <info.json> <raw dir> <output dir>
# Builds the compositing graph made on the website (see blender_nodes.py) behind an
# Image node, runs it over every raw EXR frame and saves the results. Nothing is
# rendered here, so changing the compositing later takes seconds, not a re-render.
import json
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_nodes  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
opts = json.load(open(argv[0]))
info = json.load(open(argv[1]))
raw_dir, out_dir = argv[2], argv[3]

scene = bpy.context.scene
render = scene.render
frames = sorted(f for f in os.listdir(raw_dir) if f.lower().endswith(".exr"))
if not frames:
    raise SystemExit("No raw frames to composite in " + raw_dir)

img = bpy.data.images.load(os.path.join(raw_dir, frames[0]))
render.resolution_x, render.resolution_y, render.resolution_percentage = img.size[0], img.size[1], 100
blender_nodes.setup_scene(scene, info, opts.get("format", "PNG"))
used, connected = blender_nodes.build(scene, opts["compositing"]["graph"], img)
blender_nodes.log("Nodes: %s" % (", ".join(used) or "none"))
if not connected:
    blender_nodes.log("Nothing is plugged into the Composite node, so the frames will be empty")

for name in frames:
    img.filepath = os.path.join(raw_dir, name)
    img.reload()
    render.filepath = os.path.join(out_dir, os.path.splitext(name)[0])
    bpy.ops.render.render(write_still=True)
