#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本项目打包成 Windows 发行包（QQBot.exe）。

用法（在本目录，用装好 requirements.txt 的 .venv 执行）：
    python build_exe.py          # 构建 + 组装发行目录 release/QQBot/
    python build_exe.py --zip    # 再压成 release/QQBot-windows-x64.zip

产物结构（release/QQBot/）：
    QQBot.exe + _internal/     PyInstaller onedir 运行时
    bot.py / plugins/ / web/   运行时从磁盘加载的部分（可读可改）
    .env.example               配置项完整说明

给别人的东西就是整个 QQBot 文件夹（或那个 zip）：解压后双击 QQBot.exe，
浏览器里填配置点启动即可，不需要装 Python。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV_PY = HERE / ".venv" / "Scripts" / "python.exe"
BUILD_DIR = HERE / "build_exe"
DIST_DIR = HERE / "dist_exe"
RELEASE = HERE / "release"
APP_DIR = RELEASE / "QQBot"

# 随发行包附带的运行时文件：nonebot 从磁盘加载它们，所以必须是真实文件
SHIP_FILES = ["bot.py", ".env.example", "使用说明.txt"]
SHIP_DIRS = ["plugins", "web"]


def build() -> None:
    cmd = [
        str(VENV_PY), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "QQBot",
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--collect-all", "nonebot",
        "--collect-all", "openai",
        "--collect-all", "uvicorn",
        str(HERE / "qqbot.py"),
    ]
    print("::", " ".join(cmd))
    subprocess.run(cmd, check=True)


def assemble() -> None:
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    APP_DIR.mkdir(parents=True)

    # 运行时（exe + _internal）
    for item in (DIST_DIR / "QQBot").iterdir():
        dest = APP_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # 运行时从磁盘加载的代码与资源
    for name in SHIP_FILES:
        src = HERE / name
        if src.exists():
            shutil.copy2(src, APP_DIR / name)
    for name in SHIP_DIRS:
        shutil.copytree(HERE / name, APP_DIR / name)

    print(f":: 发行目录已就绪：{APP_DIR}")


def make_zip() -> Path:
    zip_path = RELEASE / "QQBot-windows-x64.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP_DIR.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(APP_DIR.parent))
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f":: 压缩包：{zip_path}（{size_mb:.1f} MB）")
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 QQBot.exe")
    parser.add_argument("--zip", action="store_true", help="构建后再压一个 zip")
    args = parser.parse_args()

    if not VENV_PY.exists():
        print("先建好 .venv 并装上 requirements.txt 再打包", file=sys.stderr)
        return 1

    build()
    assemble()
    if args.zip:
        make_zip()
    return 0


if __name__ == "__main__":
    sys.exit(main())
