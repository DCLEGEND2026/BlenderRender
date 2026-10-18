"""Blender Render website: upload a .blend (or .zip), render it on the Colab GPU, download the result.

Settings come from environment variables (the Colab notebook sets them):
  BLENDER_BIN   path to the blender executable          (default: blender)
  JOBS_DIR      where uploads and renders are kept       (default: ./jobs)
  ACCESS_KEY    password for the site; empty = no login  (default: empty)
  PORT          port to listen on                        (default: 5000)
"""
import hmac
import os
import re
import shutil
import subprocess

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory

from jobs import JobManager

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
JOBS_DIR = os.path.abspath(os.environ.get("JOBS_DIR", "jobs"))
UPLOAD_DIR = os.path.join(JOBS_DIR, "_uploads")
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
PORT = int(os.environ.get("PORT", "5000"))

CHUNK_MAX = 64 * 1024 * 1024        # one upload request; the tunnel caps requests at 100 MB
UPLOAD_MAX = 20 * 1024 * 1024 * 1024  # whole file
RE_UPLOAD_ID = re.compile(r"^[a-f0-9]{16,32}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK_MAX + 1024 * 1024
os.makedirs(UPLOAD_DIR, exist_ok=True)
jobs = JobManager(JOBS_DIR, BLENDER_BIN)


# ---- Login -----------------------------------------------------------------------
def key_ok(value):
    return not ACCESS_KEY or hmac.compare_digest(str(value or ""), ACCESS_KEY)


@app.before_request
def require_key():
    if not ACCESS_KEY or request.endpoint in ("login", "static", "healthz"):
        return None
    if key_ok(request.cookies.get("br_key")):
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="Not logged in"), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    key = request.values.get("key", "")
    if key and key_ok(key):
        resp = redirect("/")
        resp.set_cookie("br_key", key, max_age=30 * 86400, httponly=True, samesite="Lax",
                        secure=request.headers.get("X-Forwarded-Proto", request.scheme) == "https")
        return resp
    return render_template("login.html", wrong=bool(key))


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def index():
    return render_template("index.html")


# ---- System info -----------------------------------------------------------------
_blender_version = None


def blender_version():
    global _blender_version
    if _blender_version is None:
        try:
            out = subprocess.run([BLENDER_BIN, "-b", "--version"], capture_output=True, text=True, timeout=60).stdout
            line = next((l.strip() for l in out.splitlines() if l.startswith("Blender")), "unknown")
            _blender_version = line.split(" (hash")[0]
        except Exception:
            _blender_version = "not found"
    return _blender_version


@app.route("/api/system")
def system():
    gpu = ""
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        name, used, total, util = [x.strip() for x in gpu.split(",")]
        gpu = {"name": name, "mem_used": int(used), "mem_total": int(total), "util": int(util)}
    except Exception:
        gpu = None
    disk = shutil.disk_usage(JOBS_DIR)
    return jsonify(blender=blender_version(), gpu=gpu, disk_free=disk.free, disk_total=disk.total)


# ---- Upload (chunked, so files bigger than the tunnel's 100 MB request cap work) --
@app.route("/api/upload", methods=["POST"])
def upload_chunk():
    upload_id = request.headers.get("X-Upload-Id", "")
    if not RE_UPLOAD_ID.match(upload_id):
        abort(400, "Bad upload id")
    try:
        offset = int(request.headers.get("X-Offset", "-1"))
    except ValueError:
        offset = -1
    data = request.get_data(cache=False)
    if offset < 0 or offset + len(data) > UPLOAD_MAX:
        abort(400, "Bad offset or file too big")
    path = os.path.join(UPLOAD_DIR, upload_id + ".part")
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if offset != size:
        # Chunks arrive in order; tell the client where to continue.
        return jsonify(ok=False, size=size), 409
    with open(path, "ab") as f:
        f.write(data)
    return jsonify(ok=True, size=size + len(data))


def int_or_none(v, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def float_or_none(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def clean_options(raw):
    raw = raw or {}
    pick = lambda key, allowed, default: raw.get(key) if raw.get(key) in allowed else default
    mode = pick("mode", ("still", "animation"), "still")
    opts = {
        "mode": mode,
        "engine": pick("engine", ("file", "cycles", "eevee", "workbench"), "cycles"),
        "format": pick("format", ("PNG", "JPEG", "OPEN_EXR"), "PNG"),
        "resolution_pct": int_or_none(raw.get("resolution_pct"), 1, 100),
        "video": bool(raw.get("video")) and mode == "animation",
        # Cycles (None / "file" = keep what the .blend has)
        "device": pick("device", ("gpu", "cpu"), "gpu"),
        "samples": int_or_none(raw.get("samples"), 1, 65536),
        "noise_threshold": float_or_none(raw.get("noise_threshold"), 0, 1),
        "min_samples": int_or_none(raw.get("min_samples"), 0, 65536),
        "time_limit": float_or_none(raw.get("time_limit"), 0, 86400),
        "max_bounces": int_or_none(raw.get("max_bounces"), 0, 1024),
        "denoise": pick("denoise", ("file", "on", "off"), "file"),
        "denoiser": pick("denoiser", ("file", "OPTIX", "OPENIMAGEDENOISE"), "file"),
        # Eevee
        "eevee_samples": int_or_none(raw.get("eevee_samples"), 1, 65536),
        "eevee_raytracing": pick("eevee_raytracing", ("file", "on", "off"), "file"),
    }
    if mode == "still":
        opts["frame"] = int_or_none(raw.get("frame"), -1048574, 1048574)
    else:
        opts["frame_start"] = int_or_none(raw.get("frame_start"), -1048574, 1048574)
        opts["frame_end"] = int_or_none(raw.get("frame_end"), -1048574, 1048574)
    return opts


# ---- Jobs ------------------------------------------------------------------------
def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        abort(404, "No such job")
    return job


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    return jsonify(jobs=[j.to_dict() for j in jobs.all()])


@app.route("/api/jobs", methods=["POST"])
def create_job():
    body = request.get_json(silent=True) or {}
    upload_id = str(body.get("upload_id", ""))
    filename = os.path.basename(str(body.get("filename", "")))
    path = os.path.join(UPLOAD_DIR, upload_id + ".part")
    if not RE_UPLOAD_ID.match(upload_id) or not os.path.exists(path):
        return jsonify(error="Upload not found, please upload again"), 400
    try:
        job = jobs.create(path, filename, clean_options(body.get("options")))
    except ValueError as e:
        if os.path.exists(path):
            os.remove(path)
        return jsonify(error=str(e)), 400
    return jsonify(job=job.to_dict())


@app.route("/api/jobs/<job_id>")
def job_detail(job_id):
    return jsonify(job=get_job(job_id).to_dict(full=True))


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    get_job(job_id)
    jobs.cancel(job_id)
    return jsonify(ok=True)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def job_delete(job_id):
    get_job(job_id)
    jobs.delete(job_id)
    return jsonify(ok=True)


@app.route("/api/jobs/<job_id>/files/<path:name>")
def job_file(job_id, name):
    job = get_job(job_id)
    return send_from_directory(job.output_dir, name, as_attachment=request.args.get("dl") == "1")


@app.route("/api/jobs/<job_id>/zip")
def job_zip(job_id):
    job = get_job(job_id)
    if not job.saved:
        abort(404, "Nothing rendered yet")
    path = jobs.zip_outputs(job)
    stem = os.path.splitext(job.name)[0] or "render"
    return send_file(path, as_attachment=True, download_name="%s_renders.zip" % stem)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
def json_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error=getattr(e, "description", str(e))), e.code
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
