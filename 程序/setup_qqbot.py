# -*- coding: utf-8 -*-
"""QQBot 一键安装器。

单个 exe（内嵌 release/QQBot-windows-x64.zip），双击后：
  1. 解压整套程序到 %LOCALAPPDATA%\\QQBot（升级安装时保留已有 .env）
  2. 在桌面创建「QQ机器人」快捷方式
  3. 写一个卸载脚本 uninst.bat 到安装目录
  4. 直接启动 QQBot.exe 打开配置面板

重新打包：python build_installer.py（先跑 build_exe.py --zip 出新 zip）
测试：    QQBOT_SETUP_TARGET=<目录> QQBot安装器.exe --no-launch
"""

import os
import shutil
import socket
import sys
import zipfile
from pathlib import Path

APP_NAME = "QQBot"
DIR_NAME = "QQBot"
ZIP_NAME = "QQBot-windows-x64.zip"


def resource_zip() -> Path:
    """定位内嵌的发行包 zip（PyInstaller onefile 解包到 _MEIPASS）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base / ZIP_NAME
    if not candidate.exists():
        # 源码态运行时兜底：找相对路径的 release/
        candidate = Path(__file__).resolve().parent / "release" / ZIP_NAME
    if not candidate.exists():
        raise SystemExit(f"找不到内嵌发行包 {ZIP_NAME}，安装包可能不完整。")
    return candidate


def install_dir() -> Path:
    override = os.environ.get("QQBOT_SETUP_TARGET")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / DIR_NAME


def total_uncompressed(zp: Path) -> int:
    with zipfile.ZipFile(zp) as zf:
        return sum(i.file_size for i in zf.infolist())


def extract(zp: Path, target: Path) -> None:
    total = total_uncompressed(zp)
    done = 0
    with zipfile.ZipFile(zp) as zf:
        for info in zf.infolist():
            rel = info.filename.replace("\\", "/")
            # zip 里的条目都带 QQBot/ 前缀，剥掉让文件直接落到安装目录
            if rel.startswith(f"{APP_NAME}/"):
                rel = rel[len(APP_NAME) + 1:]
            elif rel.endswith("/"):
                continue
            if not rel or rel.startswith("/"):
                continue
            dest = target / rel
            # 升级安装：用户已配好的 .env 不覆盖
            if rel == ".env" and dest.exists():
                continue
            if info.filename.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info.filename) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            done += info.file_size
            pct = done * 100 // total if total else 100
            bar = "#" * (pct // 4)
            print(f"\r[{bar:<25}] {pct:3d}%  {rel[:60]:<60}", end="", flush=True)
    print()


def make_shortcut(target: Path) -> None:
    """桌面快捷方式。失败不致命（比如精简系统没有 WScript.Shell）。"""
    try:
        import win32com.client  # noqa: PLC0415

        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.exists():
            desktop = Path.home() / "Desktop"
        lnk_path = desktop / "QQ机器人.lnk"
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        shortcut.Targetpath = str(target / "QQBot.exe")
        shortcut.WorkingDirectory = str(target)
        shortcut.IconLocation = str(target / "QQBot.exe")
        shortcut.Description = "QQ 机器人（双击打开配置面板）"
        shortcut.save()
        print(f":: 桌面快捷方式：{lnk_path}")
    except Exception as exc:  # noqa: BLE001
        print(f":: 快捷方式创建失败（不影响使用）：{exc}")


def make_uninstaller(target: Path) -> None:
    bat = target / "uninst.bat"
    lines = [
        "@echo off",
        "chcp 936 >nul",
        "echo 即将删除 QQ机器人（%LOCALAPPDATA%\\QQBot）与桌面快捷方式。",
        "pause",
        'del "%USERPROFILE%\\Desktop\\QQ机器人.lnk" >nul 2>&1',
        "cd /d %LOCALAPPDATA%",
        "rmdir /s /q QQBot",
        "echo 已卸载完成。",
        "pause",
    ]
    bat.write_text("\r\n".join(lines) + "\r\n", encoding="gbk")
    print(f":: 卸载脚本：{bat}")


def already_running(target: Path) -> bool:
    """QQBot.exe 是否正在运行（升级安装时文件会被锁住）。"""
    try:
        out = os.popen(
            'tasklist /FI "IMAGENAME eq QQBot.exe" /FO CSV /NH'
        ).read()
        return "QQBot.exe" in out
    except Exception:  # noqa: BLE001
        return False


def free_port(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    busy = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return not busy


def main() -> int:
    no_launch = "--no-launch" in sys.argv
    zp = resource_zip()
    target = install_dir()

    print("=" * 66)
    print("  QQ机器人 一键安装")
    print("=" * 66)
    print(f"安装位置：{target}")
    if already_running(target):
        print("⚠ 检测到 QQ机器人正在运行，请先关掉它的窗口再运行安装器。")
        return 1
    if not free_port(8899):
        print("⚠ 配置面板端口 8899 被占用（可能已有一个机器人正在跑）。")
        print("  如果是升级安装，请先关掉旧的机器人窗口再试。")
        return 1

    target.mkdir(parents=True, exist_ok=True)
    print("正在解压（" + f"{zp.stat().st_size / 1048576:.1f} MB）…")
    try:
        extract(zp, target)
    except PermissionError as exc:
        print(f"\n解压失败：{exc}")
        print("多半是旧版本文件被占用——关掉 QQ机器人 的所有窗口后重试。")
        return 1

    make_uninstaller(target)
    if os.environ.get("QQBOT_SETUP_TARGET"):
        print(":: 测试模式（QQBOT_SETUP_TARGET 已设置）：跳过桌面快捷方式")
    else:
        make_shortcut(target)

    print(":: 安装完成！")
    if no_launch:
        print(":: --no-launch：不启动程序")
        return 0

    print(":: 正在启动配置面板…（浏览器会自动打开，填好配置点「保存并启动」）")
    import subprocess  # noqa: PLC0415

    subprocess.Popen(
        [str(target / "QQBot.exe")],
        cwd=str(target),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
