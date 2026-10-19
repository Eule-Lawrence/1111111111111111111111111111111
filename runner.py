"""调度：把「拿视频 → 生成评论 → 发出去」「拿消息 → 生成回复 → 回过去」串起来。

贯穿始终的三条规矩：
1. 串行，绝不并发；每个动作前都随机等待；
2. 发成功才落盘去重，失败不标记，下次还能重试；
3. 一碰到风控立刻停机并打印原因，不做原地重试。
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from typing import Callable, Optional

from bilibili_api.exceptions import ApiException

from bili_client import SOURCE_NAMES, BiliClient, InboxItem
from config import Config
from llm_client import LLMClient, LLMError
from risk import (
    RISK_HINT,
    FatalError,
    RiskControlError,
    describe,
    human_delay,
    is_fatal,
    is_risk,
    sleep_or_stop,
)
from storage import StateStore

log = logging.getLogger(__name__)

StatusCallback = Callable[[dict], None]

# 别人问「你主人是谁」的几种常见说法（宁可漏判，也别乱 @）
_MASTER_PATTERNS = [
    re.compile(r"(谁|哪位|什么).{0,4}主人"),
    re.compile(r"主人.{0,4}(谁|哪位|是谁|是什么)"),
    re.compile(r"(谁|哪位).{0,6}(养|管|控制|操控|运营|教)的?你"),
    re.compile(r"背后.{0,4}是谁"),
    re.compile(r"谁的小号"),
]


def _asks_about_master(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in _MASTER_PATTERNS)


class Runner:
    def __init__(self, cfg: Config, store: StateStore, on_status: Optional[StatusCallback] = None):
        self.cfg = cfg
        self.store = store
        self._on_status = on_status or (lambda _info: None)
        self._stop = threading.Event()
        self.fatal_reason = ""
        self.bili = BiliClient(cfg)
        self.llm = LLMClient(cfg)

    # ------------------------------------------------------------------
    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def _status(self, state: str) -> None:
        info = self.store.stats()
        info["state"] = state
        info["limit"] = self.cfg.daily_limit
        try:
            self._on_status(info)
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def run(self) -> None:
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        log.info("=" * 60)
        log.info("启动：%s", started)
        log.info(
            "模式：%s | 每日上限 %d | 每轮视频 %d（来源：%s）| 延迟 %d~%d 秒 | 循环：%s",
            "演练（不发送）" if self.cfg.dry_run else "正式",
            self.cfg.daily_limit,
            self.cfg.videos_per_round,
            SOURCE_NAMES.get(self.cfg.video_source, self.cfg.video_source),
            self.cfg.delay_min,
            self.cfg.delay_max,
            f"开（每 {self.cfg.loop_interval_min} 分钟一轮）" if self.cfg.loop_enabled else "关（跑完一轮就停）",
        )
        try:
            await self._prepare()
            while not self.stopping:
                await self._one_round()
                if self.stopping:
                    break
                if not self.cfg.loop_enabled:
                    log.warning(
                        "⚠️ 没有勾选「循环运行」，本轮结束后程序就停了。"
                        "想让它一直守着（回消息、刷视频），请勾上「循环运行」再点开始。"
                    )
                    break
                minutes = max(1, int(self.cfg.loop_interval_min))
                log.info("本轮结束，休息 %d 分钟后开始下一轮（点「停止」可随时中断）", minutes)
                if not await sleep_or_stop(minutes * 60, self._stop):
                    break
        except RiskControlError as exc:
            log.error("⛔ 已停止：%s", exc)
            log.error(RISK_HINT)
            self.fatal_reason = str(exc)
        except FatalError as exc:
            log.error("⛔ 已停止：%s", exc)
            self.fatal_reason = str(exc)
        except ApiException as exc:
            log.error("B站接口出错，已停止：%s", describe(exc))
            if is_risk(exc):
                log.error(RISK_HINT)
            self.fatal_reason = describe(exc)
        except asyncio.CancelledError:
            log.info("任务已取消")
            raise
        except Exception as exc:  # 兜底，别让线程静默死掉
            log.exception("运行时出现未预期的错误：%s", exc)
            self.fatal_reason = str(exc)
        finally:
            self._status("已停止" if self.stopping else "空闲")
            log.info("程序结束：%s", time.strftime("%H:%M:%S"))

    # ------------------------------------------------------------------
    async def _prepare(self) -> None:
        info = await self.bili.check_login()
        log.info("登录成功：%s（UID %s）", info.get("name") or "?", info.get("mid") or "?")

        if (self.cfg.master_uid or "").strip():
            try:
                name = await self.bili.resolve_master()
            except Exception as exc:
                name = ""
                log.warning("查主人昵称失败（%s），这次先不 @ 主人", exc)
            if name:
                log.info("主人：%s（UID %s），有人问起时会 @ 他", name, self.cfg.master_uid)

        self._status("运行中")

    # ------------------------------------------------------------------
    async def comment_single(self, bvid: str) -> tuple[bool, str]:
        """一键评论：给指定 BV 生成并发送一条评论。

        返回 (是否成功, 给用户看的文字)。失败时抛出 ApiException 由界面显示原因。
        """
        if self.store.is_commented(bvid):
            return False, "这条视频之前已经评论过了（去重记录里有它）"
        if self.store.today_count() >= self.cfg.daily_limit:
            return False, f"今日额度已用完（{self.store.today_count()}/{self.cfg.daily_limit}）"

        info = await self.bili.get_video_info(bvid)
        if not info.aid:
            return False, "拿不到这条视频的 aid，可能已经失效或需要登录"

        hot: list[str] = []
        subtitle = ""
        if self.cfg.use_hot_comments:
            try:
                hot = await self.bili.get_top_comments(info.aid, limit=5)
            except Exception as exc:
                log.warning("拉取热评失败：%s", exc)
        if self.cfg.use_subtitle:
            try:
                subtitle = await self.bili.get_subtitle_text(info.bvid, info.aid, info.cid)
            except Exception as exc:
                log.debug("拉取字幕失败：%s", exc)

        text = await self.llm.make_comment(info, hot_comments=hot, subtitle=subtitle)
        log.info(
            "一键评论｜%s｜%s（参考：热评 %d 条、字幕 %d 字）→ %s",
            info.bvid,
            info.title[:30],
            len(hot),
            len(subtitle),
            text,
        )

        if self.cfg.dry_run:
            return True, f"演练模式，没有真的发送。生成的评论：\n\n{text}"

        await self.bili.post_comment(info, text)
        self.store.mark_commented(info.bvid, info.aid, text)
        self.store.add_count()
        self.store.save()
        log.info("✅ 一键评论完成 %s，今日 %d/%d", info.bvid, self.store.today_count(), self.cfg.daily_limit)
        self._status("运行中")
        return True, text

    async def _one_round(self) -> None:
        if self.store.today_count() >= self.cfg.daily_limit:
            log.warning(
                "今日额度已用完（%d/%d），本轮不做任何事。明天会自动重置。",
                self.store.today_count(),
                self.cfg.daily_limit,
            )
            return

        if self.cfg.comment_enabled:
            await self._do_comments()
        if self.stopping or self.fatal_reason:
            return

        if self.cfg.reply_enabled:
            await self._do_replies()

        log.info(
            "本轮结束｜今日 %d/%d｜累计评论 %d 条、回复 %d 条",
            self.store.today_count(),
            self.cfg.daily_limit,
            self.store.stats()["comments"],
            self.store.stats()["replies"],
        )
        self._status("运行中")

    # ------------------------------------------------------------------
    # 评论
    # ------------------------------------------------------------------
    async def _do_comments(self) -> None:
        if not await human_delay(self.cfg, self._stop, log.info, "拉取推荐视频"):
            return
        try:
            videos = await self.bili.get_videos(self.cfg.videos_per_round)
        except ApiException as exc:
            self._handle_api_error(exc, "拉取视频列表")
            return

        log.info(
            "拿到 %d 个视频（来源：%s，已过滤掉直播/番剧/广告）",
            len(videos),
            SOURCE_NAMES.get(self.cfg.video_source, self.cfg.video_source),
        )
        if not videos:
            log.warning("推荐列表是空的，可能是账号太新或触发了风控")
            return

        for v in videos:
            if self.stopping or self.fatal_reason:
                return
            if self.store.today_count() >= self.cfg.daily_limit:
                log.info("今日额度已满（%d/%d），停止评论", self.store.today_count(), self.cfg.daily_limit)
                return
            if self.store.is_commented(v.bvid):
                log.info("跳过（评论过）：%s", v.bvid)
                continue

            # 1) 详情（要 aid 和简介）
            if not await human_delay(self.cfg, self._stop, log.info, "查看视频详情"):
                return
            try:
                info = await self.bili.get_video_info(v.bvid)
            except ApiException as exc:
                self._handle_api_error(exc, f"获取视频 {v.bvid}")
                if self.fatal_reason:
                    return
                continue
            except Exception as exc:
                log.warning("获取视频 %s 失败：%s", v.bvid, exc)
                continue

            if not info.aid:
                log.warning("视频 %s 没有 aid，跳过", v.bvid)
                continue

            # 2) 可选：参考热评 / CC 字幕，让评论更贴内容
            hot: list[str] = []
            subtitle = ""
            if (self.cfg.use_hot_comments or self.cfg.use_subtitle) and info.aid:
                await asyncio.sleep(random.uniform(1.5, 4.0))  # 两个读请求之间别贴太紧
                if self.cfg.use_hot_comments:
                    try:
                        hot = await self.bili.get_top_comments(info.aid, limit=5)
                    except ApiException as exc:
                        self._handle_api_error(exc, "拉取热评")
                        hot = []
                    except Exception as exc:
                        log.warning("拉取热评失败：%s", exc)
                if self.cfg.use_subtitle:
                    try:
                        subtitle = await self.bili.get_subtitle_text(info.bvid, info.aid, info.cid)
                    except ApiException as exc:
                        self._handle_api_error(exc, "拉取字幕")
                        subtitle = ""
                    except Exception as exc:
                        log.debug("拉取字幕失败：%s", exc)

            # 3) 生成评论
            try:
                text = await self.llm.make_comment(info, hot_comments=hot, subtitle=subtitle)
            except LLMError as exc:
                log.error("生成评论失败：%s", exc)
                self.store.add_error()
                continue

            ctx = ""
            if hot or subtitle:
                ctx = f"（参考：热评 {len(hot)} 条、字幕 {len(subtitle)} 字）"
            log.info("生成评论｜%s｜%s%s → %s", info.bvid, info.title[:30], ctx, text)

            # 3) 发送
            if self.cfg.dry_run:
                log.info("[演练] 不发送，也不写入去重记录")
                continue

            if not await human_delay(self.cfg, self._stop, log.info, "发评论"):
                return
            try:
                res = await self.bili.post_comment(info, text)
            except ApiException as exc:
                self._handle_api_error(exc, f"评论 {v.bvid}")
                if self.fatal_reason:
                    return
                self.store.add_error()
                continue
            except Exception as exc:
                log.error("评论 %s 失败：%s", v.bvid, exc)
                self.store.add_error()
                continue

            rpid = _rpid(res)
            self.store.mark_commented(info.bvid, info.aid, text)
            self.store.add_count()
            self.store.save()
            log.info("✅ 已评论 %s（rpid=%s）今日 %d/%d", v.bvid, rpid or "?", self.store.today_count(), self.cfg.daily_limit)
            self._status("运行中")

    # ------------------------------------------------------------------
    # 回复
    # ------------------------------------------------------------------
    async def _do_replies(self) -> None:
        if not await human_delay(self.cfg, self._stop, log.info, "拉取消息"):
            return
        try:
            items = await self.bili.fetch_inbox()
        except ApiException as exc:
            self._handle_api_error(exc, "拉取消息")
            return
        except Exception as exc:
            log.warning("拉取消息失败：%s", exc)
            return

        cutoff = 0.0
        if self.cfg.reply_max_age_days > 0:
            cutoff = time.time() - self.cfg.reply_max_age_days * 86400

        try:
            master_uid = int(self.cfg.master_uid.strip() or 0)
        except ValueError:
            master_uid = 0
        master_name = self.bili.master_name if master_uid else ""

        pending: list[InboxItem] = []
        expired = 0
        for it in items:
            if not it.is_video:
                continue
            if it.user_mid and it.user_mid == self.bili.self_uid:
                continue  # 自己发的，别自问自答
            if self.store.is_replied(it.key):
                continue
            if cutoff and it.ts and it.ts < cutoff:
                expired += 1
                continue
            if not it.reply_id and it.aid and it.user_mid:
                # @ 的消息有时不带 rpid，去视频最新评论里捞一次
                try:
                    rpid = await self.bili.find_recent_comment_rpid(it.aid, it.user_mid, it.text)
                except ApiException as exc:
                    self._handle_api_error(exc, "查找被 @ 的评论")
                    if self.fatal_reason:
                        return
                    rpid = 0
                except Exception:
                    rpid = 0
                if rpid:
                    it.reply_id = rpid
                    log.info("消息 %s 没带 rpid，已从视频最新评论里定位到 %s", it.key, rpid)
            if not it.reply_id:
                log.warning("消息 %s 找不到对方评论的 rpid，回不了，先跳过", it.key)
                continue
            pending.append(it)

        at_count = sum(1 for x in pending if x.kind == "at")
        extra = f"，忽略 {expired} 条超过 {self.cfg.reply_max_age_days} 天的旧消息" if expired else ""
        log.info(
            "未读：@ %d 条、评论回复 %d 条；需要处理 %d 条%s",
            at_count,
            len(pending) - at_count,
            len(pending),
            extra,
        )
        if not pending:
            return

        # 主人的消息优先回
        if master_uid:
            pending.sort(key=lambda x: 0 if x.user_mid == master_uid else 1)

        for it in pending[: self.cfg.max_replies_per_round]:
            if self.stopping or self.fatal_reason:
                return
            if self.store.today_count() >= self.cfg.daily_limit:
                log.info("今日额度已满，停止回复")
                return

            is_master = bool(master_uid) and it.user_mid == master_uid
            hint = ""
            mention = None
            if _asks_about_master(it.text) and not is_master:
                if master_name:
                    mention = (master_name, master_uid)
                    hint = f"对方在问你的主人是谁。你主人是「{master_name}」，可以得意地承认，但别透露别的信息。"
                else:
                    hint = "对方在问你的主人是谁。你没被谁收养，就说自己是野生的、自己游过来的。"

            try:
                text = await self.llm.make_reply(it, is_master=is_master, extra_hint=hint)
            except LLMError as exc:
                log.error("生成回复失败：%s", exc)
                self.store.add_error()
                continue

            if mention:
                text = f"@{mention[0]} {text}"

            who = it.user_name or f"UID{it.user_mid}"
            tag = "@我" if it.kind == "at" else "评论"
            log.info(
                "生成回复｜%s 的%s%s → %s",
                who,
                tag,
                "（主人）" if is_master else "",
                text,
            )

            if self.cfg.dry_run:
                log.info("[演练] 不发送，也不写入去重记录")
                continue

            if not await human_delay(self.cfg, self._stop, log.info, "回复消息"):
                return
            try:
                res = await self.bili.reply(it, text, mention=mention)
            except ApiException as exc:
                self._handle_api_error(exc, f"回复 {it.key}")
                if self.fatal_reason:
                    return
                self.store.add_error()
                continue
            except Exception as exc:
                log.error("回复 %s 失败：%s", it.key, exc)
                self.store.add_error()
                continue

            self.store.mark_replied(it.key, text, target=who)
            self.store.add_count()
            self.store.save()
            log.info("✅ 已回复 %s（rpid=%s）今日 %d/%d", who, _rpid(res) or "?", self.store.today_count(), self.cfg.daily_limit)
            self._status("运行中")

    # ------------------------------------------------------------------
    def _handle_api_error(self, exc: ApiException, where: str) -> None:
        if is_risk(exc):
            self.fatal_reason = describe(exc)
            log.error("在「%s」时触发风控：%s", where, describe(exc))
            log.error(RISK_HINT)
            raise RiskControlError(describe(exc))
        if is_fatal(exc):
            self.fatal_reason = describe(exc)
            log.error("在「%s」时出错：%s", where, describe(exc))
            raise FatalError(describe(exc))
        log.error("在「%s」时接口出错：%s", where, describe(exc))
        self.store.add_error()


def _rpid(res: dict) -> int:
    if not isinstance(res, dict):
        return 0
    rpid = res.get("rpid")
    if not rpid and isinstance(res.get("reply"), dict):
        rpid = res["reply"].get("rpid")
    try:
        return int(rpid or 0)
    except (TypeError, ValueError):
        return 0
