"""Render jobs: one render at a time (Colab has one GPU), plus a second queue for
re-running the compositing nodes on frames that are already rendered. A job's file
comes from an upload, the mounted Google Drive folder, or a Google Drive link."""
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP_SCRIPT = os.path.join(HERE, "blender_setup.py")
COMPOSITE_SCRIPT = os.path.join(HERE, "blender_composite.py")

RE_FRAME = re.compile(r"Fra:\s*(\d+)")
RE_SAMPLE = re.compile(r"Sample (\d+)\s*/\s*(\d+)")
RE_SAMPLE_ALT = re.compile(r"Rendering (\d+) / (\d+) samples")
RE_REMAINING = re.compile(r"Remaining:\s*([\d:.]+)")
RE_SAVED = re.compile(r"Saved: '(.+?)'")
# gdown's progress bar: " 45%|████▌     | 1.23G/2.70G [00:10<00:12, 120MB/s]"
RE_DOWNLOAD = re.compile(r"(\d+)%\|[^|]*\|\s*([\d.]+\s*\w*)/([\d.]+\s*\w*)\s*\[[^,\]]*(?:,\s*([^\]]+))?\]")

IMAGE_EXT = {"PNG": ".png", "JPEG": ".jpg", "OPEN_EXR": ".exr"}


class Job:
    def __init__(self, jobs_dir, name, opts):
        self.id = uuid.uuid4().hex[:10]
        self.dir = os.path.join(jobs_dir, self.id)
        self.input_dir = os.path.join(self.dir, "input")
        self.output_dir = os.path.join(self.dir, "output")
        self.raw_dir = os.path.join(self.dir, "raw")  # linear EXR frames before compositing
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)
        self.name = name
        self.opts = opts
        self.blend = None
        self.status = "queued"  # preparing, queued, running, compositing, encoding, done, failed, cancelled
        self.error = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.info = {}
        self.frame = None
        self.samples = (0, 0)
        self.remaining = ""
        self.fetch = ""  # while preparing: what is happening to the file (download progress)
        self.comp_started = None  # the last time compositing ran (Apply on the Compositor page)
        self.comp_finished = None
        self.saved = []
        self.raw = []
        self.version = 0  # bumped each time the frames are composited, so the page reloads them
        self.video = None
        self.proc = None
        # Keep the start of the log (setup, GPU, warnings) and a rolling tail.
        self.log_head = []
        self.log_tail = deque(maxlen=300)

    def custom_compositing(self):
        return self.opts.get("compositing", {}).get("mode") == "custom"

    # ---- progress ------------------------------------------------------------
    def frames_done(self):
        if self.status == "running" and self.custom_compositing():
            return len(self.raw)
        return len(self.saved)

    def total_frames(self):
        if not self.info:
            return 0
        return max(1, self.info["frame_end"] - self.info["frame_start"] + 1)

    def progress(self):
        if self.status in ("done", "encoding"):
            return 1.0
        if self.status == "compositing":
            return len(self.saved) / max(1, len(self.raw))
        total = self.total_frames()
        if not total:
            return 0.0
        part = self.samples[0] / self.samples[1] if self.samples[1] else 0.0
        return min(1.0, (self.frames_done() + part) / total)

    def log(self, line):
        if len(self.log_head) < 60:
            self.log_head.append(line)
        else:
            self.log_tail.append(line)

    def log_lines(self, tail=120):
        lines = list(self.log_tail)
        if len(lines) > tail:
            return self.log_head + ["..."] + lines[-tail:]
        return self.log_head + lines

    def to_dict(self, full=False):
        d = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "opts": self.opts,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "elapsed": ((self.finished or time.time()) - self.started) if self.started else 0,
            "info": self.info,
            "frame": self.frame,
            "frames_done": self.frames_done(),
            "frames_total": self.total_frames(),
            "samples": list(self.samples),
            "remaining": self.remaining,
            "fetch": self.fetch,
            "comp_elapsed": ((self.comp_finished or time.time()) - self.comp_started) if self.comp_started else 0,
            "progress": round(self.progress(), 4),
            "outputs": [os.path.basename(p) for p in self.saved],
            "video": self.video,
            "version": self.version,
            "raw_frames": len(self.raw),
            "has_file": bool(self.blend),  # can be rendered again
        }
        if full:
            d["log"] = self.log_lines()
        return d


