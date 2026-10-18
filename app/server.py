"""Blender Render website: upload a .blend (or .zip), render it on the Colab GPU, download the result.

Settings come from environment variables (the Colab notebook sets them):
  BLENDER_BIN   path to the blender executable          (default: blender)
  JOBS_DIR      where uploads and renders are kept       (default: ./jobs)
  ACCESS_KEY    password for the site; empty = no login  (default: empty)
  PORT          port to listen on                        (default: 5000)
  DRIVE_DIR     mounted Google Drive folder to pick files from; empty = off (default: empty)
"""
import hmac
import io
import os
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory

import compositing
from jobs import JobManager
from preview import Previewer

BLENDER_BIN = os.environ.get("BLENDER_BIN", "blender")
JOBS_DIR = os.path.abspath(os.environ.get("JOBS_DIR", "jobs"))
UPLOAD_DIR = os.path.join(JOBS_DIR, "_uploads")
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
PORT = int(os.environ.get("PORT", "5000"))
DRIVE_DIR = os.path.abspath(os.environ["DRIVE_DIR"]) if os.environ.get("DRIVE_DIR") else ""

CHUNK_MAX = 64 * 1024 * 1024        # one upload request; the tunnel caps requests at 100 MB
UPLOAD_MAX = 20 * 1024 * 1024 * 1024  # whole file
RE_UPLOAD_ID = re.compile(r"^[a-f0-9]{16,32}$")
RE_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = CHUNK_MAX + 1024 * 1024
os.makedirs(UPLOAD_DIR, exist_ok=True)
jobs = JobManager(JOBS_DIR, BLENDER_BIN)
previewer = Previewer(BLENDER_BIN)


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


@app.route("/compare")
def compare():
    return render_template("compare.html")


@app.route("/compositor/<job_id>")
def compositor(job_id):
    job = get_job(job_id)
    return render_template("compositor.html", job_id=job.id, job_name=job.name,
                           comp_nodes=compositing.catalog(), default_graph=compositing.default_graph())


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
        # As in file / off / the website's own node stack (see compositing.py)
        "compositing": compositing.clean(raw.get("compositing")),
    }
    if mode == "still":
        opts["frame"] = int_or_none(raw.get("frame"), -1048574, 1048574)
    else:
        opts["frame_start"] = int_or_none(raw.get("frame_start"), -1048574, 1048574)
        opts["frame_end"] = int_or_none(raw.get("frame_end"), -1048574, 1048574)
    return opts


# ---- Google Drive ------------------------------------------------------------------
def drive_label():
    mine = "/content/drive/MyDrive/"
    return "My Drive/" + DRIVE_DIR[len(mine):] if DRIVE_DIR.startswith(mine) else DRIVE_DIR


def drive_ready():
    """Whether the Drive folder is there, making it if it was deleted while Drive is connected.
    Only when its grandparent (My Drive) exists: otherwise Drive isn't mounted, and folders made
    on the local disk would block mounting it later."""
    if not DRIVE_DIR:
        return False
    if not os.path.isdir(DRIVE_DIR) and os.path.isdir(os.path.dirname(os.path.dirname(DRIVE_DIR))):
        try:
            os.makedirs(DRIVE_DIR, exist_ok=True)
        except OSError:
            pass
    return os.path.isdir(DRIVE_DIR)


