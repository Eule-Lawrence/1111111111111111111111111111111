"""打包入口：按当前系统自动分派。

    Windows → build_windows.py → dist/windows/大肥鱼.exe
    macOS   → build_mac.py     → dist/mac/大肥鱼.app

PyInstaller 不能交叉编译：Windows 的 exe 只能在 Windows 上打，
mac 的 .app 只能在 Mac 上打。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _force_utf8() -> None:
    """CI / 老控制台可能是 cp1252，中文 print 会直接崩，这里统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main() -> int:
    _force_utf8()
    if sys.platform == "darwin":
        from build_mac import main as mac_main

        return mac_main()
    if sys.platform.startswith("win"):
        from build_windows import main as win_main

        return win_main()

    print("只支持 Windows 和 macOS 打包；Linux 下可以直接源码运行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
