"""采集过程中需要向用户说明的异常。"""


class CollectorError(Exception):
    """可展示给用户的采集错误。"""


class LoginExpiredError(CollectorError):
    def __init__(self, message: str = "知乎登录状态已失效，请重新登录。"):
        super().__init__(message)


class VerificationRequiredError(CollectorError):
    def __init__(
        self,
        message: str = "知乎要求进行安全验证。程序已暂停，不会尝试自动处理验证码。请在调试模式或重新登录后手动完成验证，再继续任务。",
    ):
        super().__init__(message)


class TaskStopped(CollectorError):
    def __init__(self, message: str = "任务已停止。"):
        super().__init__(message)


class UnsupportedUrlError(CollectorError):
    def __init__(self, url: str):
        super().__init__(f"无法识别的知乎链接：{url}")
        self.url = url


class CollectionScopeReady(CollectorError):
    """收藏夹清单已读取，等待用户确认精确导出范围。"""

    def __init__(self, plan):
        super().__init__("收藏夹清单已读取，请确认导出范围。")
        self.plan = plan
