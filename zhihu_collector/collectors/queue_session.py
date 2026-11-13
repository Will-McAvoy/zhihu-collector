"""串行队列的领取和结束。界面只触发，不在这里启动第二套采集。"""

from __future__ import annotations

import threading
import time

from zhihu_collector.database.catalog import claim_next_queue_item, finish_queue_item
from zhihu_collector.errors import CollectionScopeReady, LoginExpiredError, VerificationRequiredError


class QueueSession:
    def __init__(self, database):
        self.database = database
        self.hold = False
        self._busy = False
        self._current_item_id: int | None = None
        self._lock = threading.Lock()
        self.max_in_flight = 0
        self._seen = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def try_begin(self, *, include_interrupted: bool = False):
        with self._lock:
            if self._busy or self.hold:
                return None
            item = claim_next_queue_item(self.database, include_interrupted=include_interrupted)
            if item is None:
                return None
            self._busy = True
            self._current_item_id = int(item["id"])
            self._seen += 1
            self.max_in_flight = max(self.max_in_flight, 1 if self._busy else 0)
            return item

    def release_claim(self, item_id: int) -> None:
        from zhihu_collector.database.catalog import update_queue_item

        with self.database._lock:
            self.database._conn.execute(
                "UPDATE queue_items SET status = 'waiting', started_at = NULL WHERE id = ? AND status = 'running'",
                (int(item_id),),
            )
            self.database._conn.commit()
        with self._lock:
            if self._current_item_id is None or self._current_item_id == int(item_id):
                self._busy = False
                self._current_item_id = None

    def finish(self, item_id: int, status: str, error: str = "", result: dict | None = None) -> bool:
        updated = finish_queue_item(self.database, item_id, status, error, result)
        with self._lock:
            if self._current_item_id is None or self._current_item_id == int(item_id):
                self._busy = False
                self._current_item_id = None
        return updated


def drive_queue(session: QueueSession, runner, *, include_interrupted: bool = False) -> list[int]:
    """测试和离线调度用。任意时刻只领取一条；其他调用者会等到当前项结束。"""
    done: list[int] = []
    first = include_interrupted
    spins = 0
    while True:
        item = session.try_begin(include_interrupted=first)
        first = False
        if item is None:
            if session.hold and not session.busy:
                return done
            if session.busy:
                spins += 1
                if spins > 1000:
                    return done
                time.sleep(0.005)
                continue
            return done
        spins = 0
        item_id = int(item["id"])
        try:
            outcome = runner(item) or {}
            status = outcome.get("status") or "success"
            if status in {"needs_login", "needs_verify", "awaiting_confirm"}:
                session.hold = True
            session.finish(item_id, status, outcome.get("error") or "", outcome)
            done.append(item_id)
            if session.hold or status in {"needs_login", "needs_verify", "awaiting_confirm"}:
                return done
        except CollectionScopeReady as exc:
            session.hold = True
            session.finish(item_id, "awaiting_confirm", str(exc))
            done.append(item_id)
            return done
        except LoginExpiredError as exc:
            session.hold = True
            session.finish(item_id, "needs_login", str(exc))
            done.append(item_id)
            return done
        except VerificationRequiredError as exc:
            session.hold = True
            session.finish(item_id, "needs_verify", str(exc))
            done.append(item_id)
            return done
        except Exception as exc:
            session.finish(item_id, "failed", str(exc) or exc.__class__.__name__)
            done.append(item_id)
