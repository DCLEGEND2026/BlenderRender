# Runs inside Blender after the render, with no .blend loaded:
#   blender -b --factory-startup -P blender_composite.py -- <options.json> <info.json> <raw dir> <output dir>
# Builds the compositing nodes picked on the website (see compositing.py) behind an
# Image node, runs them over every raw EXR frame and saves the results. Nothing is
# rendered here, so changing the compositing later takes seconds, not a re-render.
import json
import math
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compositing  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
opts = json.load(open(argv[0]))
info = json.load(open(argv[1]))
raw_dir, out_dir = argv[2], argv[3]

scene = bpy.context.scene
render = scene.render


def log(msg):
    print("[composite] " + msg, flush=True)


# ---- Sockets and properties that moved between Blender versions ------------------
def find_socket(sockets, names):
    """By identifier first (unique, e.g. "Color Gamma"), then by visible name."""
    for name in names:
        for s in sockets:
            if s.identifier == name:
                return s
        for s in sockets:
            if s.enabled and s.name == name:
                return s
    return None


def fit(current, value):
    """Shape value like the socket/property it goes into (scalar -> vector, RGB -> RGBA)."""
    try:
        size = len(current)
    except TypeError:
        return value
    if isinstance(current, str):
        return value
    if not isinstance(value, (list, tuple)):
        return [value] * size
    return (list(value) + [1.0] * size)[:size]


def set_param(node, p, value):
    if p["kind"] == "angle":
        value = math.radians(value)
    for spec in p["sockets"]:
        name, _, want = spec.partition(":")
        sock = find_socket(node.inputs, [name])
        if sock is None or (want and sock.type != want):
            continue
        if sock.type == "MENU":  # 5.x menus take the display name
            value = next(c[1] for c in p["choices"] if c[0] == value)
        sock.default_value = fit(sock.default_value, value)
        return True
    done = False
    for prop in p["props"]:
        if hasattr(node, prop):
            setattr(node, prop, fit(getattr(node, prop), value))
            done = True
    return done


def new_node(tree, idnames):
    """The first node type this Blender has (the compositor's Mix RGB became ShaderNodeMix in 5.x)."""
    for idname in idnames:
        if hasattr(bpy.types, idname):
            try:
                return tree.nodes.new(idname)
            except RuntimeError:
                continue
    return None


def add_node(tree, spec, params, sock):
    """Add one node from the catalogue after sock; returns its image output (or sock if skipped)."""
    node = new_node(tree, spec["node"])
    if node is None:
        log("%s: this Blender doesn't have that node, skipped" % spec["label"])
        return sock
    for attr, value in spec.get("setup", {}).items():
        if hasattr(node, attr):
            setattr(node, attr, value)
    params_left = spec["params"]
    if spec["id"] == "flip" and not hasattr(node, "axis"):
        axis = params["axis"]  # 5.x has two checkboxes instead of an axis menu
        node.inputs["Flip X"].default_value = "X" in axis
        node.inputs["Flip Y"].default_value = "Y" in axis
        params_left = []
    for p in params_left:
        try:
            ok = set_param(node, p, params[p["id"]])
        except (TypeError, ValueError, StopIteration) as e:
            ok = False
            log("%s: %s = %r not accepted (%s)" % (spec["label"], p["label"], params[p["id"]], e))
        if not ok and (p["sockets"] or p["props"]):
            log("%s: %s is not available in this Blender, left at default" % (spec["label"], p["label"]))
    tree.links.new(sock, find_socket(node.inputs, spec.get("input", ["Image"])))
    return find_socket(node.outputs, spec.get("output", ["Image"]))


def add_vignette(tree, params, sock, width, height):
    """Darken the corners: Ellipse Mask -> Blur -> Multiply over the image."""
    mask = tree.nodes.new("CompositorNodeEllipseMask")
    size = params["size"]
    if "Size" in mask.inputs:
        mask.inputs["Size"].default_value = (size, size)
    else:
        mask.mask_width = mask.mask_height = size
    blur_px = int(params["softness"] * max(width, height) * 0.5)
    soft = add_node(tree, compositing.BY_ID["blur"], {"type": "GAUSS", "size": blur_px}, mask.outputs["Mask"])
    mix = add_node(tree, compositing.BY_ID["mix"], {"blend": "MULTIPLY", "color": [1, 1, 1], "factor": params["amount"]}, sock)
    tree.links.new(soft, find_socket(mix.node.inputs, ["B_Color", "Image_001"]))
    return mix


# ---- Scene: same look and file settings as the original render --------------------
frames = sorted(f for f in os.listdir(raw_dir) if f.lower().endswith(".exr"))
if not frames:
    raise SystemExit("No raw frames to composite in " + raw_dir)
img = bpy.data.images.load(os.path.join(raw_dir, frames[0]))
width, height = img.size[0], img.size[1]
render.resolution_x, render.resolution_y, render.resolution_percentage = width, height, 100
render.use_compositing = True
if hasattr(render, "compositor_device"):
    render.compositor_device = "CPU"  # the GPU compositor needs a display

for target, values in ((scene.display_settings, info.get("display", {})), (scene.view_settings, info.get("view", {}))):
    for key, value in values.items():
        try:
            setattr(target, key, value)
        except (AttributeError, TypeError):
            log("Color management %s = %r not available here" % (key, value))

fmt = opts.get("format", "PNG")
image = render.image_settings
image.file_format = fmt
for key, value in info.get("image", {}).items():
    try:
        setattr(image, key, value)
    except TypeError:
        pass
if fmt == "JPEG":
    image.quality = 92
render.dither_intensity = info.get("dither", render.dither_intensity)
render.use_file_extension = True

# ---- Node tree ---------------------------------------------------------------------
if hasattr(scene, "compositing_node_group"):  # Blender 5+
    tree = bpy.data.node_groups.new("Website compositing", "CompositorNodeTree")
    tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
    scene.compositing_node_group = tree
    out = tree.nodes.new("NodeGroupOutput").inputs[0]
else:
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()  # also removes Render Layers, so Blender doesn't render the scene
    out = tree.nodes.new("CompositorNodeComposite").inputs["Image"]

source = tree.nodes.new("CompositorNodeImage")
source.image = img
sock = source.outputs["Image"]
chain = []
for item in opts.get("compositing", {}).get("nodes", []):
    spec = compositing.BY_ID.get(item["type"])
    if not spec or item.get("mute"):
        continue
    chain.append(spec["label"])
    if spec["id"] == "vignette":
        sock = add_vignette(tree, item["params"], sock, width, height)
    else:
        sock = add_node(tree, spec, item["params"], sock)
tree.links.new(sock, out)
log("Nodes: Image -> %s -> Output" % " -> ".join(chain) if chain else "No nodes, saving the render as it is")

# ---- Run it over every frame -------------------------------------------------------
for name in frames:
    img.filepath = os.path.join(raw_dir, name)
    img.reload()
    render.filepath = os.path.join(out_dir, os.path.splitext(name)[0])
    bpy.ops.render.render(write_still=True)
