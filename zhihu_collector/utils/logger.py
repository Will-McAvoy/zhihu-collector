"""应用日志。文件日志与 GUI 可读的同一份记录。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from zhihu_collector.config import ensure_runtime_dirs, log_dir

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("zhihu_collector")
    if _CONFIGURED:
        return logger
    ensure_runtime_dirs()
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir() / "app.log",
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    setup_logging()
    if not name or name == "zhihu_collector":
        return logging.getLogger("zhihu_collector")
    return logging.getLogger(f"zhihu_collector.{name}")
