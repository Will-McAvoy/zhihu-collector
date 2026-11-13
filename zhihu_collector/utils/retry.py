"""带指数退避的重试。验证码和登录失效不在这里重试。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class RetryGaveUp(Exception):
    def __init__(self, attempts: int, last_error: BaseException):
        super().__init__(f"重试 {attempts} 次后仍失败：{last_error}")
        self.attempts = attempts
        self.last_error = last_error


def call_with_retry(
    func: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay_ms: int = 1500,
    retry_on: tuple[type[BaseException], ...] = (OSError, TimeoutError, ConnectionError),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts 必须 >= 1")
    last_error: BaseException | None = None
    for index in range(attempts):
        try:
            return func()
        except retry_on as exc:
            last_error = exc
            if index == attempts - 1:
                break
            delay = (base_delay_ms / 1000) * (2**index) + random.uniform(0, 0.4)
            sleep(delay)
    assert last_error is not None
    raise RetryGaveUp(attempts, last_error) from last_error
