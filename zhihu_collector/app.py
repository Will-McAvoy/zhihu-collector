"""桌面程序入口。"""

from __future__ import annotations

import hashlib
import sys

from PySide6.QtCore import QByteArray
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from zhihu_collector import __app_name__, __version__
from zhihu_collector.config import data_dir, runtime_cache_dir
from zhihu_collector.utils.logger import get_logger

logger = get_logger("app")


def _server_name() -> str:
    path_str = str(data_dir().resolve()).lower()
    digest = hashlib.md5(path_str.encode("utf-8")).hexdigest()[:12]
    return f"zhihu_collector_instance_{digest}"


def main() -> int:
    from zhihu_collector.browser.owned_temp import sweep
    for cache_root in {data_dir(), runtime_cache_dir()}:
        sweep(cache_root / 'browser-temp', 'browser')
        sweep(cache_root / 'pdf-work', 'pdf')
        sweep(cache_root / 'pdf-print', 'print')
    from zhihu_collector.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    server_id = _server_name()
    socket = QLocalSocket()
    socket.connectToServer(server_id)
    if socket.waitForConnected(500):
        logger.info("检测到已有实例在运行，正在唤醒已有窗口...")
        socket.write(QByteArray(b"ACTIVATE\n"))
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        QMessageBox.information(
            None,
            "知乎内容搜集器",
            "检测到知乎内容搜集器已在运行中，已为您切换到已有窗口。",
        )
        return 0

    server = QLocalServer()
    QLocalServer.removeServer(server_id)
    if not server.listen(server_id):
        logger.warning("无法监听单实例服务端口：%s", server.errorString())

    window = MainWindow()

    def _on_new_connection():
        client = server.nextPendingConnection()
        if client:
            client.waitForReadyRead(500)
            msg = bytes(client.readAll()).decode("utf-8", errors="ignore").strip()
            if msg == "ACTIVATE":
                window.showNormal()
                window.raise_()
                window.activateWindow()
            client.disconnectFromServer()

    server.newConnection.connect(_on_new_connection)

    # 仅由主实例在启动时安全恢复上次未正常结束的崩溃任务
    try:
        window.database.recover_interrupted_tasks()
    except Exception as exc:
        logger.warning("恢复中断任务时出现异常：%s", exc)

    window.show()
    logger.info("主界面已启动（v%s）", __version__)
    result = app.exec()
    server.close()
    return result
