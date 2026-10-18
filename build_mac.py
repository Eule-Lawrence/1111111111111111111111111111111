"""macOS 打包：产出 dist/mac/大肥鱼.app

⚠️ 必须在 macOS 上运行（PyInstaller 不能交叉编译）。
用法：
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/pip install pyinstaller
    .venv/bin/python build_mac.py
或：
    python3 build.py     # 会自动分派到这里

说明：
- 图标用 macOS 自带的 sips + iconutil 从 assets/icon_1024.png 生成 .icns；
- 托盘在 macOS 上不启用（pystray 跟 Tk 抢主线程），点 × 直接退出；
- 数据目录：~/Library/Application Support/BiliFish
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "mac"
WORK = ROOT / "build" / "mac"
ICNS = ROOT / "build" / "icon.icns"
BUNDLE_ID = "com.yuziyuyu.bilifish"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="打包 macOS 版")
    ap.add_argument(
        "--universal",
        action="store_true",
        help="打通用二进制（Intel + Apple 芯片都能跑，需要 universal2 的 Python，体积翻倍）",
    )
    return ap.parse_args()


def preflight() -> bool:
    """先把最常见的两个坑挑出来，别等 PyInstaller 跑到一半才报错。"""
    ok = True

    try:
        import tkinter

        print(f"Python：{platform.python_version()}（{platform.machine()}）")
        print(f"Tk：{tkinter.TkVersion}")
    except Exception as exc:
        ok = False
        print(f"✗ 这个 Python 没有可用的 Tk（{exc}）")
        print("  用 python.org 的安装包最省事；Homebrew 的要装 python-tk@3.x")

    try:
        import PyInstaller  # noqa: F401

        print(f"PyInstaller：{PyInstaller.__version__}")
    except Exception:
        ok = False
        print("✗ 没装 PyInstaller：pip install pyinstaller")

    return ok


def make_icns() -> Path | None:
    """用系统自带的 sips / iconutil 把 1024 PNG 转成 .icns。"""
    src = ROOT / "assets" / "icon_1024.png"
    if not src.exists():
        print(f"缺少图标源文件：{src}，这次不设图标")
        return None
    if ICNS.exists():
        return ICNS

    iconset = WORK / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True, exist_ok=True)

    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    try:
        for size, name in sizes:
            subprocess.run(
                ["sips", "-z", str(size), str(size), str(src), "--out", str(iconset / name)],
                check=True,
                capture_output=True,
            )
        ICNS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)],
            check=True,
            capture_output=True,
        )
        return ICNS
    except Exception as exc:
        print(f"生成 .icns 失败（{exc}），这次不设图标")
        return None


def build_args(icon: Path | None, universal: bool = False) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "BiliFish",
        "--osx-bundle-identifier",
        BUNDLE_ID,
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--collect-data",
        "bilibili_api",
        "--hidden-import",
        "bilibili_api.clients.HTTPXClient",
        "--hidden-import",
        "dotenv",
        "--hidden-import",
        "PIL.Image",
        "--hidden-import",
        "PIL.ImageFile",
        "--hidden-import",
        "qrcode.image.pil",
        "--exclude-module",
        "aiohttp",
        "--exclude-module",
        "curl_cffi",
        "--exclude-module",
        "pystray",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "numpy",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "scipy",
        "--log-level",
        "WARN",
        # macOS 的路径分隔符是冒号
        "--add-data",
        f"{ROOT / 'assets'}:assets",
        "--add-data",
        f"{ROOT / 'icon.ico'}:.",
    ]
    if icon:
        args += ["--icon", str(icon)]
    if universal:
        args += ["--target-arch", "universal2"]
    args.append(str(ROOT / "main.py"))
    return args


def _force_utf8() -> None:
    """CI / 老控制台可能是 cp1252，中文 print 会直接崩，这里统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main() -> int:
    _force_utf8()
    if sys.platform != "darwin":
        print("这是 macOS 打包脚本，必须在 Mac 上运行。")
        return 1

    args = parse_args()
    if not preflight():
        print("\n上面标 ✗ 的先解决，再来打包。")
        return 1

    print("\n开始打包 macOS 版…（第一次会比较慢，2~5 分钟）")
    icon = make_icns()
    code = subprocess.call(build_args(icon, args.universal), cwd=str(ROOT))
    if code != 0:
        print("\n打包失败，看看上面的报错。")
        return code

    src = DIST / "BiliFish.app"
    if not src.exists():
        print(f"没找到产物：{src}")
        return 1

    dst = DIST / "大肥鱼.app"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)

    arch = "universal2" if args.universal else platform.machine()
    print(f"\n打包完成：{dst}（{arch}）")
    if not args.universal:
        target = "Apple 芯片（M 系列）" if platform.machine() == "arm64" else "Intel 芯片"
        print(f"注意：这份包是在 {platform.machine()} 上打的，适合{target}的 Mac；")
        print("      要同时在 Intel 和 Apple 芯片上跑，用 --universal 重新打（需要 universal2 的 Python）。")
    print("发给别人用 zip 压缩这个 .app；第一次打开若被拦，右键 → 打开。")
    print("配置和日志在 ~/Library/Application Support/BiliFish/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
