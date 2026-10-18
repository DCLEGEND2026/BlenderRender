"""Compositing node graph: the nodes the Compositor page offers, and the check for
the graphs the browser sends.

Every entry is a real Blender compositor node. The page lets you place them, wire
outputs to inputs and change their values, like Blender's node editor; then
blender_nodes.py builds the same graph in Blender. A graph looks like
  {"nodes": [{"id", "type", "x", "y", "mute", "values": {...}}],
   "links": [{"from": [node id, output id], "to": [node id, input id]}]}
It always has one "render" node (the rendered frame) and one "composite" node
(what gets saved), and may have one "viewer" node to preview any point.

Blender moved most node settings from node properties to input sockets between
4.x and 5.x, so each value lists the socket names to try first and the older
property names to fall back to. "Size:VECTOR" only matches a vector socket,
"Position#0" is the first part of a vector socket. "when" shows a setting only
for some values of another setting (Glare's Size only for Bloom and Fog Glow),
and "pixels" values are scaled down with the smaller preview image.

This file is plain Python: it is imported by the server and inside Blender.
"""
import re

MAX_NODES = 64
MODES = ("file", "off", "custom")
RE_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


# ---- Settings shown in a node's body ------------------------------------------------
def num(id, label, default, lo, hi, step=0.01, sockets=None, props=(), kind="float", pixels=False):
    p = {"id": id, "label": label, "kind": kind, "default": default, "min": lo, "max": hi,
         "step": step, "sockets": sockets if sockets is not None else [label], "props": list(props)}
    if pixels:
        p["pixels"] = True
    return p


def whole(id, label, default, lo, hi, sockets=None, props=(), pixels=False):
    return num(id, label, default, lo, hi, 1, sockets, props, kind="int", pixels=pixels)


def angle(id, label, default, sockets, props=()):
    # Degrees on the page, radians in Blender.
    return num(id, label, default, -180, 180, 1, sockets, props, kind="angle")


def flag(id, label, default, sockets=None, props=()):
    return {"id": id, "label": label, "kind": "bool", "default": default,
            "sockets": sockets if sockets is not None else [label], "props": list(props)}


def color(id, label, default, sockets=None, props=()):
    return {"id": id, "label": label, "kind": "color", "default": default,
            "sockets": sockets if sockets is not None else [label], "props": list(props)}


def choice(id, label, default, choices, sockets=None, props=()):
    # choices: (value, name). Blender 4.x enum properties take the value, 5.x menu sockets the name.
    return {"id": id, "label": label, "kind": "choice", "default": default,
            "choices": [list(c) for c in choices],
            "sockets": sockets if sockets is not None else [label], "props": list(props)}


# ---- Sockets that can be wired --------------------------------------------------------
def inp(id, label, type, sockets, value=None):
    """An input socket. value (a setting) is used while nothing is plugged in."""
    if value is not None:
        value = dict(value, id=id, label=label, sockets=sockets)
    return {"id": id, "label": label, "type": type, "sockets": sockets, "value": value}


def out(id, label, type, sockets):
    return {"id": id, "label": label, "type": type, "sockets": sockets}


def factor(default=1.0, sockets=("Fac", "Factor")):
    return inp("factor", "Factor", "float", list(sockets), num("", "", default, 0, 1))


IMAGE_IN = inp("image", "Image", "color", ["Image"])
IMAGE_OUT = out("image", "Image", "color", ["Image"])
COLOR_IN = inp("image", "Color", "color", ["Color"])
COLOR_OUT = out("image", "Color", "color", ["Color"])
MASK_TYPES = [("ADD", "Add"), ("SUBTRACT", "Subtract"), ("MULTIPLY", "Multiply"), ("NOT", "Not")]


def node(id, label, group, blender, inputs=(), outputs=(), params=(), hint="", **extra):
    return dict({"id": id, "label": label, "group": group, "node": list(blender), "inputs": list(inputs),
                 "outputs": list(outputs), "params": list(params), "hint": hint}, **extra)


