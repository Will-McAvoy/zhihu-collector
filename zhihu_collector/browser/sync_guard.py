"""Session-scoped protection against a dead Playwright dispatcher spinning forever.

The installed sync bridge loops while a task is pending even if its dispatcher
greenlet has exited. Keep normal dispatch unchanged; fail a dead dispatcher.
"""
import asyncio
import traceback
from types import MethodType

import greenlet
from playwright.sync_api import Error
from playwright._impl._sync_base import SyncBase, _capture_stack_trace


def safe_sync(self, coro):
    if self._loop.is_closed():
        coro.close()
        raise Error("浏览器事件循环已关闭")
    caller = greenlet.getcurrent()
    task = self._loop.create_task(coro)
    task.__pw_stack__ = _capture_stack_trace()
    task.__pw_stack_trace__ = traceback.extract_stack(limit=10)
    task.add_done_callback(lambda _: caller.switch() if not caller.dead else None)
    while not task.done():
        if self._dispatcher_fiber.dead:
            task.cancel()
            if not self._loop.is_running():
                self._loop.run_until_complete(asyncio.sleep(0))
            raise Error("浏览器调度已退出，已取消等待，不能继续使用此会话。")
        self._dispatcher_fiber.switch()
    asyncio._set_running_loop(self._loop)
    return task.result()


def install(api):
    if isinstance(api, SyncBase):
        api._sync = MethodType(safe_sync, api)
    return api
