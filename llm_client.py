"""DeepSeek 调用（OpenAI 兼容接口）。

只负责「给一段视频信息 → 一句像人的评论」「给一条回复 → 一句像人的回复」。
所有输出都会再清洗一遍（去引号/去链接/去 @/截断），避免模型偶尔不听话。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from config import Config

if TYPE_CHECKING:  # 只为了类型提示，避免运行时循环导入
    from bili_client import InboxItem, VideoInfo

log = logging.getLogger(__name__)

COMMENT_LIMIT = 50
REPLY_LIMIT = 30

_FALLBACK_COMMENTS = [
    "这条我先码住，回头慢慢看(￣▽￣)",
    "有点东西，先收藏了",
    "刷到就是缘分，蹲个后续",
]
_FALLBACK_REPLIES = [
    "好家伙，被你发现了(￣▽￣)",
    "确实确实，我也是这么想的",
    "哈哈哈懂的都懂",
]


class LLMError(RuntimeError):
    """模型调用失败（网络、额度、鉴权等），由上层决定是否继续。"""


class LLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        key = (cfg.api_key or "").strip()
        if not key:
            raise LLMError("还没有填写 DeepSeek API Key")
        self._client = AsyncOpenAI(
            api_key=key,
            base_url=(cfg.base_url or "https://api.deepseek.com/v1").strip(),
            timeout=60.0,
            max_retries=1,
        )
        self.last_usage: dict[str, Any] = {}

    # ------------------------------------------------------------------
    async def make_comment(
        self,
        info: "VideoInfo",
        hot_comments: list[str] | None = None,
        subtitle: str = "",
    ) -> str:
        lines = [
            f"视频标题：{info.title or '（无）'}",
            f"视频简介：{info.short_desc or '（无）'}",
            f"UP主：{info.up_name or '（未知）'}",
            f"分区：{info.tname or '（未知）'}",
            f"播放量：{info.view}",
        ]
        if hot_comments:
            lines.append("")
            lines.append("热评参考（帮你理解大家在聊什么，别照抄、别复读）：")
            lines.extend(f"- {c[:60]}" for c in hot_comments[:5])
        if subtitle:
            lines.append("")
            lines.append("字幕片段（自动生成，可能有错字，只用来理解内容）：")
            lines.append(subtitle[:600])
        lines.append("")
        lines.append("给这个视频写一条评论吧。")
        prompt = "\n".join(lines)
        raw = await self._chat(self.cfg.system_prompt, prompt)
        text = clean_text(raw, COMMENT_LIMIT)
        if not text:
            log.warning("模型返回空评论，使用兜底文案")
            text = _pick(_FALLBACK_COMMENTS)
        return text

    async def make_reply(self, item: "InboxItem", is_master: bool = False, extra_hint: str = "") -> str:
        lines = [f"视频：{item.title or item.bvid or '（未知）'}"]
        if item.user_name:
            lines.append(f"回复我的人：{item.user_name}")
        if item.text:
            lines.append(f"对方说：{item.text[:200]}")
        else:
            lines.append("对方 @ 了我，但没拿到具体内容，就随口接一句")
        if is_master:
            lines.append("注意：这条是我主人（账号主人）发来的，可以亲近一点、撒个娇，但别泄露隐私。")
        if extra_hint:
            lines.append(extra_hint)
        lines.append("回他一句吧。")
        raw = await self._chat(self.cfg.reply_prompt, "\n".join(lines))
        text = clean_text(raw, REPLY_LIMIT)
        if not text:
            log.warning("模型返回空回复，使用兜底文案")
            text = _pick(_FALLBACK_REPLIES)
        return text

    async def ping(self) -> str:
        """设置页里的「测试 AI」：发一句最短的请求确认 Key 和网络。"""
        raw = await self._chat("你是一个测试助手，只回答两个字。", "请回复：正常", max_tokens=16)
        return raw.strip() or "(空响应)"

    # ------------------------------------------------------------------
    async def _chat(self, system: str, user: str, max_tokens: int | None = None) -> str:
        messages = [
            {"role": "system", "content": system or "你是一条可爱的B站大肥鱼。"},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.chat.completions.create(
                    model=self.cfg.model or "deepseek-chat",
                    messages=messages,
                    temperature=float(self.cfg.temperature),
                    max_tokens=int(max_tokens or self.cfg.max_tokens),
                    stream=False,
                )
                choice = resp.choices[0] if resp.choices else None
                content = (choice.message.content if choice and choice.message else "") or ""
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    self.last_usage = {
                        "prompt": getattr(usage, "prompt_tokens", 0),
                        "completion": getattr(usage, "completion_tokens", 0),
                    }
                return content.strip()
            except AuthenticationError as exc:
                raise LLMError("DeepSeek API Key 无效（401），请检查 Key 是否填错或已删除") from exc
            except RateLimitError as exc:
                raise LLMError("DeepSeek 限流或余额不足（429），请稍后再试或充值") from exc
            except APITimeoutError as exc:
                last_error = exc
                if attempt == 0:
                    log.warning("DeepSeek 超时，3 秒后重试一次")
                    await _sleep(3)
                    continue
                raise LLMError("DeepSeek 请求超时，请检查网络（可能需要代理）") from exc
            except APIConnectionError as exc:
                last_error = exc
                if attempt == 0:
                    log.warning("连不上 DeepSeek，3 秒后重试一次")
                    await _sleep(3)
                    continue
                raise LLMError(
                    "连不上 DeepSeek（api.deepseek.com）。如果开了代理，请确认代理可用；"
                    "或者在设置里把 Base URL 换成可访问的地址"
                ) from exc
            except APIError as exc:
                raise LLMError(f"DeepSeek 接口错误：{exc}") from exc
        raise LLMError(f"DeepSeek 调用失败：{last_error}")


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

_QUOTE_CHARS = "\"'“”‘’「」『』 "
_LABEL_RE = re.compile(r"^(评论|回复|回答|输出|文案)\s*[:：]\s*")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_AT_RE = re.compile(r"@[^\s@]{1,20}")
_SPACE_RE = re.compile(r"\s+")
_OPEN_TO_CLOSE = {"（": "）", "(": ")", "【": "】", "「": "」", "《": "》"}


def clean_text(raw: str, limit: int) -> str:
    if not raw:
        return ""
    text = raw.strip()
    # 去掉 markdown 代码块 / 引用符号
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _LABEL_RE.sub("", text).strip()
    text = _URL_RE.sub("", text)
    text = _AT_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    text = text.strip(_QUOTE_CHARS).strip()
    text = text.strip(_QUOTE_CHARS).strip()
    if len(text) > limit:
        text = text[:limit]
        text = text.rstrip("，,。.、；;：:…—- ")
        # 截断后半个括号/引号不好看，去掉悬空的开括号
        for opener, closer in _OPEN_TO_CLOSE.items():
            if text.count(opener) > text.count(closer):
                text = text[: text.rfind(opener)].rstrip("，,。.、；; ")
    return text.strip()


def _pick(items: list[str]) -> str:
    import random

    return random.choice(items)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