def mask_node(id, label, blender):
    return node(id, label, "Mask", [blender],
                [inp("mask", "Mask", "float", ["Mask"], num("", "", 0.0, 0, 1)),
                 inp("value", "Value", "float", ["Value"], num("", "", 1.0, 0, 1))],
                [out("mask", "Mask", "float", ["Mask"])],
                [choice("operation", "Operation", "ADD", MASK_TYPES, props=["mask_type"]),
                 num("x", "X", 0.5, -1, 2, sockets=["Position#0"], props=["x"]),
                 num("y", "Y", 0.5, -1, 2, sockets=["Position#1"], props=["y"]),
                 num("width", "Width", 0.4, 0, 2, sockets=["Size#0"], props=["mask_width"]),
                 num("height", "Height", 0.3, 0, 2, sockets=["Size#1"], props=["mask_height"]),
                 angle("rotation", "Rotation", 0, ["Rotation"], ["rotation"])])


NODES = [
    # ---- Input / output ---------------------------------------------------------------
    node("render", "Render Result", "Input", [], outputs=[
        out("image", "Image", "color", []), out("alpha", "Alpha", "float", [])],
        hint="The rendered frame.", fixed=True),
    node("rgb", "RGB", "Input", ["CompositorNodeRGB"], outputs=[out("color", "Color", "color", ["Color", "RGBA"])],
         params=[color("color", "Color", [1.0, 0.6, 0.3], sockets=[])]),
    node("value", "Value", "Input", ["CompositorNodeValue", "ShaderNodeValue"],
         outputs=[out("value", "Value", "float", ["Value"])],
         params=[num("value", "Value", 0.5, -1000, 1000, sockets=[])]),
    node("composite", "Composite", "Output", [], inputs=[inp("image", "Image", "color", [])],
         hint="What gets saved.", fixed=True),
    node("viewer", "Viewer", "Output", [], inputs=[inp("image", "Image", "color", [])],
         hint="While it is plugged in, the preview shows this instead of the Composite.", single=True),

    # ---- Color --------------------------------------------------------------------------
    node("exposure", "Exposure", "Color", ["CompositorNodeExposure"], [IMAGE_IN], [IMAGE_OUT],
         [num("exposure", "Exposure", 0.0, -10, 10, 0.1)]),
    node("bright_contrast", "Brightness/Contrast", "Color", ["CompositorNodeBrightContrast"], [IMAGE_IN], [IMAGE_OUT],
         [num("bright", "Brightness", 0.0, -100, 100, 1, sockets=["Bright", "Brightness"]),
          num("contrast", "Contrast", 0.0, -100, 100, 1)]),
    node("hue_sat", "Hue/Saturation/Value", "Color", ["CompositorNodeHueSat"], [IMAGE_IN, factor()], [IMAGE_OUT],
         [num("hue", "Hue", 0.5, 0, 1), num("saturation", "Saturation", 1.0, 0, 2), num("value", "Value", 1.0, 0, 2)]),
    node("color_balance", "Color Balance", "Color", ["CompositorNodeColorBalance"], [IMAGE_IN, factor()], [IMAGE_OUT],
         [color("lift", "Lift", [1, 1, 1], ["Color Lift"], ["lift"]),
          color("gamma", "Gamma", [1, 1, 1], ["Color Gamma"], ["gamma"]),
          color("gain", "Gain", [1, 1, 1], ["Color Gain"], ["gain"])],
         hint="Lift tints the shadows, Gamma the midtones, Gain the highlights. White = no change."),
    node("color_correction", "Color Correction", "Color", ["CompositorNodeColorCorrection"],
         [IMAGE_IN, inp("mask", "Mask", "float", ["Mask"], num("", "", 1.0, 0, 1))], [IMAGE_OUT],
         [num("saturation", "Saturation", 1.0, 0, 4, sockets=["Master Saturation"], props=["master_saturation"]),
          num("contrast", "Contrast", 1.0, 0, 4, sockets=["Master Contrast"], props=["master_contrast"]),
          num("gamma", "Gamma", 1.0, 0.01, 4, sockets=["Master Gamma"], props=["master_gamma"]),
          num("gain", "Gain", 1.0, 0, 4, sockets=["Master Gain"], props=["master_gain"]),
          num("offset", "Offset", 0.0, -1, 1, sockets=["Master Offset"], props=["master_lift"])]),
    node("mix", "Mix", "Color", ["CompositorNodeMixRGB", "ShaderNodeMix"],
         [factor(0.5, ["Factor_Float", "Fac"]),
          inp("a", "A", "color", ["A_Color", "Image"], color("", "", [0.5, 0.5, 0.5])),
          inp("b", "B", "color", ["B_Color", "Image_001"], color("", "", [0.5, 0.5, 0.5]))],
         [out("image", "Result", "color", ["Result_Color", "Image"])],
         [choice("blend", "Blend", "MIX", [
             ("MIX", "Mix"), ("DARKEN", "Darken"), ("MULTIPLY", "Multiply"), ("BURN", "Color Burn"),
             ("LIGHTEN", "Lighten"), ("SCREEN", "Screen"), ("DODGE", "Color Dodge"), ("ADD", "Add"),
             ("OVERLAY", "Overlay"), ("SOFT_LIGHT", "Soft Light"), ("LINEAR_LIGHT", "Linear Light"),
             ("DIFFERENCE", "Difference"), ("EXCLUSION", "Exclusion"), ("SUBTRACT", "Subtract"),
             ("DIVIDE", "Divide"), ("HUE", "Hue"), ("SATURATION", "Saturation"), ("COLOR", "Color"),
             ("VALUE", "Value")], sockets=[], props=["blend_type"])],
         setup={"data_type": "RGBA"}),
    node("alpha_over", "Alpha Over", "Color", ["CompositorNodeAlphaOver"],
         [factor(1.0, ["Fac"]),
          inp("background", "Background", "color", ["Background", "Image"], color("", "", [1, 1, 1])),
          inp("foreground", "Foreground", "color", ["Foreground", "Image_001"], color("", "", [1, 1, 1]))],
         [IMAGE_OUT], hint="Puts the foreground over the background using its alpha."),
    node("posterize", "Posterize", "Color", ["CompositorNodePosterize"], [IMAGE_IN], [IMAGE_OUT],
         [num("steps", "Steps", 8, 2, 1024, 1)]),
    node("invert", "Invert Color", "Color", ["CompositorNodeInvert"], [COLOR_IN, factor()], [COLOR_OUT]),
    node("rgb_to_bw", "RGB to BW", "Color", ["CompositorNodeRGBToBW"], [IMAGE_IN],
         [out("value", "Val", "float", ["Val"])]),

    # ---- Filter -------------------------------------------------------------------------
    node("glare", "Glare", "Filter", ["CompositorNodeGlare"], [IMAGE_IN],
         [IMAGE_OUT, out("glare", "Glare", "color", ["Glare"])],
         [choice("type", "Type", "BLOOM", [
             ("BLOOM", "Bloom"), ("FOG_GLOW", "Fog Glow"), ("STREAKS", "Streaks"),
             ("GHOSTS", "Ghosts"), ("SIMPLE_STAR", "Simple Star"), ("SUN_BEAMS", "Sun Beams")],
             props=["glare_type"]),
          choice("quality", "Quality", "MEDIUM", [("HIGH", "High"), ("MEDIUM", "Medium"), ("LOW", "Low")],
                 props=["quality"]),
          num("threshold", "Threshold", 1.0, 0, 100, 0.05, sockets=["Highlights Threshold", "Threshold"],
              props=["threshold"]),
          num("strength", "Strength", 1.0, 0, 1),
          dict(num("size", "Size", 0.5, 0, 1), when={"type": ["BLOOM", "FOG_GLOW"]}),
          num("saturation", "Saturation", 1.0, 0, 1),
          color("tint", "Tint", [1, 1, 1]),
          dict(whole("streaks", "Streaks", 4, 1, 16, props=["streaks"]), when={"type": ["STREAKS"]})]),
    node("blur", "Blur", "Filter", ["CompositorNodeBlur"], [IMAGE_IN], [IMAGE_OUT],
         [choice("type", "Type", "GAUSS", [
             ("GAUSS", "Gaussian"), ("FAST_GAUSS", "Fast Gaussian"), ("FLAT", "Flat"),
             ("TENT", "Tent"), ("CUBIC", "Cubic")], props=["filter_type"]),
          whole("size", "Size (px)", 10, 0, 2048, sockets=["Size:VECTOR"], props=["size_x", "size_y"], pixels=True)]),
    node("denoise", "Denoise", "Filter", ["CompositorNodeDenoise"], [IMAGE_IN], [IMAGE_OUT],
         [choice("prefilter", "Prefilter", "ACCURATE", [("NONE", "None"), ("FAST", "Fast"), ("ACCURATE", "Accurate")],
                 props=["prefilter"]),
          flag("hdr", "HDR", True, props=["use_hdr"])],
         hint="OpenImageDenoise, on the CPU. Slow on big images."),
    node("filter", "Filter", "Filter", ["CompositorNodeFilter"], [IMAGE_IN, factor()], [IMAGE_OUT],
         [choice("type", "Type", "SHARPEN", [
             ("SOFTEN", "Soften"), ("SHARPEN", "Box Sharpen"), ("SHARPEN_DIAMOND", "Diamond Sharpen"),
             ("LAPLACE", "Laplace"), ("SOBEL", "Sobel"), ("PREWITT", "Prewitt"),
             ("KIRSCH", "Kirsch"), ("SHADOW", "Shadow")], props=["filter_type"])]),
    node("kuwahara", "Kuwahara", "Filter", ["CompositorNodeKuwahara"], [IMAGE_IN], [IMAGE_OUT],
         [choice("type", "Type", "ANISOTROPIC", [("CLASSIC", "Classic"), ("ANISOTROPIC", "Anisotropic")],
                 props=["variation"]),
          num("size", "Size", 6, 1, 100, 1, pixels=True)],
         hint="Painterly look."),
    node("pixelate", "Pixelate", "Filter", ["CompositorNodePixelate"], [COLOR_IN], [COLOR_OUT],
         [whole("size", "Size (px)", 8, 1, 512, sockets=["Size"], props=["pixel_size"], pixels=True)]),
    node("vignette", "Vignette", "Filter", [], [IMAGE_IN], [IMAGE_OUT],
         [num("amount", "Amount", 0.5, 0, 1, sockets=[]),
          num("size", "Size", 0.9, 0.1, 2, sockets=[]),
          num("softness", "Softness", 0.4, 0, 1, sockets=[])],
         hint="Ellipse Mask, Blur and Multiply nodes in one, to darken the corners."),

    # ---- Mask ---------------------------------------------------------------------------
    mask_node("ellipse_mask", "Ellipse Mask", "CompositorNodeEllipseMask"),
    mask_node("box_mask", "Box Mask", "CompositorNodeBoxMask"),
    node("dilate_erode", "Dilate/Erode", "Mask", ["CompositorNodeDilateErode"],
         [inp("mask", "Mask", "float", ["Mask"], num("", "", 0.0, 0, 1))], [out("mask", "Mask", "float", ["Mask"])],
         [choice("type", "Type", "FEATHER", [("STEP", "Steps"), ("THRESHOLD", "Threshold"),
                                             ("DISTANCE", "Distance"), ("FEATHER", "Feather")], props=["mode"]),
          whole("size", "Size (px)", 20, -500, 500, sockets=["Size"], props=["distance"], pixels=True)],
         hint="Grows (positive) or shrinks (negative) a mask. Feather softens it."),

    # ---- Converter ----------------------------------------------------------------------
    node("math", "Math", "Converter", ["CompositorNodeMath", "ShaderNodeMath"],
         [inp("a", "Value", "float", ["Value"], num("", "", 0.5, -1000, 1000)),
          inp("b", "Value", "float", ["Value_001"], num("", "", 0.5, -1000, 1000))],
         [out("value", "Value", "float", ["Value"])],
         [choice("operation", "Operation", "MULTIPLY", [
             ("ADD", "Add"), ("SUBTRACT", "Subtract"), ("MULTIPLY", "Multiply"), ("DIVIDE", "Divide"),
             ("POWER", "Power"), ("MINIMUM", "Minimum"), ("MAXIMUM", "Maximum"),
             ("LESS_THAN", "Less Than"), ("GREATER_THAN", "Greater Than")], sockets=[], props=["operation"]),
          flag("clamp", "Clamp", False, sockets=[], props=["use_clamp"])]),

    # ---- Distort ------------------------------------------------------------------------
    node("lens", "Lens Distortion", "Distort", ["CompositorNodeLensdist"], [IMAGE_IN], [IMAGE_OUT],
         [num("distortion", "Distortion", 0.0, -0.999, 1),
          num("dispersion", "Dispersion", 0.02, 0, 1, 0.005),
          flag("jitter", "Jitter", False, props=["use_jitter"]),
          flag("fit", "Fit", False, props=["use_fit"])],
         hint="Dispersion gives chromatic aberration (color fringes)."),
    node("transform", "Transform", "Distort", ["CompositorNodeTransform"], [IMAGE_IN], [IMAGE_OUT],
         [num("x", "X (px)", 0, -4096, 4096, 1, pixels=True, sockets=["X"]),
          num("y", "Y (px)", 0, -4096, 4096, 1, pixels=True, sockets=["Y"]),
          angle("angle", "Angle", 0, ["Angle"]),
          num("scale", "Scale", 1.0, 0.01, 10, sockets=["Scale"])]),
    node("rotate", "Rotate", "Distort", ["CompositorNodeRotate"], [IMAGE_IN], [IMAGE_OUT],
         [angle("angle", "Angle", 0, ["Angle", "Degr"])]),
    node("flip", "Flip", "Distort", ["CompositorNodeFlip"], [IMAGE_IN], [IMAGE_OUT],
         [choice("axis", "Axis", "X", [("X", "Horizontal"), ("Y", "Vertical"), ("XY", "Both")],
                 sockets=[], props=["axis"])]),
]

