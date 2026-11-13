"""联网采集和离线导出共用一把锁，避免两条路径同时写同一批文件。"""

from __future__ import annotations

import threading


class RunGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.owner = ""

    def acquire(self, owner: str) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        self.owner = owner
        return True

    def release(self) -> None:
        self.owner = ""
        try:
            self._lock.release()
        except RuntimeError:
            pass

    @property
    def busy(self) -> bool:
        return self._lock.locked()


GATE = RunGate()
BUSY_MESSAGE = "已有采集或导出正在进行，请稍后再试。"
