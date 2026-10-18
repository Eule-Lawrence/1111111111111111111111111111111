"""登录：扫码自动拿 Cookie（推荐）＋ 手动粘贴 Cookie 兜底。

⚠️ 2025 年底起 B站改了 web 扫码登录流程：
    旧流程：扫码成功 → poll 返回的 url 里直接带 SESSDATA / bili_jct
    新流程：扫码成功 → poll 只给一个 ticket → 必须再访问一次 crossDomain，
            服务器才会用 Set-Cookie 把真正的 Cookie 发下来
bilibili-api 17.4.2 还按旧流程解析，所以拿到的 Cookie 是空的。
这里干脆自己实现一遍，两种流程都兼容。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlparse

import httpx
import qrcode

from config import Config, app_home
from platform_utils import open_path

log = logging.getLogger(__name__)

LOGIN_PAGE = "https://www.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# 新流程用来「兑换」Cookie 的跨域地址，几个域名都试一遍
CROSS_DOMAIN = (
    "https://passport.biligame.com/x/passport-login/web/crossDomain",
    "https://passport.bilibili.com/x/passport-login/web/crossDomain",
    "https://passport.biligame.com/crossDomain",
)

COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID")


@dataclass
class LoginResult:
    sessdata: str = ""
    bili_jct: str = ""
    buvid3: str = ""
    buvid4: str = ""
    dedeuserid: str = ""

    def apply_to(self, cfg: Config) -> Config:
        if self.sessdata:
            cfg.sessdata = self.sessdata
        if self.bili_jct:
            cfg.bili_jct = self.bili_jct
        if self.buvid3:
            cfg.buvid3 = self.buvid3
        if self.buvid4:
            cfg.buvid4 = self.buvid4
        if self.dedeuserid:
            cfg.dedeuserid = self.dedeuserid
        return cfg

    @property
    def ok(self) -> bool:
        return bool(self.sessdata and self.bili_jct)


STATE_TEXT = {
    "scan": "等待扫码…请用 B站手机客户端扫上面的二维码",
    "confirm": "已扫码，请在手机上点「确认登录」",
    "timeout": "二维码已过期，请点「刷新二维码」",
    "done": "登录成功！",
}


def _collect_cookies(resp: httpx.Response) -> dict[str, str]:
    """从响应（含重定向链）的 Set-Cookie 里收集 Cookie。"""
    out: dict[str, str] = {}
    for r in list(resp.history) + [resp]:
        try:
            headers = r.headers.get_list("set-cookie")
        except Exception:
            headers = []
        for raw in headers:
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    out[k] = v
    for k, v in resp.cookies.items():
        out.setdefault(k, v)
    return out


class QrLoginFlow:
    """一次扫码登录会话。GUI 和命令行共用。"""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self.qr_key = ""
        self.qr_url = ""
        self.png_path: Optional[Path] = None
        self.flow_kind = ""  # "old" / "ticket"，仅用于日志

    async def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
            )
        return self._client

    async def generate(self) -> Path:
        c = await self._c()
        r = await c.get(QR_GENERATE)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        self.qr_url = str(data.get("url") or "")
        self.qr_key = str(data.get("qrcode_key") or "")
        if not (self.qr_url and self.qr_key):
            raise RuntimeError("申请二维码失败：B站返回里没有 url / qrcode_key")

        self.png_path = app_home() / "login_qrcode.png"
        qrcode.make(self.qr_url).save(str(self.png_path))
        return self.png_path

    @property
    def terminal(self) -> str:
        # 不再画字符二维码，统一用图片，Windows 控制台颜色不靠谱
        return ""

    async def poll(self) -> tuple[str, Optional[LoginResult]]:
        if not self.qr_key:
            return "scan", None
        c = await self._c()
        r = await c.get(QR_POLL, params={"qrcode_key": self.qr_key})
        data = (r.json() or {}).get("data") or {}
        code = data.get("code")

        if code == 86101:
            return "scan", None
        if code == 86090:
            return "confirm", None
        if code == 86038:
            return "timeout", None

        # code == 0：确认登录
        return "done", await self._collect(r, data)

    async def _collect(self, poll_resp: httpx.Response, data: dict) -> Optional[LoginResult]:
        cookies = _collect_cookies(poll_resp)
        url = str(data.get("url") or "")
        query = dict(parse_qsl(urlparse(url).query))

        if query.get("SESSDATA"):
            self.flow_kind = "old"
            cookies.update({k: query[k] for k in COOKIE_NAMES if query.get(k)})
            log.info("扫码成功（旧流程，url 直接带 Cookie）")
        elif query.get("ticket"):
            self.flow_kind = "ticket"
            log.info("扫码成功（新流程：ticket），正在兑换 Cookie…")
            cookies.update(await self._exchange(query["ticket"], query.get("gourl") or LOGIN_PAGE))
        else:
            self.flow_kind = "unknown"
            log.warning("扫码成功，但 url 里既没有 Cookie 也没有 ticket：%s", url[:100])

        sess = cookies.get("SESSDATA", "")
        jct = cookies.get("bili_jct", "")
        dede = cookies.get("DedeUserID", "")
        if not (sess and jct):
            log.error(
                "扫码成功但没拿到 SESSDATA/bili_jct（已拿到的字段：%s）",
                ", ".join(sorted(cookies)) or "无",
            )
            return None

        buvid3, buvid4 = await self._buvid()
        return LoginResult(
            sessdata=sess,
            bili_jct=jct,
            buvid3=buvid3,
            buvid4=buvid4,
            dedeuserid=dede,
        )

    async def _exchange(self, ticket: str, gourl: str) -> dict[str, str]:
        c = await self._c()
        cookies: dict[str, str] = {}
        for base in CROSS_DOMAIN:
            try:
                r = await c.get(base, params={"ticket": ticket, "gourl": gourl})
            except Exception as exc:
                log.debug("兑换失败（%s）：%s", base, exc)
                continue
            cookies.update(_collect_cookies(r))
            # 重定向过程中 Set-Cookie 可能已经被客户端收进 jar，一并取出来
            try:
                cookies.update({k: v for k, v in c.cookies.items() if v})
            except Exception:
                pass
            if cookies.get("SESSDATA"):
                log.info("兑换成功（%s）", urlparse(base).netloc)
                break
        return cookies

    async def _buvid(self) -> tuple[str, str]:
        try:
            c = await self._c()
            r = await c.get(SPI_URL)
            d = (r.json() or {}).get("data") or {}
            return str(d.get("b_3") or ""), str(d.get("b_4") or "")
        except Exception:
            return "", ""


# ---------------------------------------------------------------------------
# 命令行登录：python main.py login
# ---------------------------------------------------------------------------

async def login_cli(timeout: int = 180) -> int:
    flow = QrLoginFlow()
    print("正在生成二维码…")
    png = await flow.generate()
    print(f"二维码已保存：{png}")

    try:
        open_path(png)  # 直接用系统看图工具打开，比终端二维码靠谱
        print("已帮你打开二维码图片。")
    except Exception:
        print("请打开上面的图片文件扫码。")

    print("用手机 B站客户端扫码，扫完记得在手机上点「确认登录」。\n")

    deadline = time.time() + timeout
    last_state = ""
    while time.time() < deadline:
        state, result = await flow.poll()
        if state != last_state:
            print(f"[{STATE_TEXT.get(state, state)}]")
            last_state = state
        if state == "done":
            if result is None:
                print("扫码成功了，但没换到 Cookie。请重跑一次；还不行就改用界面里的「从剪贴板导入 Cookie」。")
                return 4
            cfg = Config.load()
            result.apply_to(cfg).save()
            print("\n登录成功，Cookie 已写入配置：", cfg.masked())
            return 0
        if state == "timeout":
            print("二维码过期了，重新运行一次 `python main.py login` 即可。")
            return 2
        await asyncio.sleep(2)

    print("等待超时，请重新运行。")
    return 3


# ---------------------------------------------------------------------------
# 兜底：手动粘贴 / 浏览器
# ---------------------------------------------------------------------------

_COOKIE_KEYS = {
    "sessdata": "sessdata",
    "bili_jct": "bili_jct",
    "buvid3": "buvid3",
    "buvid4": "buvid4",
    "dedeuserid": "dedeuserid",
}


def parse_cookie_text(text: str) -> LoginResult:
    """从一整串 Cookie 里解析出需要的字段。

    支持 `SESSDATA=xxx; bili_jct=yyy`、以及每行一个的格式。
    """
    result = LoginResult()
    for raw in (text or "").replace("\n", ";").split(";"):
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        field = _COOKIE_KEYS.get(key.strip().lower())
        if field:
            setattr(result, field, value.strip().strip('"'))
    return result


def open_login_page() -> None:
    """用系统浏览器打开 B站，方便手动登录后复制 Cookie。"""
    webbrowser.open(LOGIN_PAGE)
