#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：把启动器与机器人核心装进同一个 exe（配合 build_exe.py 使用）。

QQBot.exe              双击 → 图形化启动器（网页配置面板）
QQBot.exe --run-bot    由启动器内部调用，运行机器人核心（等价 python bot.py）

为什么需要 --run-bot：打包后用户机器上没有 Python，启动器拉不起
`python bot.py`，就让启动器把 exe 自己再开一个进程、带上这个参数
充当机器人核心，进程模型与源码版完全一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _app_root() -> Path:
    """exe 所在目录（源码运行时则是本文件所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def run_bot() -> int:
    root = _app_root()
    os.chdir(root)  # nonebot 从 CWD 读 .env / plugins/
    # 冻结态的 sys.path 不含 exe 目录，而 nonebot.load_plugins("plugins")
    # 靠 namespace package 机制从磁盘导入插件 —— 不补这条会报
    # ModuleNotFoundError: No module named 'plugins'
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # 这些显式 import 是给 PyInstaller 的静态分析看的：
    # 真正的启动在下面的 runpy 里动态完成，不写在这里这些依赖会被漏收。
    import nonebot  # noqa: F401
    import nonebot.adapters.onebot.v11  # noqa: F401
    import nonebot.drivers.fastapi  # noqa: F401

    sys.argv = ["bot.py"]
    import runpy

    runpy.run_path(str(root / "bot.py"), run_name="__main__")
    return 0


def run_launcher() -> int:
    os.chdir(_app_root())
    import launcher

    return launcher.main()


def main() -> int:
    if "--run-bot" in sys.argv:
        sys.argv.remove("--run-bot")
        return run_bot()
    return run_launcher()


if __name__ == "__main__":
    sys.exit(main())
