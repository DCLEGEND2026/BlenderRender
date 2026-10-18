"""Live compositing previews: one Blender kept running (blender_preview.py) that the
Compositor page asks for a new image on every change. Starting Blender takes about
a second, but once it runs a preview takes a few hundredths of a second."""
import json
import os
import queue
import subprocess
import threading
import uuid
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
PREVIEW_SCRIPT = os.path.join(HERE, "blender_preview.py")


class Previewer:
    def __init__(self, blender_bin):
        self.blender = blender_bin
        self.lock = threading.Lock()  # one request at a time
        self.proc = None
        self.answers = queue.Queue()
        self.log = deque(maxlen=200)

    def _start(self):
        self.proc = subprocess.Popen(
            [self.blender, "-b", "--factory-startup", "-P", PREVIEW_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors="replace", start_new_session=True,
        )
        self.answers = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.answers), daemon=True).start()
        self._wait("ready", 120)

    def _read(self, proc, answers):
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@preview "):
                answers.put(line[len("@@preview "):])
            elif line:
                self.log.append(line)
        answers.put(None)  # Blender quit

    def _wait(self, req_id, timeout):
        while True:
            try:
                line = self.answers.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("The preview took too long")
            if line is None:
                raise RuntimeError("Blender stopped: " + " | ".join(list(self.log)[-3:]))
            got, _, rest = line.partition(" ")
            if got == req_id:
                return rest

    def _stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None

    def render(self, raw_path, info, graph, out_dir, max_size):
        """Composite raw_path with graph; returns the PNG's path, or None if nothing reaches the output."""
        req_id = uuid.uuid4().hex[:12]
        out = os.path.join(out_dir, "preview_" + req_id)
        with self.lock:
            for attempt in (1, 2):  # a crashed Blender is started again once
                try:
                    if not self.proc or self.proc.poll() is not None:
                        self._start()
                    self.proc.stdin.write(json.dumps({
                        "id": req_id, "raw": raw_path, "info": info, "graph": graph,
                        "max_size": max_size, "out": out}) + "\n")
                    self.proc.stdin.flush()
                    result = self._wait(req_id, 120)
                    break
                except (RuntimeError, OSError) as e:
                    self._stop()
                    if attempt == 2:
                        raise RuntimeError(str(e))
        if result.startswith("ok"):
            return out + ".png"
        if result == "empty":
            return None
        raise RuntimeError(result.partition(" ")[2] or result)
