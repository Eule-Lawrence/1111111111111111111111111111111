"""日志：控制台 + 按天切分的文件 + 给界面用的队列。"""
from __future__ import annotations

import logging
import os
import queue
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler

from config import logs_dir

_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup(level: int = logging.INFO, console: bool = True) -> str:
    """初始化根日志，返回日志文件路径。重复调用不会叠加 handler。"""
    global _configured
    root = logging.getLogger()
    path = logs_dir() / f"bot-{datetime.now():%Y%m%d}.log"

    if _configured:
        return path
    _configured = True

    # 打包成 --windowed 后 sys.stderr 是 None，某些库写日志会炸，这里兜一层
    if not console:
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))

    root.setLevel(level)
    fmt = logging.Formatter(_FORMAT, _DATEFMT)

    fh = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    # 这些库太吵，降噪
    for noisy in ("httpx", "httpcore", "openai", "bilibili-api-request", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return str(path)


class QueueHandler(logging.Handler):
    """把日志塞进队列，供 Tkinter 主线程取走显示。"""

    def __init__(self, q: "queue.Queue[str]", fmt: logging.Formatter | None = None):
        super().__init__()
        self.queue = q
        self.setFormatter(fmt or logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(self.format(record))
        except Exception:
            pass
