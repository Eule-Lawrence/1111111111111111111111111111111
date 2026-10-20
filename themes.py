"""界面主题：4 套浅色配色，每套配一张头部背景和一张右侧立绘。

为什么只有浅色：Tk 的 ttk 控件（输入框、标签页、下拉框）在 Windows 原生主题下
没法可靠地改底色，强行做深色会变成「一半黑一半白」，所以只做浅色系。
"""
from __future__ import annotations

THEMES: dict[str, dict[str, str]] = {
    "blue": {
        "label": "海盐蓝",
        "bg": "#F2F7F9",
        "shell": "#E7F1F9",
        "title": "#173B57",
        "sub": "#4D7390",
        "mark": "#7C9AB3",
        "status": "#5A7A92",
        "header": "assets/theme_blue.png",
        "panel": "assets/panel_blue.png",
    },
    "pink": {
        "label": "樱花粉",
        "bg": "#FFF7FB",
        "shell": "#FFE9F3",
        "title": "#5C2237",
        "sub": "#8A5A6C",
        "mark": "#B98CA0",
        "status": "#8A5A6C",
        "header": "assets/theme_pink.png",
        "panel": "assets/panel_pink.png",
    },
    "green": {
        "label": "薄荷绿",
        "bg": "#F4FBF7",
        "shell": "#E5F6EE",
        "title": "#14463A",
        "sub": "#4C7A6D",
        "mark": "#86AFA2",
        "status": "#4C7A6D",
        "header": "assets/theme_green.png",
        "panel": "assets/panel_green.png",
    },
    "sand": {
        "label": "暖沙",
        "bg": "#FBF7F0",
        "shell": "#F3E9DB",
        "title": "#4A3524",
        "sub": "#7A6650",
        "mark": "#B3A08A",
        "status": "#7A6650",
        "header": "assets/theme_sand.png",
        "panel": "assets/panel_sand.png",
    },
}

DEFAULT = "blue"

LABELS = {k: v["label"] for k, v in THEMES.items()}
LABEL_TO_KEY = {v: k for k, v in LABELS.items()}


def get(key: str) -> dict[str, str]:
    return THEMES.get((key or "").strip(), THEMES[DEFAULT])
