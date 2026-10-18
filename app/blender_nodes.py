# Runs inside Blender: builds a compositing graph from the website (see compositing.py)
# as real compositor nodes behind an Image node holding the rendered frame.
# Used by blender_composite.py (full size, every frame) and blender_preview.py.
import math
import os
import sys

import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compositing  # noqa: E402


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
    if isinstance(current, str):
        return value
    try:
        size = len(current)
    except TypeError:
        return value
    if not isinstance(value, (list, tuple)):
        return [value] * size
    return (list(value) + [1.0] * size)[:size]


def set_value(node, p, value, scale):
    if p["kind"] == "angle":
        value = math.radians(value)
    if p.get("pixels"):
        value = int(round(value * scale)) if p["kind"] == "int" else value * scale
    for spec in p["sockets"]:
        spec, _, part = spec.partition("#")
        name, _, want = spec.partition(":")
        sock = find_socket(node.inputs, [name])
        if sock is None or (want and sock.type != want):
            continue
        if sock.type == "MENU":  # 5.x menus take the display name
            value = next(c[1] for c in p["choices"] if c[0] == value)
        if part:
            vec = list(sock.default_value)
            vec[int(part)] = value
            sock.default_value = vec
        else:
            sock.default_value = fit(sock.default_value, value)
        return True
    done = False
    for prop in p["props"]:
        if hasattr(node, prop):
            setattr(node, prop, fit(getattr(node, prop), value))
            done = True
    return done


def new_node(tree, idnames):
    """The first node type this Blender has (e.g. Mix RGB became ShaderNodeMix in 5.x)."""
    for idname in idnames:
        if hasattr(bpy.types, idname):
            try:
                return tree.nodes.new(idname)
            except RuntimeError:
                continue
    return None


class Built:
    """A website node once it is in Blender: where its inputs and outputs are."""
    def __init__(self, inputs=None, outputs=None):
        self.inputs = inputs or {}
        self.outputs = outputs or {}


def add_node(tree, spec, values, scale, label=""):
    node = new_node(tree, spec["node"])
    if node is None:
        log("%s: this Blender doesn't have that node, skipped" % spec["label"])
        return None
    node.label = label
    for attr, value in spec.get("setup", {}).items():
        if hasattr(node, attr):
            setattr(node, attr, value)
    todo = compositing.values_of(spec)
    if spec["id"] == "flip" and not hasattr(node, "axis"):
        node.inputs["Flip X"].default_value = "X" in values["axis"]  # 5.x: two checkboxes, no axis menu
        node.inputs["Flip Y"].default_value = "Y" in values["axis"]
        todo = []
    elif spec["id"] in ("rgb", "value"):  # the value lives on the output socket
        key = "color" if spec["id"] == "rgb" else "value"
        node.outputs[0].default_value = fit(node.outputs[0].default_value, values[key])
        todo = []
    for p in todo:
        try:
            ok = set_value(node, p, values[p["id"]], scale)
        except (TypeError, ValueError, StopIteration) as e:
            ok = False
            log("%s: %s = %r not accepted (%s)" % (spec["label"], p["label"], values[p["id"]], e))
        if not ok and (p["sockets"] or p["props"]):
            log("%s: %s is not available in this Blender, left at default" % (spec["label"], p["label"]))
    return Built({i["id"]: find_socket(node.inputs, i["sockets"]) for i in spec["inputs"]},
                 {o["id"]: find_socket(node.outputs, o["sockets"]) for o in spec["outputs"]}), node


