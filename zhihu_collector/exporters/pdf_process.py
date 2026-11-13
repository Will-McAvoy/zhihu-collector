"""PDF renderer isolated from the collector's browser and event dispatcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
from collections import deque

from zhihu_collector.browser.process_owner import DriverOwner
from zhihu_collector.browser.owned_temp import ManagedTemp, attach, sweep
from zhihu_collector.errors import CollectorError, TaskStopped
from zhihu_collector.utils.task_diagnostics import pdf_event, pdf_stage_label


class PdfProcess:
    IDLE_TIMEOUT = 100

    def __init__(self, controller=None, on_progress=None, on_stage=None):
        from zhihu_collector.config import runtime_cache_dir
        root = runtime_cache_dir() / "pdf-work"
        root.mkdir(parents=True, exist_ok=True)
        self.work = ManagedTemp(root, 'pdf')
        self.controller = controller
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.process = self.owner = None
        self.replies = queue.Queue()
        self.stderr_tail = deque(maxlen=20)

    def _start(self):
        args = [sys.executable]
        if not getattr(sys, "frozen", False):
            args += ["-u", "-m", "zhihu_collector"]
        args += ["--pdf-worker", self.work.name]
        options = {}
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            options["startupinfo"] = startup
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, **options)
        self.owner = DriverOwner(self.process)
        self.replies = queue.Queue()
        self.stderr_tail = deque(maxlen=20)
        replies, process = self.replies, self.process

        def read():
            try:
                for line in process.stdout:
                    replies.put(json.loads(line))
            except (OSError, ValueError):
                pass
            finally:
                replies.put({"error": "PDF 渲染进程意外退出"})
        threading.Thread(target=read, daemon=True).start()
        tail = self.stderr_tail
        def read_errors():
            try:
                for line in process.stderr:
                    tail.append(line.rstrip()[:600])
            except (OSError, ValueError):
                pass
        threading.Thread(target=read_errors, daemon=True).start()

    def write(self, markdown, destination, title, frontmatter):
        job_id = uuid4().hex
        job = Path(self.work.name) / "job.json"
        job.write_text(json.dumps({"markdown": markdown, "destination": str(destination.resolve()),
            "title": title, "frontmatter": frontmatter}, ensure_ascii=False), encoding="utf-8")
        for attempt in range(2):
            started = time.monotonic()
            stage, part, total = 'starting', 0, 0
            last_activity = started
            try:
                if self.process is None:
                    self._start()
                pdf_event('start', job=job_id, attempt=attempt+1, worker=self.process.pid, target=destination.name)
                self.process.stdin.write("render\n")
                self.process.stdin.flush()
                # Merged documents render one answer at a time to bound browser
                # memory. Keep a finite overall deadline as well as each RPC limit.
                # A productive large document must not expire at the old fixed
                # 900-second ceiling. Each finite segment still has RPC limits.
                seconds = 120 + (markdown.count("{#item-") + 1) * 100
                deadline = time.monotonic() + seconds
                while True:
                    if self.controller:
                        checkpoint_started = time.monotonic()
                        self.controller.checkpoint()
                        paused_time = time.monotonic() - checkpoint_started
                        deadline += paused_time
                        last_activity += paused_time
                    if time.monotonic() >= deadline:
                        raise CollectorError(f"PDF 独立渲染超过 {seconds} 秒，已回收渲染进程，采集内容保留。")
                    if time.monotonic() - last_activity >= self.IDLE_TIMEOUT:
                        raise CollectorError(f'PDF 阶段“{pdf_stage_label(stage,part,total)}”超过 {self.IDLE_TIMEOUT:g} 秒没有进展')
                    try:
                        reply = self.replies.get(timeout=.1)
                    except queue.Empty:
                        continue
                    last_activity = time.monotonic()
                    if 'stage' in reply:
                        stage = str(reply['stage'])
                        part, total = int(reply.get('part',0)), int(reply.get('total',0))
                        if self.owner is not None and (stage == 'prepare' or (stage == 'assets' and part % 20 == 1)):
                            # Retain the browser's handles while its worker is
                            # alive, so an abrupt worker exit cannot orphan it.
                            self.owner.capture()
                        pdf_event('stage', job=job_id, attempt=attempt+1, stage=stage, part=part, total=total,
                                  elapsed=round(last_activity-started,3))
                        if self.on_stage:
                            self.on_stage(stage,part,total)
                        continue
                    if "progress" in reply:
                        if self.on_progress:
                            self.on_progress(*reply["progress"])
                        continue
                    if reply.get("ok"):
                        pdf_event('completed', job=job_id, attempt=attempt+1, elapsed=round(time.monotonic()-started,3))
                        return destination
                    raise CollectorError(reply.get("error") or "PDF 渲染未完成")
            except TaskStopped:
                self._stop()
                raise
            except (OSError, CollectorError) as exc:
                self._stop()
                pdf_event('failed' if attempt else 'retry', job=job_id, attempt=attempt+1, stage=stage,
                          part=part, total=total, elapsed=round(time.monotonic()-started,3),
                          error=(str(exc) or type(exc).__name__)[:1500], stderr=list(self.stderr_tail))
                if attempt:
                    raise

    def _stop(self):
        if self.owner is not None:
            self.owner.capture()
            self.owner.terminate()
            self.owner.release()
            self.owner = None
        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass
            self.process = None
        # Browser profiles have their own leases. Keep them beside PDF work,
        # rather than nesting a second profile tree under a long job path.
        sweep(self.work.root.parent / 'browser-temp', 'browser')

    def close(self):
        try:
            if self.owner is not None:
                self.owner.capture()
            cancelled = self.controller is not None and self.controller.stopped
            if self.process is not None and not cancelled:
                try:
                    self.process.stdin.write("close\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._stop()
        finally:
            try:
                self.work.cleanup()
            except OSError:
                pass


def worker_main(directory):
    # Only protocol replies go to stdout. Never load the collector's auth state.
    def pipe_stream(kind, mode):
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(kind & 0xffffffff)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == "r" else os.O_WRONLY)
        return os.fdopen(fd, mode, encoding="utf-8", buffering=1)

    # PyInstaller's windowed executable sets sys.std* to None even with pipes.
    protocol = sys.stdout or pipe_stream(-11, "w")
    commands = sys.stdin or pipe_stream(-10, "r")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr
    root = Path(directory).resolve()
    work_lease = attach(root, 'pdf')
    import zhihu_collector.config as config
    config.user_data_root = lambda: root / "runtime"
    os.environ["ZHIHU_COLLECTOR_RUNTIME_DIR"] = str(root.parent.parent)
    from zhihu_collector.browser.playwright_manager import BrowserSession
    from zhihu_collector.exporters.pdf_exporter import PdfExporter
    session = exporter = None
    try:
        for command in commands:
            if command.strip() == "close":
                break
            try:
                if session is None:
                    protocol.write(json.dumps({'stage':'starting'})+'\n');protocol.flush()
                    session = BrowserSession(offline=True)
                    session.start()
                    def report_progress(done, total):
                        protocol.write(json.dumps({"progress": [done, total]}) + "\n")
                        protocol.flush()
                    def report_stage(stage, part=0, total=0):
                        protocol.write(json.dumps({'stage':stage,'part':part,'total':total})+'\n')
                        protocol.flush()
                    exporter = PdfExporter(session=session, on_progress=report_progress,
                                           on_stage=report_stage, cache_dir=root/'page-cache')
                job = json.loads((root / "job.json").read_text(encoding="utf-8"))
                exporter.write(job["markdown"], Path(job["destination"]), title=job["title"], frontmatter=job["frontmatter"])
                protocol.write(json.dumps({"ok": True}) + "\n")
                protocol.flush()
            except Exception as exc:
                protocol.write(json.dumps({"error": (str(exc) or type(exc).__name__)[:1500]}, ensure_ascii=False) + "\n")
                protocol.flush()
                if session:
                    session.close()
                    session = None
    finally:
        if session:
            session.close()
        work_lease.close()
    return 0
