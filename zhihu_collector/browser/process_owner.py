"""Terminate only descendants of this Playwright driver, retaining Windows handles.

Handles and creation times prevent PID reuse from targeting another application.
No command line, cookie or browser profile contents are recorded.
"""
from __future__ import annotations

import ctypes as C
import os
import threading
import time
from ctypes import wintypes as W


class _Entry(C.Structure):
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD), ("name", W.WCHAR * 260)]


class DriverOwner:
    def __init__(self, process):
        self.process = process
        self.pid = process.pid
        self.lock = threading.Lock()
        self.handles = {}
        if os.name == "nt":
            k = self.k = C.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
            k.OpenProcess.restype = W.HANDLE
            k.CloseHandle.argtypes = [W.HANDLE]
            k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
            k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
            k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
            k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
            k.CreateToolhelp32Snapshot.restype = W.HANDLE
            k.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(_Entry)]
            k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(_Entry)]
            self._retain(self.pid, 0)

    def _retain(self, pid, earliest, latest=None):
        handle = self.k.OpenProcess(0x1000 | 0x100000 | 1, False, pid)
        if not handle:
            return
        times = [W.FILETIME() for _ in range(4)]
        if self.k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
            created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
            if created >= earliest and (latest is None or created <= latest):
                self.handles[pid] = (handle, created)
                return
        self.k.CloseHandle(handle)

    def capture(self):
        if os.name != "nt":
            return
        # Reject a PID reused after this snapshot started, even when the old
        # process's parent was ours. Retaining its handle then prevents reuse.
        snapshot_started = time.time_ns() // 100 + 116444736000000000
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == C.c_void_p(-1).value:
            return
        entries = []
        entry = _Entry()
        entry.size = C.sizeof(entry)
        try:
            ok = self.k.Process32FirstW(snapshot, C.byref(entry))
            while ok:
                entries.append((entry.pid, entry.parent))
                ok = self.k.Process32NextW(snapshot, C.byref(entry))
        finally:
            self.k.CloseHandle(snapshot)
        while True:
            before = len(self.handles)
            for pid, parent in entries:
                if pid not in self.handles and parent in self.handles:
                    handle, created = self.handles[parent]
                    if self.k.WaitForSingleObject(handle, 0) == 258:
                        self._retain(pid, created, snapshot_started)
            if before == len(self.handles):
                break

    def terminate(self):
        with self.lock:
            if os.name == "nt":
                self.capture()
                # Descendants were inserted after parents. Retained handles avoid PID races.
                for handle, _ in reversed(list(self.handles.values())):
                    if self.k.WaitForSingleObject(handle, 0) == 258:
                        self.k.TerminateProcess(handle, 1)
                # TerminateProcess is asynchronous; allow file handles to be released.
                deadline = time.monotonic() + 2
                for handle, _ in self.handles.values():
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    self.k.WaitForSingleObject(handle, remaining)
            elif self.process.returncode is None:
                self.process.kill()

    def release(self):
        with self.lock:
            if os.name == "nt":
                for handle, _ in self.handles.values():
                    self.k.CloseHandle(handle)
                self.handles.clear()