class JobManager:
    def __init__(self, jobs_dir, blender_bin):
        self.jobs_dir = jobs_dir
        self.blender = blender_bin
        self.jobs = {}
        self.order = []
        self.queue = queue.Queue()
        self.comp_queue = queue.Queue()
        self.lock = threading.Lock()
        os.makedirs(jobs_dir, exist_ok=True)
        threading.Thread(target=self._worker, args=(self.queue, self._render), daemon=True).start()
        # Compositing runs on the CPU, so it doesn't have to wait for a render to finish.
        threading.Thread(target=self._worker, args=(self.comp_queue, self._recomposite), daemon=True).start()

    # ---- public API ----------------------------------------------------------
    def create(self, upload_path, filename, opts):
        job = Job(self.jobs_dir, filename, opts)
        try:
            job.blend = self._unpack(upload_path, filename, job.input_dir)
        except Exception as e:
            shutil.rmtree(job.dir, ignore_errors=True)
            raise ValueError(str(e))
        self._add(job)
        self.queue.put(job.id)
        return job

    def create_from_drive(self, path, opts):
        """A file in the mounted Google Drive folder. A .blend renders right where it is,
        so textures next to it on Drive are found; a .zip is unpacked here first."""
        name = os.path.basename(path)
        job = Job(self.jobs_dir, name, opts)
        if name.lower().endswith(".blend"):
            job.blend = path
            job.log("Rendering %s from Google Drive" % path)
            self._add(job)
            self.queue.put(job.id)
        else:
            job.fetch = "Unpacking the zip from Google Drive"
            self._prepare(job, lambda: self._unpack(path, name, job.input_dir, keep=True))
        return job

    def create_from_link(self, url, folder, drive, opts):
        """Download a file (or a Google Drive folder) with gdown, then render it."""
        name = ("Google Drive " + ("folder" if folder else "file")) if drive else \
            "Download from " + (urlparse(url).hostname or "link")
        job = Job(self.jobs_dir, name, opts)
        job.fetch = "Starting download"
        self._prepare(job, lambda: self._download(job, url, folder, drive))
        return job

    def create_again(self, old, opts):
        """Render an earlier render's file again with new settings."""
        if not old.blend or not os.path.isfile(old.blend):
            raise ValueError("The file of that render is gone, so it can't be rendered again. Add it again above.")
        job = Job(self.jobs_dir, old.name, opts)
        if not old.blend.startswith(old.input_dir + os.sep):  # rendered in place from Google Drive
            job.blend = old.blend
            job.log("Rendering %s again" % old.blend)
            self._add(job)
            self.queue.put(job.id)
            return job

        def copy():
            # Hard links: instant and no extra disk, and deleting the old render doesn't touch them.
            def link(src, dst):
                try:
                    os.link(src, dst)
                except OSError:
                    shutil.copy2(src, dst)
            shutil.copytree(old.input_dir, job.input_dir, copy_function=link, dirs_exist_ok=True)
            return os.path.join(job.input_dir, os.path.relpath(old.blend, old.input_dir))
        job.fetch = "Getting the file of the earlier render"
        self._prepare(job, copy)
        return job

    def recomposite(self, job_id, compositing):
        """Run new compositing nodes on a finished render's raw frames (no re-render)."""
        job = self.jobs.get(job_id)
        if job.status in ("preparing", "queued", "running", "compositing", "encoding"):
            raise ValueError("This render is still busy")
        if not job.raw:
            raise ValueError("This render has no raw frames to composite. Render it with Custom nodes first.")
        job.opts = dict(job.opts, compositing=compositing)
        job.status = "queued"
        job.error = ""
        self.comp_queue.put(job.id)
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def all(self):
        return [self.jobs[i] for i in self.order if i in self.jobs]

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status in ("queued", "preparing"):
            job.status = "cancelled"
            if job.proc:  # a download
                self._kill(job.proc)
        elif job.status in ("running", "compositing", "encoding") and job.proc:
            job.status = "cancelled"
            self._kill(job.proc)
        return True

    def delete(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return False
        self.cancel(job_id)
        with self.lock:
            self.jobs.pop(job_id, None)
            if job_id in self.order:
                self.order.remove(job_id)
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    def zip_outputs(self, job):
        path = os.path.join(job.dir, "renders_%s.zip" % job.id)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            for p in job.saved:
                if os.path.isfile(p):
                    z.write(p, os.path.basename(p))
            if job.video:
                z.write(os.path.join(job.output_dir, job.video), job.video)
        return path

    # ---- internals -----------------------------------------------------------
    def _add(self, job):
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)

    def _prepare(self, job, get_blend):
        """Get the job's file ready in the background (downloads can take minutes), then queue it."""
        job.status = "preparing"
        self._add(job)

        def run():
            try:
                blend = get_blend()
                if job.status != "preparing":
                    return  # cancelled meanwhile
                job.blend = blend
                job.fetch = ""
                job.status = "queued"
                self.queue.put(job.id)
            except Exception as e:
                if job.status == "preparing":
                    job.status = "failed"
                    job.fetch = ""
                    job.error = str(e)
                    job.finished = time.time()
            finally:
                job.proc = None
        threading.Thread(target=run, daemon=True).start()

    def _download(self, job, url, folder, drive):
        dl_dir = os.path.join(job.dir, "download")
        os.makedirs(dl_dir)
        try:
            return self._fetch(job, url, folder, drive, dl_dir)
        finally:
            shutil.rmtree(dl_dir, ignore_errors=True)  # partial downloads too: Colab's disk is small

    def _fetch(self, job, url, folder, drive, dl_dir):
        cmd = [sys.executable, "-m", "gdown"] + (["--folder"] if folder else []) + [url, "-O", dl_dir + os.sep]
        job.log("$ " + " ".join(cmd))
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        # The progress bar redraws itself with \r, so split on that as well as \n.
        buf, said = b"", []
        while True:
            chunk = job.proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = RE_DOWNLOAD.search(line)
                if m:
                    pct, done, total, speed = m.groups()
                    job.fetch = "Downloading %s%% · %sB of %sB%s" % (
                        pct, done, total, " · " + speed if speed and not speed.startswith("?") else "")
                else:
                    job.log(line)
                    said.append(line)
        code = job.proc.wait()
        if job.status != "preparing":
            return None
        if code != 0:
            raise ValueError(self._download_error(said, code, drive))
        job.fetch = "Unpacking"
        items = [n for n in os.listdir(dl_dir) if not n.startswith(".")]
        if len(items) == 1 and os.path.isfile(os.path.join(dl_dir, items[0])):
            name = self._name_by_content(dl_dir, items[0])
            job.name = name
            return self._unpack(os.path.join(dl_dir, name), name, job.input_dir)
        for name in items:  # a folder: the .blend and its textures
            shutil.move(os.path.join(dl_dir, name), os.path.join(job.input_dir, name))
        blend = self._find_blend(job.input_dir, "No .blend file found in the Drive folder")
        job.name = os.path.basename(blend)
        return blend

    @staticmethod
    def _name_by_content(folder, name):
        """Links like .../download?id=5 save a file without .blend or .zip: look inside."""
        clean = name.split("?")[0] or "download"  # gdown can keep the link's ?query in the name
        if clean != name:
            os.rename(os.path.join(folder, name), os.path.join(folder, clean))
            name = clean
        if name.lower().endswith((".blend", ".zip")):
            return name
        with open(os.path.join(folder, name), "rb") as f:
            head = f.read(4)
        # .blend files start with "BLENDER", or are zstd / gzip compressed; zips start with "PK"
        ext = ".blend" if head.startswith(b"BLEN") or head in (b"\x28\xb5\x2f\xfd",) or head[:2] == b"\x1f\x8b" else \
            ".zip" if head[:2] == b"PK" else ""
        if not ext:
            raise ValueError("The link gave %s, which isn't a .blend or .zip file. Use a link that downloads "
                             "the file itself, not a web page about it." % name)
        os.rename(os.path.join(folder, name), os.path.join(folder, name + ext))
        return name + ext

    @staticmethod
    def _download_error(said, code, drive=True):
        """A short reason from gdown's (multi-line) error output."""
        text = " ".join(said)
        if not drive and ("404" in text or "403" in text):
            return "Nothing could be downloaded from that link (%s). Check it." % ("404" if "404" in text else "403")
        if "Anyone with the link" in text or "many accesses" in text:
            return ("Google Drive wouldn't hand over the file. Share it as 'Anyone with the link' and try "
                    "again. If it is shared already, Google may be limiting downloads of it for a while: "
                    "put a copy in your own Drive folder instead.")
        if "404" in text:
            return "Google Drive says that file doesn't exist. Check the link."
        skip = ("To report issues", "Check FAQ", "Error:", "Using cookies", "warning:", "Retrieving folder")
        reason = next((s for s in said if not s.startswith(skip)), "gdown exited with code %d" % code)
        return ("Google Drive download failed: " if drive else "Download failed: ") + reason

    @staticmethod
    def _find_blend(dest, missing):
        blends = []
        for base, dirs, files in os.walk(dest):
            dirs[:] = [d for d in dirs if d != "__MACOSX"]
            for f in files:
                if f.lower().endswith(".blend"):
                    p = os.path.join(base, f)
                    blends.append((p.count(os.sep), p))
        if not blends:
            raise ValueError(missing)
        return sorted(blends)[0][1]  # the one closest to the top

    @classmethod
    def _unpack(cls, upload_path, filename, dest, keep=False):
        """Put the file in dest and return the .blend to render. keep leaves the source alone."""
        low = filename.lower()
        if low.endswith(".blend"):
            target = os.path.join(dest, os.path.basename(filename))
            (shutil.copy2 if keep else shutil.move)(upload_path, target)
            return target
        if low.endswith(".zip"):
            with zipfile.ZipFile(upload_path) as z:
                root = os.path.realpath(dest)
                for m in z.infolist():
                    out = os.path.realpath(os.path.join(dest, m.filename))
                    if not out.startswith(root + os.sep) and out != root:
                        raise ValueError("Zip contains an unsafe path: %s" % m.filename)
                z.extractall(dest)
            if not keep:
                os.remove(upload_path)
            return cls._find_blend(dest, "No .blend file found inside the zip")
        raise ValueError("Use a .blend file, or a .zip with the .blend and its textures")

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass

    def _worker(self, jobs_queue, handler):
        while True:
            job_id = jobs_queue.get()
            job = self.jobs.get(job_id)
            if not job or job.status != "queued":
                continue
            try:
                handler(job)
            except Exception as e:  # never let one job kill the worker
                job.status = "failed"
                job.error = str(e)
            finally:
                job.proc = None

    def _render(self, job):
        try:
            self._run(job)
        finally:
            job.finished = time.time()

    def _paths(self, job):
        opts_path = os.path.join(job.dir, "options.json")
        with open(opts_path, "w") as f:
            json.dump(job.opts, f)
        return opts_path, os.path.join(job.dir, "info.json")

    def _blender(self, job, cmd, info_path, cwd=None):
        """Run Blender, following its output for progress; returns the exit code."""
        job.log("$ " + " ".join(cmd))
        job.proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace", start_new_session=True,
        )
        out_dir, raw_dir = os.path.realpath(job.output_dir), os.path.realpath(job.raw_dir)
        for raw in job.proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            job.log(line)
            if not job.info and os.path.exists(info_path):
                try:
                    job.info = json.load(open(info_path))
                except ValueError:
                    pass
            m = RE_FRAME.search(line)
            if m:
                job.frame = int(m.group(1))
            m = RE_SAMPLE.search(line) or RE_SAMPLE_ALT.search(line)
            if m:
                job.samples = (int(m.group(1)), int(m.group(2)))
            m = RE_REMAINING.search(line)
            if m:
                job.remaining = m.group(1)
            m = RE_SAVED.search(line)
            if m:
                where = os.path.realpath(os.path.dirname(m.group(1)))
                if where == out_dir:
                    job.saved.append(m.group(1))
                elif where == raw_dir:
                    job.raw.append(m.group(1))
                job.samples = (0, 0)
        return job.proc.wait()

    def _run(self, job):
        job.status = "running"
        job.started = time.time()
        opts_path, info_path = self._paths(job)
        custom = job.custom_compositing()
        cmd = [
            self.blender, "-b", "--factory-startup", job.blend,
            "-P", SETUP_SCRIPT,
            "-o", os.path.join(job.raw_dir if custom else job.output_dir, "frame_####"),
            "-a",
            "--", opts_path, info_path,
        ] + (["raw"] if custom else [])
        code = self._blender(job, cmd, info_path, cwd=os.path.dirname(job.blend))

        if job.status == "cancelled":
            return
        if code != 0 or not (job.raw if custom else job.saved):
            job.status = "failed"
            job.error = self._last_error(job) or "Blender exited with code %d and saved no images" % code
            return
        if custom:
            self._composite(job)
        self._finish(job)

    def _recomposite(self, job):
        try:
            self._composite(job)
            self._finish(job)
        finally:
            job.comp_finished = time.time()  # MP4 included

    def _composite(self, job):
        """Add the website's compositing nodes to the raw frames. Nothing is rendered, so it is quick."""
        job.status = "compositing"
        job.comp_started, job.comp_finished = time.time(), None
        job.version += 1
        job.saved = []
        if job.video:
            job.video = None
            try:
                os.remove(os.path.join(job.output_dir, "video.mp4"))
            except OSError:
                pass
        opts_path, info_path = self._paths(job)
        cmd = [
            self.blender, "-b", "--factory-startup",
            "-P", COMPOSITE_SCRIPT,
            "--", opts_path, info_path, job.raw_dir, job.output_dir,
        ]
        code = self._blender(job, cmd, info_path)
        job.comp_finished = time.time()
        if job.status == "compositing" and (code != 0 or len(job.saved) < len(job.raw)):
            job.status = "failed"
            job.error = self._last_error(job) or "Compositing stopped with code %d" % code

    def _finish(self, job):
        """MP4 if asked for, then done (unless the job failed or was cancelled on the way)."""
        if job.status not in ("running", "compositing"):
            return
        if job.opts.get("video") and len(job.saved) > 1 and job.opts.get("format") != "OPEN_EXR":
            job.status = "encoding"
            self._encode(job)
            if job.status == "cancelled":
                return
        job.status = "done"

    @staticmethod
    def _last_error(job):
        for line in reversed(job.log_lines(300)):
            if "error" in line.lower() or "Traceback" in line:
                return line.split("| ", 1)[-1][:300]
        return ""

    def _encode(self, job):
        ext = IMAGE_EXT.get(job.opts.get("format", "PNG"), ".png")
        fps = job.info.get("fps") or 24
        out = os.path.join(job.output_dir, "video.mp4")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", "%.3f" % fps,
            "-pattern_type", "glob", "-i", os.path.join(job.output_dir, "frame_*" + ext),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-movflags", "+faststart", out,
        ]
        job.log("$ " + " ".join(cmd))
        job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)
        output, _ = job.proc.communicate()
        if output:
            job.log(output.strip())
        if job.proc.returncode == 0 and os.path.exists(out):
            job.video = "video.mp4"
        else:
            job.log("MP4 encoding failed; the frames are still available.")
