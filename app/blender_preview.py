# Runs inside Blender and stays running (see preview.py), so the Compositor page can
# show the effect of every change within a moment:
#   blender -b --factory-startup -P blender_preview.py
# Reads one JSON request per line on stdin, composites a shrunk copy of the frame
# with the graph and saves a PNG, then answers with a line starting "@@preview".
import json
import os
import sys
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blender_nodes  # noqa: E402

scene = bpy.context.scene
cache = {}  # (path, max size) -> (image, scale)


def source(path, max_size):
    """The frame, shrunk to max_size on its long side; kept for the next requests."""
    key = (path, max_size)
    if key not in cache:
        if len(cache) >= 6:
            img, _ = cache.pop(next(iter(cache)))
            bpy.data.images.remove(img)
        img = bpy.data.images.load(path, check_existing=False)
        w, h = img.size[0], img.size[1]
        scale = min(1.0, max_size / max(w, h)) if max_size else 1.0
        if scale < 1.0:
            img.scale(max(1, round(w * scale)), max(1, round(h * scale)))
        cache[key] = (img, scale)
    return cache[key]


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
        img, scale = source(req["raw"], req.get("max_size", 0))
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