BY_ID = {n["id"]: n for n in NODES}


def values_of(spec):
    """Every value a node keeps: its settings plus the inputs that have a value when unplugged."""
    return list(spec["params"]) + [i["value"] for i in spec["inputs"] if i["value"]]


def catalog():
    """The node list for the page (without the Blender socket/property names)."""
    def public(p):
        return {k: v for k, v in p.items() if k not in ("sockets", "props")}
    return [{
        "id": n["id"], "label": n["label"], "group": n["group"], "hint": n["hint"],
        "fixed": n.get("fixed", False), "single": n.get("single", False),
        "inputs": [{"id": i["id"], "label": i["label"], "type": i["type"],
                    "value": public(i["value"]) if i["value"] else None} for i in n["inputs"]],
        "outputs": [{"id": o["id"], "label": o["label"], "type": o["type"]} for o in n["outputs"]],
        "params": [public(p) for p in n["params"]],
    } for n in NODES]


def default_graph():
    return {
        "nodes": [{"id": "render", "type": "render", "x": 0, "y": 0, "mute": False, "values": {}},
                  {"id": "composite", "type": "composite", "x": 520, "y": 0, "mute": False, "values": {}}],
        "links": [{"from": ["render", "image"], "to": ["composite", "image"]}],
    }


# ---- Checking what the browser sends --------------------------------------------------
def clean_value(p, v):
    kind = p["kind"]
    if kind == "bool":
        return bool(v) if isinstance(v, bool) else p["default"]
    if kind == "choice":
        return v if v in [c[0] for c in p["choices"]] else p["default"]
    if kind == "color":
        try:
            rgb = [min(1.0, max(0.0, float(x))) for x in v][:3]
        except (TypeError, ValueError):
            return p["default"]
        return rgb if len(rgb) == 3 else p["default"]
    try:
        n = float(v)
    except (TypeError, ValueError):
        return p["default"]
    if n != n:  # NaN
        return p["default"]
    n = min(p["max"], max(p["min"], n))
    return int(round(n)) if kind == "int" else n


