"""Compositing nodes the website offers, and the check for what the browser sends.

Every entry is a real Blender compositor node. The page shows them as a stack:
render result -> node 1 -> node 2 -> ... -> output. blender_composite.py builds the
node tree from this same list, so the page and Blender always agree.

Blender moved most node settings from node properties to input sockets between
4.x and 5.x, so each setting lists the socket names to try first ("Size:VECTOR"
only matches a vector socket) and the older property names to fall back to.
"when" shows a setting on the page only for some values of another setting
(Glare's Size only for Bloom and Fog Glow).

This file is plain Python: it is imported by the server and inside Blender.
"""

MAX_NODES = 32
MODES = ("file", "off", "custom")


def num(id, label, default, lo, hi, step=0.01, sockets=None, props=(), kind="float"):
    return {"id": id, "label": label, "kind": kind, "default": default, "min": lo, "max": hi,
            "step": step, "sockets": sockets if sockets is not None else [label], "props": list(props)}


def whole(id, label, default, lo, hi, sockets=None, props=()):
    return num(id, label, default, lo, hi, 1, sockets, props, kind="int")


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


FACTOR = num("factor", "Factor", 1.0, 0, 1, sockets=["Fac", "Factor"])

NODES = [
    # ---- Color ------------------------------------------------------------------
    {"id": "exposure", "label": "Exposure", "group": "Color", "node": ["CompositorNodeExposure"],
     "params": [num("exposure", "Exposure", 0.0, -10, 10, 0.1)]},
    {"id": "bright_contrast", "label": "Brightness/Contrast", "group": "Color", "node": ["CompositorNodeBrightContrast"],
     "params": [num("bright", "Brightness", 0.0, -100, 100, 1, sockets=["Bright", "Brightness"]),
                num("contrast", "Contrast", 0.0, -100, 100, 1)]},
    {"id": "hue_sat", "label": "Hue/Saturation/Value", "group": "Color", "node": ["CompositorNodeHueSat"],
     "params": [num("hue", "Hue", 0.5, 0, 1), num("saturation", "Saturation", 1.0, 0, 2),
                num("value", "Value", 1.0, 0, 2), FACTOR]},
    {"id": "color_balance", "label": "Color Balance", "group": "Color", "node": ["CompositorNodeColorBalance"],
     "hint": "Lift tints the shadows, Gamma the midtones, Gain the highlights. White = no change.",
     "params": [color("lift", "Lift", [1, 1, 1], ["Color Lift"], ["lift"]),
                color("gamma", "Gamma", [1, 1, 1], ["Color Gamma"], ["gamma"]),
                color("gain", "Gain", [1, 1, 1], ["Color Gain"], ["gain"]),
                FACTOR]},
    {"id": "color_correction", "label": "Color Correction", "group": "Color", "node": ["CompositorNodeColorCorrection"],
     "params": [num("saturation", "Saturation", 1.0, 0, 4, sockets=["Master Saturation"], props=["master_saturation"]),
                num("contrast", "Contrast", 1.0, 0, 4, sockets=["Master Contrast"], props=["master_contrast"]),
                num("gamma", "Gamma", 1.0, 0.01, 4, sockets=["Master Gamma"], props=["master_gamma"]),
                num("gain", "Gain", 1.0, 0, 4, sockets=["Master Gain"], props=["master_gain"]),
                num("offset", "Offset", 0.0, -1, 1, sockets=["Master Offset"], props=["master_lift"])]},
    {"id": "mix", "label": "Mix (color)", "group": "Color", "node": ["CompositorNodeMixRGB", "ShaderNodeMix"],
     "setup": {"data_type": "RGBA"},
     "input": ["A_Color", "Image"], "output": ["Result_Color", "Image"],
     "hint": "Blends a flat color over the image, like a Mix node with the color in its bottom slot.",
     "params": [choice("blend", "Blend", "OVERLAY", [
                    ("MIX", "Mix"), ("MULTIPLY", "Multiply"), ("SCREEN", "Screen"), ("OVERLAY", "Overlay"),
                    ("SOFT_LIGHT", "Soft Light"), ("ADD", "Add"), ("SUBTRACT", "Subtract"), ("COLOR", "Color"),
                    ("HUE", "Hue"), ("DARKEN", "Darken"), ("LIGHTEN", "Lighten")],
                    sockets=[], props=["blend_type"]),
                color("color", "Color", [1.0, 0.6, 0.3], ["B_Color", "Image_001"]),
                num("factor", "Factor", 0.25, 0, 1, sockets=["Factor_Float", "Fac"])]},
    {"id": "posterize", "label": "Posterize", "group": "Color", "node": ["CompositorNodePosterize"],
     "params": [num("steps", "Steps", 8, 2, 1024, 1)]},
    {"id": "invert", "label": "Invert Color", "group": "Color", "node": ["CompositorNodeInvert"],
     "input": ["Color"], "output": ["Color"],
     "params": [FACTOR]},

    # ---- Filter -----------------------------------------------------------------
    {"id": "glare", "label": "Glare", "group": "Filter", "node": ["CompositorNodeGlare"],
     "params": [choice("type", "Type", "BLOOM", [
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
                dict(whole("streaks", "Streaks", 4, 1, 16, props=["streaks"]), when={"type": ["STREAKS"]})]},
    {"id": "blur", "label": "Blur", "group": "Filter", "node": ["CompositorNodeBlur"],
     "params": [choice("type", "Type", "GAUSS", [
                    ("GAUSS", "Gaussian"), ("FAST_GAUSS", "Fast Gaussian"), ("FLAT", "Flat"),
                    ("TENT", "Tent"), ("CUBIC", "Cubic")],
                    props=["filter_type"]),
                whole("size", "Size (px)", 10, 0, 2048, sockets=["Size:VECTOR"], props=["size_x", "size_y"])]},
    {"id": "denoise", "label": "Denoise", "group": "Filter", "node": ["CompositorNodeDenoise"],
     "hint": "OpenImageDenoise on the finished image, on the CPU.",
     "params": [choice("prefilter", "Prefilter", "ACCURATE", [("NONE", "None"), ("FAST", "Fast"), ("ACCURATE", "Accurate")],
                       props=["prefilter"]),
                flag("hdr", "HDR", True, props=["use_hdr"])]},
    {"id": "filter", "label": "Filter", "group": "Filter", "node": ["CompositorNodeFilter"],
     "params": [choice("type", "Type", "SHARPEN", [
                    ("SOFTEN", "Soften"), ("SHARPEN", "Box Sharpen"), ("SHARPEN_DIAMOND", "Diamond Sharpen"),
                    ("LAPLACE", "Laplace"), ("SOBEL", "Sobel"), ("PREWITT", "Prewitt"),
                    ("KIRSCH", "Kirsch"), ("SHADOW", "Shadow")],
                    props=["filter_type"]),
                FACTOR]},
    {"id": "kuwahara", "label": "Kuwahara", "group": "Filter", "node": ["CompositorNodeKuwahara"],
     "hint": "Painterly look.",
     "params": [choice("type", "Type", "ANISOTROPIC", [("CLASSIC", "Classic"), ("ANISOTROPIC", "Anisotropic")],
                       props=["variation"]),
                num("size", "Size", 6, 1, 100, 1)]},
    {"id": "pixelate", "label": "Pixelate", "group": "Filter", "node": ["CompositorNodePixelate"],
     "input": ["Color"], "output": ["Color"],
     "params": [whole("size", "Size (px)", 8, 1, 512, sockets=["Size"], props=["pixel_size"])]},

    # ---- Lens & transform -------------------------------------------------------
    {"id": "lens", "label": "Lens Distortion", "group": "Lens & transform", "node": ["CompositorNodeLensdist"],
     "hint": "Dispersion gives chromatic aberration (color fringes).",
     "params": [num("distortion", "Distortion", 0.0, -0.999, 1),
                num("dispersion", "Dispersion", 0.02, 0, 1, 0.005),
                flag("jitter", "Jitter", False, props=["use_jitter"]),
                flag("fit", "Fit", False, props=["use_fit"])]},
    {"id": "vignette", "label": "Vignette", "group": "Lens & transform",
     "node": [],  # built from Ellipse Mask + Blur + Mix nodes in blender_composite.py
     "hint": "Ellipse Mask, Blur and Multiply nodes that darken the corners.",
     "params": [num("amount", "Amount", 0.5, 0, 1, sockets=[]),
                num("size", "Size", 0.9, 0.1, 2, sockets=[]),
                num("softness", "Softness", 0.4, 0, 1, sockets=[])]},
    {"id": "flip", "label": "Flip", "group": "Lens & transform", "node": ["CompositorNodeFlip"],
     "params": [choice("axis", "Axis", "X", [("X", "Horizontal"), ("Y", "Vertical"), ("XY", "Both")],
                       sockets=[], props=["axis"])]},
    {"id": "rotate", "label": "Rotate", "group": "Lens & transform", "node": ["CompositorNodeRotate"],
     "params": [angle("angle", "Angle", 0, ["Angle", "Degr"])]},
]

BY_ID = {n["id"]: n for n in NODES}


def catalog():
    """The node list for the page (without the Blender socket/property names)."""
    return [{
        "id": n["id"], "label": n["label"], "group": n["group"], "hint": n.get("hint", ""),
        "params": [{k: v for k, v in p.items() if k not in ("sockets", "props")} for p in n["params"]],
    } for n in NODES]


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


def clean(raw):
    """{"mode": file|off|custom, "nodes": [{"type", "mute", "params"}]} with every value checked."""
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else "file"
    nodes = []
    if mode == "custom" and isinstance(raw.get("nodes"), list):
        for item in raw["nodes"][:MAX_NODES]:
            spec = BY_ID.get(item.get("type")) if isinstance(item, dict) else None
            if not spec:
                continue
            given = item.get("params") if isinstance(item.get("params"), dict) else {}
            nodes.append({
                "type": spec["id"],
                "mute": bool(item.get("mute")),
                "params": {p["id"]: clean_value(p, given.get(p["id"])) for p in spec["params"]},
            })
    return {"mode": mode, "nodes": nodes}
