"""本地状态：已评论的 BV、已回复的消息、每日配额。

存到 %APPDATA%\\BiliFish\\state.json，原子写入（先写临时文件再替换），
避免程序中途被杀导致 JSON 损坏、去重记录全丢。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import state_path

MAX_COMMENTED = 5000
MAX_REPLIED = 5000
KEEP_DAYS = 60


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class StateStore:
    path: Path = field(default_factory=state_path)
    state: dict[str, Any] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------------
    def load(self) -> "StateStore":
        with self._lock:
            data: dict[str, Any] = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = raw
                except Exception:
                    # 文件坏了：备份一份再重建，不让程序起不来
                    try:
                        self.path.replace(self.path.with_suffix(".broken.json"))
                    except Exception:
                        pass
            self.state = {
                "version": 1,
                "commented": data.get("commented") or {},
                "replied": data.get("replied") or {},
                "daily": data.get("daily") or {},
                "totals": data.get("totals") or {"comments": 0, "replies": 0, "errors": 0},
            }
            return self

    def save(self) -> None:
        with self._lock:
            self._trim()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # 去重
    # ------------------------------------------------------------------
    def is_commented(self, bvid: str) -> bool:
        with self._lock:
            return bvid in self.state["commented"]

    def mark_commented(self, bvid: str, aid: int, text: str, dry_run: bool = False) -> None:
        with self._lock:
            self.state["commented"][bvid] = {
                "at": _now(),
                "aid": aid,
                "text": text,
                "dry_run": bool(dry_run),
            }
            if not dry_run:
                self.state["totals"]["comments"] = int(self.state["totals"].get("comments", 0)) + 1

    def is_replied(self, key: str) -> bool:
        with self._lock:
            return key in self.state["replied"]

    def mark_replied(self, key: str, text: str, target: str = "", dry_run: bool = False) -> None:
        with self._lock:
            self.state["replied"][key] = {
                "at": _now(),
                "text": text,
                "target": target,
                "dry_run": bool(dry_run),
            }
            if not dry_run:
                self.state["totals"]["replies"] = int(self.state["totals"].get("replies", 0)) + 1

    # ------------------------------------------------------------------
    # 每日配额
    # ------------------------------------------------------------------
    def today_count(self) -> int:
        with self._lock:
            return int(self.state["daily"].get(_today(), 0))

    def add_count(self, n: int = 1) -> int:
        with self._lock:
            day = _today()
            self.state["daily"][day] = int(self.state["daily"].get(day, 0)) + n
            return self.state["daily"][day]

    def add_error(self) -> None:
        with self._lock:
            self.state["totals"]["errors"] = int(self.state["totals"].get("errors", 0)) + 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "today": self.today_count(),
                "commented": len(self.state["commented"]),
                "replied": len(self.state["replied"]),
                "comments": int(self.state["totals"].get("comments", 0)),
                "replies": int(self.state["totals"].get("replies", 0)),
                "errors": int(self.state["totals"].get("errors", 0)),
            }

    # ------------------------------------------------------------------
    def _trim(self) -> None:
        commented: dict[str, Any] = self.state["commented"]
        if len(commented) > MAX_COMMENTED:
            for k in list(commented.keys())[: len(commented) - MAX_COMMENTED]:
                commented.pop(k, None)

        replied: dict[str, Any] = self.state["replied"]
        if len(replied) > MAX_REPLIED:
            for k in list(replied.keys())[: len(replied) - MAX_REPLIED]:
                replied.pop(k, None)

        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        daily: dict[str, Any] = self.state["daily"]
        for day in [d for d in daily if d < cutoff]:
            daily.pop(day, None)
