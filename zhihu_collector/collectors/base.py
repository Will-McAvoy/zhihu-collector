"""后台任务的暂停、继续和停止。GUI 与命令行共用。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from zhihu_collector.errors import TaskStopped


@dataclass
class Progress:
    status: str = "idle"
    found: int = 0
    completed: int = 0
    current_title: str = ""
    message: str = ""
    percent: float | None = None
    failed: int = 0
    scanned: int | None = None
    declared_total: int | None = None
    saved_new: int = 0
    saved_updated: int = 0
    reused_existing: int = 0
    integrity: str = ""


def progress_labels(progress: Progress) -> tuple[str, str]:
    """采集过程中把实际扫描和网站参考总数分开写。"""
    failed = f"（失败 {progress.failed}）" if progress.failed else ""
    if progress.scanned is not None or progress.declared_total:
        scanned = progress.scanned or 0
        if progress.declared_total:
            found = f"已扫描：{scanned}篇，网站参考{progress.declared_total}条"
        else:
            found = f"已扫描：{scanned}篇"
        done = (
            f"本次新增：{progress.saved_new}，更新：{progress.saved_updated}，"
            f"复用：{progress.reused_existing}{failed}"
        )
        return found, done
    return f"已发现：{progress.found}", f"已完成：{progress.completed}{failed}"


class TaskController:
    """pause 时阻塞在 checkpoint；stop 时抛出 TaskStopped。"""

    def __init__(self):
        self._pause = threading.Event()
        self._pause.set()
        self._stop = threading.Event()

    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def is_stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set() and not self._stop.is_set()

    def checkpoint(self) -> None:
        while not self._pause.is_set():
            if self._stop.is_set():
                raise TaskStopped()
            if not self._pause.wait(0.2):
                continue
        if self._stop.is_set():
            raise TaskStopped()
