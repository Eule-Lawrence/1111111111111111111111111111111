"""Windows 打包：产出 dist/windows/大肥鱼.exe

用法（在 Windows 上）：
    .venv\\Scripts\\python.exe build.py
或直接：
    .venv\\Scripts\\python.exe build_windows.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "windows"
WORK = ROOT / "build" / "windows"

ARGS = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name",
    "BiliFish",
    "--distpath",
    str(DIST),
    "--workpath",
    str(WORK),
    # bilibili-api 的接口定义是包里的 JSON 文件，必须打进去
    "--collect-data",
    "bilibili_api",
    # 插件式导入的模块，静态分析扫不到
    "--hidden-import",
    "bilibili_api.clients.HTTPXClient",
    "--hidden-import",
    "pystray._win32",
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
    "matplotlib",
    "--exclude-module",
    "numpy",
    "--exclude-module",
    "pandas",
    "--exclude-module",
    "scipy",
    "--exclude-module",
    "IPython",
    "--exclude-module",
    "pytest",
    "--log-level",
    "WARN",
    # 图标和背景图
    "--icon",
    str(ROOT / "icon.ico"),
    "--add-data",
    f"{ROOT / 'icon.ico'};.",
    "--add-data",
    f"{ROOT / 'assets'};assets",
    str(ROOT / "main.py"),
]


def main() -> int:
    if sys.platform != "win32":
        print("这是 Windows 打包脚本；macOS 请运行 build_mac.py")
        return 1

    print("开始打包 Windows 版…（第一次会比较慢，2~5 分钟）")
    code = subprocess.call(ARGS, cwd=str(ROOT))
    if code != 0:
        print("\n打包失败，看看上面的报错。")
        return code

    src = DIST / "BiliFish.exe"
    if not src.exists():
        print(f"没找到产物：{src}")
        return 1

    dst = DIST / "大肥鱼.exe"
    if dst.exists():
        dst.unlink()
    shutil.copy2(src, dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"\n打包完成：{dst}（{size_mb:.1f} MB）")
    print("发给别人只需要这一个 exe；配置和日志在 %APPDATA%\\BiliFish\\")
    return 0


if __name__ == "__main__":
    sys.exit(main())