def _number(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return 0.0
    return min(1e5, max(-1e5, n)) if n == n else 0.0


def clean_graph(raw):
    raw = raw if isinstance(raw, dict) else {}
    nodes, by_id, kinds = [], {}, {}
    for item in raw.get("nodes") if isinstance(raw.get("nodes"), list) else []:
        if not isinstance(item, dict) or len(nodes) >= MAX_NODES:
            continue
        spec = BY_ID.get(item.get("type"))
        nid = item.get("id")
        if not spec or not isinstance(nid, str) or not RE_NODE_ID.match(nid) or nid in by_id:
            continue
        if (spec.get("fixed") or spec.get("single")) and kinds.get(spec["id"]):
            continue  # only one Render Result, Composite and Viewer
        given = item.get("values") if isinstance(item.get("values"), dict) else {}
        n = {"id": nid, "type": spec["id"], "x": _number(item.get("x")), "y": _number(item.get("y")),
             "mute": bool(item.get("mute")) and not spec.get("fixed"),
             "values": {p["id"]: clean_value(p, given.get(p["id"])) for p in values_of(spec)}}
        nodes.append(n)
        by_id[nid] = n
        kinds[spec["id"]] = True
    for fixed in default_graph()["nodes"]:
        if not kinds.get(fixed["type"]):
            fixed = dict(fixed, id=fixed["id"] if fixed["id"] not in by_id else fixed["id"] + "_1")
            nodes.append(fixed)
            by_id[fixed["id"]] = fixed

    links, into, edges = {}, {}, {}

    def reaches(a, b):  # is there already a path a -> ... -> b?
        seen, todo = set(), [a]
        while todo:
            cur = todo.pop()
            if cur == b:
                return True
            if cur not in seen:
                seen.add(cur)
                todo.extend(edges.get(cur, ()))
        return False

    for item in raw.get("links") if isinstance(raw.get("links"), list) else []:
        try:
            (src, out_id), (dst, in_id) = item["from"], item["to"]
        except (TypeError, KeyError, ValueError):
            continue
        a, b = by_id.get(src), by_id.get(dst)
        if not a or not b or src == dst:
            continue
        if out_id not in [o["id"] for o in BY_ID[a["type"]]["outputs"]]:
            continue
        if in_id not in [i["id"] for i in BY_ID[b["type"]]["inputs"]]:
            continue
        if (dst, in_id) in into or reaches(dst, src):
            continue  # one wire per input, and no loops
        into[(dst, in_id)] = True
        edges.setdefault(src, []).append(dst)
        links[(dst, in_id)] = {"from": [src, out_id], "to": [dst, in_id]}
    return {"nodes": nodes, "links": list(links.values())}


def clean(raw):
    """{"mode": file|off|custom, "graph": {...}} with every value checked."""
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else "file"
    if mode != "custom":
        return {"mode": mode}
    return {"mode": mode, "graph": clean_graph(raw.get("graph")) if raw.get("graph") else default_graph()}
