"""平台差异都收在这里：字体、主题、打开文件、窗口图标、单实例。

Windows 和 macOS 共用同一套业务代码，这里做最小的适配：
    - 字体：Windows 用微软雅黑，mac 用苹方
    - 主题：Windows 用 vista，mac 用 aqua
    - 打开文件/文件夹：Windows 用 os.startfile，mac 用 open
    - 单实例：Windows 用命名互斥量，mac 用 fcntl 文件锁
    - 托盘：只在 Windows 上启用（pystray 在 mac 上跟 Tk 抢主线程，不可靠）
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path

from config import app_home, resource_path

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ---------------------------------------------------------------------------
# 字体 / 主题
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    FONT = ("Microsoft YaHei UI", 9)
    FONT_SMALL = ("Microsoft YaHei UI", 8)
    FONT_BIG = ("Microsoft YaHei UI", 11, "bold")
    FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
    FONT_MONO = ("Consolas", 9)
    PREFERRED_THEME = "vista"
elif IS_MAC:
    FONT = ("PingFang SC", 12)
    FONT_SMALL = ("PingFang SC", 10)
    FONT_BIG = ("PingFang SC", 14, "bold")
    FONT_TITLE = ("PingFang SC", 24, "bold")
    FONT_MONO = ("Menlo", 11)
    PREFERRED_THEME = "aqua"
else:
    FONT = ("DejaVu Sans", 9)
    FONT_SMALL = ("DejaVu Sans", 8)
    FONT_BIG = ("DejaVu Sans", 11, "bold")
    FONT_TITLE = ("DejaVu Sans", 18, "bold")
    FONT_MONO = ("DejaVu Sans Mono", 9)
    PREFERRED_THEME = "clam"


def apply_theme(root: tk.Misc) -> None:
    """挑一个当前平台好看的主题；没有就用 clam 兜底。"""
    try:
        from tkinter import ttk

        style = ttk.Style(root)
        names = style.theme_names()
        for want in (PREFERRED_THEME, "aqua", "clam", "default"):
            if want in names:
                style.theme_use(want)
                return
    except Exception:
        pass


def set_window_icon(root: tk.Tk) -> None:
    if IS_WINDOWS:
        try:
            root.iconbitmap(str(resource_path("icon.ico")))
            return
        except Exception:
            pass
    try:
        img = tk.PhotoImage(file=str(resource_path("assets/avatar.png")))
        root.iconphoto(True, img)
        root._icon_photo = img  # 防止被回收
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 打开文件 / 文件夹
# ---------------------------------------------------------------------------

def open_path(path: str | Path) -> None:
    p = str(path)
    if IS_WINDOWS:
        os.startfile(p)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------

MUTEX_NAME = "Local\\BiliFish_SingleInstance_v1"
ERROR_ALREADY_EXISTS = 183

_lock_handle = None  # 进程活多久，锁就持有多久


def _flag_path() -> Path:
    return app_home() / "show.request"


def request_show() -> None:
    try:
        _flag_path().write_text("show", encoding="utf-8")
    except Exception:
        pass


def consume_show_request() -> bool:
    p = _flag_path()
    try:
        if p.exists():
            p.unlink()
            return True
    except Exception:
        pass
    return False


def ensure_single_instance() -> bool:
    """True = 我是第一个，继续；False = 已经有一个，叫它出来并退出。"""
    return _win_lock() if IS_WINDOWS else _posix_lock()


def _win_lock() -> bool:
    global _lock_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = kernel32.GetLastError()
        if not handle:
            return True
        _lock_handle = handle
        if err == ERROR_ALREADY_EXISTS:
            request_show()
            return False
        consume_show_request()
        return True
    except Exception:
        return True


def _posix_lock() -> bool:
    global _lock_handle
    try:
        import fcntl

        fh = open(app_home() / "instance.lock", "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            request_show()
            return False
        _lock_handle = fh
        consume_show_request()
        return True
    except Exception:
        return True
