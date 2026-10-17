"""Render jobs: one queue, one Blender process at a time (Colab has one GPU)."""
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP_SCRIPT = os.path.join(HERE, "blender_setup.py")

RE_FRAME = re.compile(r"Fra:\s*(\d+)")
RE_SAMPLE = re.compile(r"Sample (\d+)\s*/\s*(\d+)")
RE_SAMPLE_ALT = re.compile(r"Rendering (\d+) / (\d+) samples")
RE_REMAINING = re.compile(r"Remaining:\s*([\d:.]+)")
RE_SAVED = re.compile(r"Saved: '(.+?)'")

IMAGE_EXT = {"PNG": ".png", "JPEG": ".jpg", "OPEN_EXR": ".exr"}


class Job:
    def __init__(self, jobs_dir, name, opts):
        self.id = uuid.uuid4().hex[:10]
        self.dir = os.path.join(jobs_dir, self.id)
        self.input_dir = os.path.join(self.dir, "input")
        self.output_dir = os.path.join(self.dir, "output")
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)
        self.name = name
        self.opts = opts
        self.blend = None
        self.status = "queued"  # queued, running, encoding, done, failed, cancelled
        self.error = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.info = {}
        self.frame = None
        self.samples = (0, 0)
        self.remaining = ""
        self.saved = []
        self.video = None
        self.proc = None
        # Keep the start of the log (setup, GPU, warnings) and a rolling tail.
        self.log_head = []
        self.log_tail = deque(maxlen=300)

    # ---- progress ------------------------------------------------------------
    def total_frames(self):
        if not self.info:
            return 0
        return max(1, self.info["frame_end"] - self.info["frame_start"] + 1)

    def progress(self):
        if self.status in ("done", "encoding"):
            return 1.0
        total = self.total_frames()
        if not total:
            return 0.0
        part = self.samples[0] / self.samples[1] if self.samples[1] else 0.0
        return min(1.0, (len(self.saved) + part) / total)

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
            "frames_done": len(self.saved),
            "frames_total": self.total_frames(),
            "samples": list(self.samples),
            "remaining": self.remaining,
            "progress": round(self.progress(), 4),
            "outputs": [os.path.basename(p) for p in self.saved],
            "video": self.video,
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
        self.lock = threading.Lock()
        os.makedirs(jobs_dir, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- public API ----------------------------------------------------------
    def create(self, upload_path, filename, opts):
        job = Job(self.jobs_dir, filename, opts)
        try:
            job.blend = self._unpack(upload_path, filename, job.input_dir)
        except Exception as e:
            shutil.rmtree(job.dir, ignore_errors=True)
            raise ValueError(str(e))
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
        self.queue.put(job.id)
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def all(self):
        return [self.jobs[i] for i in self.order if i in self.jobs]

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if not job:
            return False
        if job.status == "queued":
            job.status = "cancelled"
        elif job.status in ("running", "encoding") and job.proc:
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
    @staticmethod
    def _unpack(upload_path, filename, dest):
        """Put the upload in dest and return the .blend to render."""
        low = filename.lower()
        if low.endswith(".blend"):
            target = os.path.join(dest, os.path.basename(filename))
            shutil.move(upload_path, target)
            return target
        if low.endswith(".zip"):
            with zipfile.ZipFile(upload_path) as z:
                root = os.path.realpath(dest)
                for m in z.infolist():
                    out = os.path.realpath(os.path.join(dest, m.filename))
                    if not out.startswith(root + os.sep) and out != root:
                        raise ValueError("Zip contains an unsafe path: %s" % m.filename)
                z.extractall(dest)
            os.remove(upload_path)
            blends = []
            for base, dirs, files in os.walk(dest):
                dirs[:] = [d for d in dirs if d != "__MACOSX"]
                for f in files:
                    if f.lower().endswith(".blend"):
                        p = os.path.join(base, f)
                        blends.append((p.count(os.sep), p))
            if not blends:
                raise ValueError("No .blend file found inside the zip")
            return sorted(blends)[0][1]  # the one closest to the top of the zip
        raise ValueError("Upload a .blend file, or a .zip with the .blend and its textures")

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

    def _worker(self):
        while True:
            job_id = self.queue.get()
            job = self.jobs.get(job_id)
            if not job or job.status != "queued":
                continue
            try:
                self._run(job)
            except Exception as e:  # never let one job kill the worker
                job.status = "failed"
                job.error = str(e)
            finally:
                job.finished = time.time()
                job.proc = None

    def _run(self, job):
        job.status = "running"
        job.started = time.time()
        opts_path = os.path.join(job.dir, "options.json")
        info_path = os.path.join(job.dir, "info.json")
        with open(opts_path, "w") as f:
            json.dump(job.opts, f)

        cmd = [
            self.blender, "-b", "--factory-startup", job.blend,
            "-P", SETUP_SCRIPT,
            "-o", os.path.join(job.output_dir, "frame_####"),
            "-a",
            "--", opts_path, info_path,
        ]
        job.log("$ " + " ".join(cmd))
        job.proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(job.blend),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace", start_new_session=True,
        )
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
            if m and os.path.realpath(os.path.dirname(m.group(1))) == os.path.realpath(job.output_dir):
                job.saved.append(m.group(1))
                job.samples = (0, 0)
        code = job.proc.wait()

        if job.status == "cancelled":
            return
        if code != 0 or not job.saved:
            job.status = "failed"
            job.error = self._last_error(job) or "Blender exited with code %d and saved no images" % code
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
