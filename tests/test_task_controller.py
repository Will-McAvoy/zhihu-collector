import threading

import pytest

from zhihu_collector.errors import TaskStopped
from zhihu_collector.collectors.base import TaskController


def test_stop_interrupts_checkpoint():
    controller = TaskController()
    controller.stop()
    with pytest.raises(TaskStopped):
        controller.checkpoint()


def test_pause_blocks_until_resume():
    controller = TaskController()
    controller.pause()
    reached = threading.Event()

    def worker():
        controller.checkpoint()
        reached.set()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(0.3)
    assert not reached.is_set()
    controller.resume()
    thread.join(1)
    assert reached.is_set()
