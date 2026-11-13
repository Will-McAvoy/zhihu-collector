"""Application-owned scratch directories with live leases and deferred cleanup.

Ownership records and lease files live outside the directory being removed, so
a partially failed removal never loses the evidence needed to retry it safely.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading
import time

log = logging.getLogger('zhihu_collector.temporary')
_KINDS = {'browser': 'session-', 'pdf': 'render-', 'print': 'print-'}
_jobs = queue.Queue()
_queued = set()
_mutex = threading.Lock()
_thread = None


class LeaseBusy(OSError):
    pass


class Lease:
    def __init__(self, path: Path, *, shared: bool, create: bool = False):
        if path.is_symlink() or path.resolve().parent != path.parent.resolve():
            raise ValueError('拒绝使用指向外部位置的临时目录锁')
        self.file = path.open('x+b' if create else 'r+b')
        if create:
            self.file.write(b'0'); self.file.flush()
        self.path = path
        try:
            if os.name == 'nt':
                import msvcrt
                class Overlapped(ctypes.Structure):
                    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
                self.overlap = Overlapped()
                self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                self.kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                                  wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
                self.handle = msvcrt.get_osfhandle(self.file.fileno())
                if not self.kernel.LockFileEx(self.handle, 1 | (0 if shared else 2), 0, 1, 0, ctypes.byref(self.overlap)):
                    raise LeaseBusy('临时目录仍由活动进程使用')
            else:
                import fcntl
                fcntl.flock(self.file, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise

    def close(self):
        # Closing the handle releases the lock, including on process termination.
        self.file.close()


def _paths(root: Path, name: str):
    return root / name, root / f'.{name}.owner.json', root / f'.{name}.lease'


def _owned(root: Path, name: str, kind: str) -> bool:
    if kind not in _KINDS or not re.fullmatch(re.escape(_KINDS[kind]) + r'[a-zA-Z0-9_-]+', name):
        return False
    target, marker, lock = _paths(root, name)
    try:
        for path in (target, marker, lock):
            if path.is_symlink() or path.resolve().parent != root.resolve():
                return False
            if path.exists() and getattr(path.stat(), 'st_file_attributes', 0) & 0x400:
                return False
        if marker.stat().st_size > 4096:
            return False
        record = json.loads(marker.read_text(encoding='utf-8'))
        return record == {'owner': 'zhihu_collector', 'schema': 1, 'kind': kind, 'name': name}
    except (OSError, ValueError, TypeError):
        return False


def cleanup_once(root: Path, name: str, kind: str) -> bool:
    root = root.resolve()
    if not _owned(root, name, kind):
        return False
    target, marker, lock = _paths(root, name)
    try:
        lease = Lease(lock, shared=False)
    except (OSError, ValueError):
        return False
    try:
        if not _owned(root, name, kind):
            return False
        # The resolved absolute target must be a direct child of this cache root.
        if target.resolve().parent != root or target.is_symlink():
            return False
        if target.exists():
            shutil.rmtree(target)
        marker.unlink(missing_ok=True)
    finally:
        lease.close()
    try:
        lock.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _cleaner():
    while True:
        root, name, kind = key = _jobs.get()
        try:
            for delay in (0, .2, .8):
                if delay:
                    time.sleep(delay)
                try:
                    if cleanup_once(root, name, kind):
                        log.debug('临时文件清理完成：%s', name)
                        break
                    break  # Live lease or invalid record: leave it for its owner.
                except OSError as exc:
                    error = f'{type(exc).__name__}: {getattr(exc, "winerror", None) or exc.errno}'
                    if delay == .8:
                        log.info('临时文件暂被占用，已保留归属记录，下次启动重试：%s（%s）', name, error)
        finally:
            with _mutex:
                _queued.discard(key)
            _jobs.task_done()


def schedule_cleanup(root: Path, name: str, kind: str):
    global _thread
    key = (root.resolve(), name, kind)
    with _mutex:
        if key in _queued:
            return
        _queued.add(key)
        _jobs.put(key)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_cleaner, name='owned-cache-cleanup', daemon=True)
            _thread.start()


def sweep(root: Path, kind: str, limit: int = 32):
    if not root.is_dir():
        return
    for marker in list(root.glob(f'.{_KINDS[kind]}*.owner.json'))[:limit]:
        name = marker.name[1:-len('.owner.json')]
        if _owned(root.resolve(), name, kind):
            schedule_cleanup(root, name, kind)


def wait_for_cleanup(timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while _jobs.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.02)
    return not _jobs.unfinished_tasks


class ManagedTemp:
    def __init__(self, root: Path, kind: str):
        self.root, self.kind = root.resolve(), kind
        self.root.mkdir(parents=True, exist_ok=True)
        sweep(self.root, kind)
        path = Path(tempfile.mkdtemp(prefix=_KINDS[kind], dir=self.root))
        self.name = str(path)
        _, marker, lock = _paths(self.root, path.name)
        self.lease = None
        try:
            self.lease = Lease(lock, shared=True, create=True)
            marker.write_text(json.dumps({'owner': 'zhihu_collector', 'schema': 1, 'kind': kind, 'name': path.name}), encoding='utf-8')
        except BaseException:
            if self.lease is not None:
                self.lease.close()
            # Constructor failure occurs before any browser data is written.
            for file in (marker,lock):
                try:file.unlink(missing_ok=True)
                except OSError:pass
            try:path.rmdir()
            except OSError:pass
            raise

    def cleanup(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None
            schedule_cleanup(self.root, Path(self.name).name, self.kind)


def attach(path: Path, kind: str) -> Lease:
    if not _owned(path.parent.resolve(), path.name, kind):
        raise ValueError('临时目录归属记录无效')
    lease = Lease(_paths(path.parent, path.name)[2], shared=True)
    if not _owned(path.parent.resolve(), path.name, kind):
        lease.close()
        raise ValueError('临时目录已经退出使用')
    return lease