def add_vignette(tree, values, scale, width, height):
    """Darken the corners: Ellipse Mask -> Blur -> Multiply over the image."""
    mask, _ = add_node(tree, compositing.BY_ID["ellipse_mask"], {
        "mask": 0.0, "value": 1.0, "operation": "ADD", "x": 0.5, "y": 0.5,
        "width": values["size"], "height": values["size"], "rotation": 0}, 1)
    blur, _ = add_node(tree, compositing.BY_ID["blur"], {
        "type": "GAUSS", "size": int(values["softness"] * max(width, height) * 0.5)}, 1)
    mix, _ = add_node(tree, compositing.BY_ID["mix"], {
        "blend": "MULTIPLY", "factor": values["amount"], "a": [1, 1, 1], "b": [1, 1, 1]}, 1)
    tree.links.new(mask.outputs["mask"], blur.inputs["image"])
    tree.links.new(blur.outputs["image"], mix.inputs["b"])
    return Built({"image": mix.inputs["a"]}, {"image": mix.outputs["image"]})


def passthrough(tree):
    """A muted node that Blender can't mute by itself (Vignette): a reroute."""
    r = tree.nodes.new("NodeReroute")
    return Built({"image": r.inputs[0]}, {"image": r.outputs[0]})


# ---- Scene and tree ------------------------------------------------------------------
def setup_scene(scene, info, fmt):
    """Same look and file settings as the original render."""
    render = scene.render
    render.use_compositing = True
    if hasattr(render, "compositor_device"):
        render.compositor_device = "CPU"  # the GPU compositor needs a display
    for target, values in ((scene.display_settings, info.get("display", {})), (scene.view_settings, info.get("view", {}))):
        for key, value in values.items():
            try:
                setattr(target, key, value)
            except (AttributeError, TypeError):
                log("Color management %s = %r not available here" % (key, value))
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


def new_tree(scene):
    """An empty compositor tree; returns (tree, the socket whose image is saved)."""
    if hasattr(scene, "compositing_node_group"):  # Blender 5+
        old = scene.compositing_node_group
        tree = bpy.data.node_groups.new("Website compositing", "CompositorNodeTree")
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        scene.compositing_node_group = tree
        if old:
            bpy.data.node_groups.remove(old)
        return tree, tree.nodes.new("NodeGroupOutput").inputs[0]
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()  # also removes Render Layers, so Blender doesn't render the scene
    return tree, tree.nodes.new("CompositorNodeComposite").inputs["Image"]


def build(scene, graph, img, scale=1.0, preview=False):
    """Build graph with img as the Render Result. scale shrinks pixel sizes for a smaller
    preview image. With preview, a plugged-in Viewer is shown instead of the Composite."""
    tree, out = new_tree(scene)
    width, height = img.size[0], img.size[1]
    built, used = {}, []
    for n in graph["nodes"]:
        spec = compositing.BY_ID[n["type"]]
        if spec["id"] == "render":
            src = tree.nodes.new("CompositorNodeImage")
            src.image = img
            built[n["id"]] = Built(outputs={"image": src.outputs["Image"], "alpha": src.outputs["Alpha"]})
        elif spec["id"] in ("composite", "viewer"):
            built[n["id"]] = Built()
        elif spec["id"] == "vignette":
            built[n["id"]] = passthrough(tree) if n["mute"] else add_vignette(tree, n["values"], scale, width, height)
        else:
            made = add_node(tree, spec, n["values"], scale, label=n["id"])
            if made:
                built[n["id"]], node = made
                node.mute = n["mute"]
                used.append(spec["label"] + (" (muted)" if n["mute"] else ""))

    result = {}
    for link in graph["links"]:
        (src, out_id), (dst, in_id) = link["from"], link["to"]
        if src not in built or dst not in built:
            continue
        sock = built[src].outputs.get(out_id)
        if sock is None:
            continue
        kind = compositing.BY_ID[next(n["type"] for n in graph["nodes"] if n["id"] == dst)]["id"]
        if kind in ("composite", "viewer"):
            result[kind] = sock
        elif built[dst].inputs.get(in_id) is not None:
            tree.links.new(sock, built[dst].inputs[in_id])
    final = result.get("viewer") if preview and "viewer" in result else result.get("composite")
    if final is not None:
        tree.links.new(final, out)
    return used, final is not None
