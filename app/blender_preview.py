# Runs inside Blender and stays running (see preview.py), so the Compositor page can
# show the effect of every change within a moment:
#   blender -b --factory-startup -P blender_preview.py
# Reads one JSON request per line on stdin, composites a (shrunk) copy of the frame
# with the graph and saves a PNG, then answers with a line starting "@@preview".
import array
import json
import os
import sys
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_nodes  # noqa: E402

scene = bpy.context.scene
cache = {}  # (path, scale) -> image


def source(path, scale):
    """The frame, shrunk to scale (1 = full size); kept for the next requests."""
    key = (path, scale)
    if key not in cache:
        if len(cache) >= 6:
            bpy.data.images.remove(cache.pop(next(iter(cache))))
        img = bpy.data.images.load(path, check_existing=False)
        if scale < 1.0:
            img = shrink(img, max(1, round(img.size[0] * scale)), max(1, round(img.size[1] * scale)))
        cache[key] = img
    return cache[key]


def shrink(src, w, h):
    """A w x h copy of src. It has to be a new image: the compositor places a file image that
    was only scaled in memory at its size on disk, so the picture comes out shifted."""
    src.scale(w, h)
    img = bpy.data.images.new("preview", w, h, alpha=True, float_buffer=True)
    img.colorspace_settings.name = src.colorspace_settings.name
    img.alpha_mode = src.alpha_mode
    px = array.array("f", [0.0]) * (w * h * 4)
    src.pixels.foreach_get(px)
    img.pixels.foreach_set(px)
    bpy.data.images.remove(src)
    return img


def answer(req_id, text):
    print("@@preview %s %s" % (req_id, text), flush=True)


print("@@preview ready", flush=True)
while True:
    line = sys.stdin.readline()
    if not line:
        break  # the server went away
    req_id = "?"
    try:
        req = json.loads(line)
        req_id = req["id"]
        started = time.time()
        scale = min(1.0, max(0.05, float(req.get("scale", 1.0))))
        img = source(req["raw"], scale)
        render = scene.render
        render.resolution_x, render.resolution_y, render.resolution_percentage = img.size[0], img.size[1], 100
        info = dict(req["info"], image={"color_mode": "RGBA", "color_depth": "8"})
        blender_nodes.setup_scene(scene, info, "PNG")
        _, connected = blender_nodes.build(scene, req["graph"], img, scale=scale, preview=True)
        if not connected:
            answer(req_id, "empty")
            continue
        render.filepath = req["out"]
        bpy.ops.render.render(write_still=True)
        answer(req_id, "ok %d %d %.3f" % (img.size[0], img.size[1], time.time() - started))
    except Exception as e:  # keep serving the next request
        answer(req_id, "error " + str(e).replace("\n", " ")[:300])
