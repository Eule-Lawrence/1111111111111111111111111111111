"""风控识别与随机延迟。

设计原则：
- 识别到风控（412 / -412 / -352 / -509 / 验证码）→ 抛 RiskControlError，由上层直接停机，
  绝不原地重试；
- 普通网络抖动（超时、连接重置）→ 交给上层做有限次重试；
- 每次动作前随机等待，模拟人手的速度。
"""
from __future__ import annotations

import asyncio
import random
import threading
from typing import Optional

from bilibili_api.exceptions import (
    ApiException,
    ExClimbWuzhiException,
    GeetestException,
    NetworkException,
    ResponseCodeException,
)

# HTTP 层面的风控状态码（403 通常是权限问题，不一定是风控，别乱停）
RISK_HTTP_STATUS = {412, 429}
# 接口 code 层面的风控 / 限流代码
RISK_CODES = {-412, -352, -509, -799}
# 这些代码说明再跑也没意义（没登录 / 号被封），需要停机换 Cookie
FATAL_CODES = {-101, -102}

RISK_HINT = (
    "触发 B站风控。请停一会儿（建议 6~24 小时）：\n"
    "  1) 关掉程序，别反复重启重试；\n"
    "  2) 用浏览器打开 bilibili.com 手动刷几分钟，必要时过一下验证码；\n"
    "  3) 把「每次动作前延迟」调大（例如 60~120 秒）、每日上限调小（例如 5~10）；\n"
    "  4) 如果 Cookie 是很久以前抓的，重新扫码登录一次。"
)


class RiskControlError(RuntimeError):
    """检测到风控，必须停机。"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class FatalError(RuntimeError):
    """没登录 / 账号被封之类，重试没有意义，直接停。"""


def is_fatal(exc: BaseException) -> bool:
    if isinstance(exc, FatalError):
        return True
    return isinstance(exc, ResponseCodeException) and int(getattr(exc, "code", 0) or 0) in FATAL_CODES


def is_risk(exc: BaseException) -> bool:
    if isinstance(exc, RiskControlError):
        return True
    if isinstance(exc, NetworkException) and int(getattr(exc, "status", 0) or 0) in RISK_HTTP_STATUS:
        return True
    if isinstance(exc, ResponseCodeException) and int(getattr(exc, "code", 0) or 0) in RISK_CODES:
        return True
    if isinstance(exc, (ExClimbWuzhiException, GeetestException)):
        return True
    text = str(exc)
    if "412" in text and ("风控" in text or "security control" in text):
        return True
    return False


def _strip_html(text: str, limit: int = 160) -> str:
    """B站 412 返回的是一整页 HTML，日志里只留一句人话。"""
    if "<html" in text.lower() or "<!doctype" in text.lower():
        return "B站返回了风控拦截页（HTTP 412）"
    return text.strip().replace("\n", " ")[:limit]


def describe(exc: BaseException) -> str:
    """把异常翻译成给用户看的中文说明。"""
    if isinstance(exc, NetworkException):
        status = int(getattr(exc, "status", 0) or 0)
        if status in RISK_HTTP_STATUS:
            return f"HTTP {status}：{_strip_html(str(exc))}"
        return f"网络请求失败（HTTP {status}）：{_strip_html(str(exc))}"
    if isinstance(exc, ResponseCodeException):
        code = int(getattr(exc, "code", 0) or 0)
        msg = str(getattr(exc, "msg", "") or exc)
        if code == -101:
            return "账号未登录（-101）：Cookie 过期了，请重新扫码登录"
        if code == -102:
            return "账号被封禁（-102）：这个账号不能再用来自动评论了"
        if code in RISK_CODES:
            return f"风控/限流（code {code}）：{msg}"
        return f"接口返回错误（code {code}）：{msg}"
    if isinstance(exc, ApiException):
        return f"B站接口异常：{_strip_html(str(exc))}"
    return f"{type(exc).__name__}: {_strip_html(str(exc), 300)}"


async def sleep_or_stop(seconds: float, stop_event: Optional[threading.Event], tick: float = 0.25) -> bool:
    """可被「停止」打断的 sleep。返回 False 表示收到了停止信号。"""
    remaining = float(seconds)
    while remaining > 0:
        if stop_event is not None and stop_event.is_set():
            return False
        step = min(tick, remaining)
        await asyncio.sleep(step)
        remaining -= step
    return not (stop_event is not None and stop_event.is_set())


async def human_delay(cfg, stop_event: Optional[threading.Event], log=None, label: str = "下一步") -> bool:
    """随机等待 delay_min ~ delay_max 秒。返回 False 表示被停止。"""
    lo = max(0, int(cfg.delay_min))
    hi = max(lo, int(cfg.delay_max))
    seconds = random.uniform(lo, hi) if hi > lo else float(lo)
    if seconds <= 0:
        return not (stop_event is not None and stop_event.is_set())
    if log:
        log(f"等待 {seconds:.0f} 秒后{label}…")
    return await sleep_or_stop(seconds, stop_event)
