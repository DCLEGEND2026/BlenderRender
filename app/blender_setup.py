# Runs inside Blender (blender -b scene.blend -P blender_setup.py -- <options.json> <info.json> [raw]).
# Applies the render options chosen on the website, turns on the GPU for Cycles,
# and writes scene info (frame range, fps, resolution, color settings) for the server.
# With "raw" the frames are saved as linear EXR without the file's compositing, for
# blender_composite.py to add the website's compositing nodes afterwards.
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
opts = json.load(open(argv[0])) if argv else {}
info_path = argv[1] if len(argv) > 1 else None
raw = len(argv) > 2 and argv[2] == "raw"

scene = bpy.context.scene
render = scene.render


def log(msg):
    print("[setup] " + msg, flush=True)


def set_engine(name):
    """Switch to a friendly engine name; returns False if this Blender doesn't have it.
    Eevee's id changed across versions, and Cycles is an add-on engine that isn't in
    the static enum list, so just try each id."""
    for eid in {
        "cycles": ["CYCLES"],
        "eevee": ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"],
        "workbench": ["BLENDER_WORKBENCH"],
    }.get(name, []):
        try:
            render.engine = eid
            return True
        except TypeError:
            continue
    return False


def enable_gpu():
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.get_devices()
        gpus = [d for d in prefs.devices if d.type == kind]
        if gpus:
            for d in prefs.devices:
                d.use = d.type == kind
            scene.cycles.device = "GPU"
            log("Cycles on GPU (%s): %s" % (kind, ", ".join(d.name for d in gpus)))
            return True
    scene.cycles.device = "CPU"
    log("No CUDA/OptiX GPU found, Cycles will use the CPU")
    return False


# ---- Engine ----------------------------------------------------------------------
engine = opts.get("engine", "file")
if engine != "file":
    if not set_engine(engine):
        log("Engine %r is not available in this Blender, keeping %s" % (engine, render.engine))
log("Engine: " + render.engine)

if render.engine == "CYCLES":
    if opts.get("device", "gpu") == "gpu":
        enable_gpu()
    else:
        scene.cycles.device = "CPU"
        log("Cycles on CPU")
    cy = scene.cycles
    if opts.get("samples"):
        cy.samples = int(opts["samples"])
    if opts.get("noise_threshold") is not None:
        t = float(opts["noise_threshold"])
        cy.use_adaptive_sampling = t > 0  # 0 = adaptive sampling off
        if t > 0:
            cy.adaptive_threshold = t
    if opts.get("min_samples") is not None:
        cy.adaptive_min_samples = int(opts["min_samples"])
    if opts.get("time_limit") is not None:
        cy.time_limit = float(opts["time_limit"])
    if opts.get("max_bounces") is not None:
        cy.max_bounces = int(opts["max_bounces"])
    if opts.get("denoise") in ("on", "off"):
        cy.use_denoising = opts["denoise"] == "on"
    if opts.get("denoiser") in ("OPTIX", "OPENIMAGEDENOISE"):
        try:
            cy.denoiser = opts["denoiser"]
        except TypeError:
            log("Denoiser %s is not available here, keeping %s" % (opts["denoiser"], cy.denoiser))
    if hasattr(cy, "denoising_use_gpu"):
        cy.denoising_use_gpu = cy.device == "GPU"
    log("Samples: %d max, %s, min %d, time limit %s, bounces %d" % (
        cy.samples,
        "noise threshold %g" % cy.adaptive_threshold if cy.use_adaptive_sampling else "adaptive sampling off",
        cy.adaptive_min_samples, ("%gs" % cy.time_limit) if cy.time_limit else "none", cy.max_bounces))
    log("Denoise: %s" % (cy.denoiser if cy.use_denoising else "off"))

if render.engine in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    ee = scene.eevee
    if opts.get("eevee_samples"):
        ee.taa_render_samples = int(opts["eevee_samples"])
    if opts.get("eevee_raytracing") in ("on", "off") and hasattr(ee, "use_raytracing"):
        ee.use_raytracing = opts["eevee_raytracing"] == "on"
    log("Eevee samples: %d, ray tracing: %s" % (ee.taa_render_samples, getattr(ee, "use_raytracing", "n/a")))

# ---- Frames (the server always renders with -a, so set the range here) ----------
if opts.get("mode") == "still":
    f = int(opts.get("frame") or scene.frame_current)
    scene.frame_start = scene.frame_end = f
else:
    if opts.get("frame_start"):
        scene.frame_start = int(opts["frame_start"])
    if opts.get("frame_end"):
        scene.frame_end = int(opts["frame_end"])
    if scene.frame_end < scene.frame_start:
        scene.frame_end = scene.frame_start
log("Frames: %d-%d" % (scene.frame_start, scene.frame_end))

# ---- Output ----------------------------------------------------------------------
if opts.get("resolution_pct"):
    render.resolution_percentage = int(opts["resolution_pct"])

fmt = opts.get("format", "PNG")
image = render.image_settings
image.file_format = fmt
if fmt == "JPEG":
    image.quality = 92
final_image = {"color_mode": image.color_mode, "color_depth": image.color_depth}
render.use_file_extension = True
render.use_overwrite = True
render.use_placeholder = False

# ---- Compositing -----------------------------------------------------------------
if hasattr(render, "compositor_device"):
    render.compositor_device = "CPU"  # the GPU compositor needs a display
comp = opts.get("compositing", {}).get("mode", "file")
if comp == "off":
    render.use_compositing = False
    log("Compositing: off")
elif raw:
    # The website's nodes are added later on these frames, so the look can change without a re-render.
    render.use_compositing = False
    image.file_format = "OPEN_EXR"
    image.color_mode = "RGBA"
    image.color_depth = "16"
    image.exr_codec = "ZIP"
    log("Compositing: website nodes, rendering raw EXR frames first")
else:
    log("Compositing: as in file (%s)" % ("on" if render.use_compositing else "off"))

# Report textures the .blend points at but that were not uploaded.
try:
    bpy.ops.file.report_missing_files()
except Exception:
    pass

if info_path:
    with open(info_path, "w") as f:
        json.dump({
            "engine": render.engine,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "frame_current": scene.frame_current,
            "fps": render.fps / (render.fps_base or 1),
            "width": int(render.resolution_x * render.resolution_percentage / 100),
            "height": int(render.resolution_y * render.resolution_percentage / 100),
            "scene": scene.name,
            "blender": bpy.app.version_string,
            # For blender_composite.py, so composited frames look like a direct render.
            "image": final_image,
            "display": {"display_device": scene.display_settings.display_device},
            "view": {k: getattr(scene.view_settings, k) for k in (
                "view_transform", "look", "exposure", "gamma",
                "use_white_balance", "white_balance_temperature", "white_balance_tint")
                if hasattr(scene.view_settings, k)},
            "dither": render.dither_intensity,
        }, f)