@app.route("/api/drive")
def drive_files():
    """.blend and .zip files in the mounted Drive folder (and its subfolders), newest first."""
    if not drive_ready():
        return jsonify(enabled=False, folder="", files=[])
    files = []
    for base, dirs, names in os.walk(DRIVE_DIR):
        depth = os.path.relpath(base, DRIVE_DIR).count(os.sep) + (base != DRIVE_DIR)
        # Skip the Renders folder (step 5 of the notebook copies results there) and go 3 levels deep.
        dirs[:] = [] if depth >= 3 else [d for d in dirs if not d.startswith(".") and not (base == DRIVE_DIR and d == "Renders")]
        for name in names:
            if name.lower().endswith((".blend", ".zip")):
                path = os.path.join(base, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files.append({"path": os.path.relpath(path, DRIVE_DIR), "name": name, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda f: -f["mtime"])
    return jsonify(enabled=True, folder=drive_label(), files=files[:500])


def drive_path(rel):
    """A path inside DRIVE_DIR from the page, or None if it points anywhere else."""
    if not DRIVE_DIR or not isinstance(rel, str):
        return None
    path = os.path.realpath(os.path.join(DRIVE_DIR, rel))
    if not path.startswith(os.path.realpath(DRIVE_DIR) + os.sep) or not os.path.isfile(path):
        return None
    return path if path.lower().endswith((".blend", ".zip")) else None


def file_link(url):
    """(URL to download, is a Drive folder, from Google Drive) for a link from the page, or None.
    Google Drive share links (files and folders) and direct download links both work."""
    try:
        u = urlparse(str(url).strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    if u.hostname in DRIVE_HOSTS:
        parts = [p for p in u.path.split("/") if p]
        if "folders" in parts and parts.index("folders") + 1 < len(parts):
            fid = parts[parts.index("folders") + 1]
            return ("https://drive.google.com/drive/folders/" + fid, True, True) if RE_DRIVE_ID.match(fid) else None
        fid = parts[parts.index("d") + 1] if "d" in parts and parts.index("d") + 1 < len(parts) else \
            (parse_qs(u.query).get("id") or [""])[0]
        return ("https://drive.google.com/uc?id=" + fid, False, True) if RE_DRIVE_ID.match(fid) else None
    if u.hostname.endswith("dropbox.com"):  # a share link opens a web page unless dl=1
        query = [q for q in u.query.split("&") if q and not q.startswith("dl=")] + ["dl=1"]
        return (u._replace(query="&".join(query)).geturl(), False, False)
    return (u.geturl(), False, False)


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
    opts = clean_options(body.get("options"))
    if "drive_path" in body:
        path = drive_path(body["drive_path"])
        if not path:
            return jsonify(error="That file is not in the Google Drive folder any more"), 400
        try:
            return jsonify(job=jobs.create_from_drive(path, opts).to_dict())
        except ValueError as e:
            return jsonify(error=str(e)), 400
    if "url" in body:
        link = file_link(body["url"])
        if not link:
            if any(h in str(body["url"]) for h in DRIVE_HOSTS):
                return jsonify(error="That Google Drive link isn't to a file or folder. Use Share → Copy link "
                                     "on the file (or the folder) itself."), 400
            return jsonify(error="That doesn't look like a link. Paste the whole thing, starting with https://"), 400
        return jsonify(job=jobs.create_from_link(*link, opts=opts).to_dict())
    if "again" in body:  # the same file as an earlier render, with new settings
        old = get_job(str(body["again"]))
        if opts["compositing"]["mode"] == "custom" and old.custom_compositing():
            opts["compositing"] = old.opts["compositing"]  # keep the nodes built for it
        try:
            return jsonify(job=jobs.create_again(old, opts).to_dict())
        except ValueError as e:
            return jsonify(error=str(e)), 400
    upload_id = str(body.get("upload_id", ""))
    filename = os.path.basename(str(body.get("filename", "")))
    path = os.path.join(UPLOAD_DIR, upload_id + ".part")
    if not RE_UPLOAD_ID.match(upload_id) or not os.path.exists(path):
        return jsonify(error="Upload not found, please upload again"), 400
    try:
        job = jobs.create(path, filename, opts)
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


@app.route("/api/jobs/<job_id>/preview", methods=["POST"])
def job_preview(job_id):
    """The graph applied to one raw frame, at scale (a share of the render's size) for speed."""
    job = get_job(job_id)
    raw = sorted(job.raw)
    if not raw:
        return jsonify(error="No rendered frames yet"), 400
    body = request.get_json(silent=True) or {}
    frame = int_or_none(body.get("frame"), 0, len(raw) - 1) or 0
    graph = compositing.clean_graph(body.get("graph"))
    out_dir = os.path.join(job.dir, "preview")
    os.makedirs(out_dir, exist_ok=True)
    try:
        scale = float(body.get("scale", 0.5))
    except (TypeError, ValueError):
        scale = 0.5
    scale = min(1.0, max(0.1, scale))
    try:
        path = previewer.render(raw[frame], job.info, graph, out_dir, scale)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    if path is None:
        return "", 204  # nothing plugged into the output
    with open(path, "rb") as f:
        data = io.BytesIO(f.read())
    os.remove(path)
    return send_file(data, mimetype="image/png", max_age=0)


@app.route("/api/jobs/<job_id>/composite", methods=["POST"])
def job_composite(job_id):
    get_job(job_id)
    body = request.get_json(silent=True) or {}
    comp = compositing.clean({"mode": "custom", "graph": body.get("graph")})
    try:
        job = jobs.recomposite(job_id, comp)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(job=job.to_dict())


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
@app.errorhandler(500)
def json_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error=getattr(e, "description", str(e))), e.code
    return e


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
