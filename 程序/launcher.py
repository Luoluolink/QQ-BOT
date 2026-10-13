#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ AI 机器人 · 图形化启动器。

流程：
  1. 在终端里检查 Python、建虚拟环境、装依赖
  2. 起一个本地网页，自动打开浏览器
  3. 你在网页里填：QQ 号 / 模型供应商 / 模型名称 / 接口地址 / API Key /
     机器人名字 / 机器人人设
  4. 点「保存并启动」→ 写 .env → 准备 NapCat → 拉起两个服务 → 跳转扫码页

用法：双击「启动.bat」（推荐），或在本目录执行 python launcher.py。

可选参数：
    --cli            不用网页，改回终端里逐项问答
    --no-browser     起了网页但不自动打开浏览器
    --web-port N     指定网页端口（默认 8899，被占用会自动往后找）
    --check-only     只做环境检查与依赖安装，不启动服务
    --skip-napcat    只启动机器人核心（配合 mock_napcat.py 做无 NapCat 的测试）

颜色控制：遵循 NO_COLOR / FORCE_COLOR 环境变量；
老式 cmd 字体把制表符渲染成全角导致框线错位时，设 QQBOT_ASCII=1。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- 常量

# 打包成 exe 后 __file__ 指向解包临时目录，配置/网页/NapCat 都得以 exe 所在目录为准
ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
VENV_DIR = ROOT / ".venv"
ENV_FILE = ROOT / ".env"
REQUIREMENTS = ROOT / "requirements.txt"
DEPS_MARKER = VENV_DIR / ".deps-ok"
WEB_DIR = ROOT / "web"

PORT_NONEBOT = 8080
PORT_NAPCAT_WEBUI = 6099
PORT_WEB_DEFAULT = 8899

# NapCat 自动下载源（GitHub 官方 + 国内加速镜像）
NAPCAT_VERSION = "v4.18.37"
NAPCAT_ASSET = "NapCat.Shell.Windows.Node.zip"
NAPCAT_URLS = [
    f"https://gh-proxy.com/https://github.com/NapNeko/NapCatQQ/releases/download/{NAPCAT_VERSION}/{NAPCAT_ASSET}",
    f"https://github.com/NapNeko/NapCatQQ/releases/download/{NAPCAT_VERSION}/{NAPCAT_ASSET}",
]

# 国内 pip 镜像，默认源失败时回退
PIP_MIRRORS = [
    None,  # 先用默认源
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
]

PLACEHOLDER_MARKERS = ("请替换", "你的", "改成你自己", "xxx")

BOX_W = 60          # 卡片总宽度
INNER = BOX_W - 2   # 卡片内部宽度（两个竖线之间）
RULE_W = 58         # 分隔线宽度

# 这两个由 main() 填好，供 HTTP 处理线程使用
LAUNCH_PY: Path = Path()
LAUNCH_SKIP_NAPCAT = False


# ================================================================ 终端样式

ANSI_RE = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def _enable_windows_ansi() -> bool:
    """在 Windows 控制台上打开 ANSI 转义支持。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


def _decide_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not _enable_windows_ansi():
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


COLOR = _decide_color()

# 老式 cmd 窗口用「新宋体」时，制表符（─│╭╮）会按全角渲染，卡片右边框就歪了。
ASCII_MODE = bool(os.environ.get("QQBOT_ASCII"))


def c(code: str) -> str:
    """把颜色码包成可开关的转义序列。"""
    return code if COLOR else ""


class C:
    """256 色前景色。Windows 10 之后的控制台都支持。"""

    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[38;5;44m"
    green = "\033[38;5;42m"
    yellow = "\033[38;5;214m"
    red = "\033[38;5;203m"
    purple = "\033[38;5;141m"
    gray = "\033[38;5;243m"
    light = "\033[38;5;252m"


class SYM:
    """状态符号。选的都是各字体覆盖率高的字符。"""

    if ASCII_MODE:
        ok, err, warn, info, dot, arrow = "v", "x", "!", "-", "*", ">"
    else:
        ok, err, warn, info, dot, arrow = "√", "×", "!", "·", "●", "→"


class BOX:
    """卡片边框字符。"""

    if ASCII_MODE:
        tl = tr = bl = br = "+"
        h, v = "-", "|"
        bar_on, bar_off = "#", "-"
    else:
        tl, tr, bl, br = "╭", "╮", "╰", "╯"
        h, v = "─", "│"
        bar_on, bar_off = "█", "░"


def dwidth(text: str) -> int:
    """按终端显示宽度计算字符串长度（中文算 2 列，ANSI 转义不计入）。"""
    text = ANSI_RE.sub("", text)
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def wrap(text: str, width: int) -> list[str]:
    """按显示宽度折行，并且保留 ANSI 颜色状态。

    卡片宽度是固定的，但内容长度不受控（比如 NapCat 装在很深的目录里，
    路径能长到 80 列）。与其让右边框被顶歪，不如在这里折成多行。
    """
    if width <= 0:
        return [text]
    rows: list[str] = []
    cur = ""     # 当前这一行（含转义序列）
    state = ""   # 到目前为止还生效着的 SGR 序列
    col = 0      # 当前这一行的显示宽度
    i = 0
    while i < len(text):
        m = ANSI_RE.match(text, i)
        if m:
            seq = m.group(0)
            if seq.endswith("m"):
                if seq in ("\033[0m", "\033[m"):
                    state = ""
                else:
                    state += seq
            cur += seq
            i = m.end()
            continue
        ch = text[i]
        w = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if col + w > width:
            rows.append(cur + (c(C.reset) if state else ""))
            cur = state + ch
            col = w
        else:
            cur += ch
            col += w
        i += 1
    rows.append(cur)
    return rows


# ================================================================ 基础输出


def blank() -> None:
    print()


def rule(char: str = "", width: int = RULE_W, color: str = C.gray) -> None:
    char = BOX.h if (ASCII_MODE or not char) else char
    print(f"  {c(color)}{char * width}{c(C.reset)}")


def panel(lines: list[str], color: str = C.cyan, bold_first: bool = True) -> None:
    """画一个圆角卡片。lines 里可以含 ANSI 颜色码，宽度会自动扣除。

    注意：卡片内容里只放中文和 ASCII。像 · √ → 这类「东亚宽度模糊」的字符
    在中文字体里可能按全角渲染，会把右边框顶歪，所以只用在不需要对齐的状态行上。
    """
    top = BOX.tl + BOX.h * INNER + BOX.tr
    bottom = BOX.bl + BOX.h * INNER + BOX.br
    edge = f"{c(color)}"
    print(f"  {edge}{top}{c(C.reset)}")
    for i, line in enumerate(lines):
        use_bold = bold_first and i == 0 and bool(ANSI_RE.sub("", line).strip())
        body = f"{c(C.bold)}{line}{c(C.reset)}" if use_bold else line
        for row in wrap(body, INNER - 2):
            fill = " " * max(0, INNER - 2 - dwidth(row))
            print(f"  {edge}{BOX.v}{c(C.reset)} {row}{fill} {edge}{BOX.v}{c(C.reset)}")
    print(f"  {edge}{bottom}{c(C.reset)}")


def step(n: int, total: int, text: str) -> None:
    blank()
    badge = f"{c(C.cyan)}{c(C.bold)}[{n}/{total}]{c(C.reset)}"
    print(f"  {badge} {c(C.bold)}{c(C.light)}{text}{c(C.reset)}")
    rule()


def ok(text: str) -> None:
    print(f"    {c(C.green)}{SYM.ok}{c(C.reset)} {text}")


def warn(text: str) -> None:
    print(f"    {c(C.yellow)}{SYM.warn}{c(C.reset)} {c(C.yellow)}{text}{c(C.reset)}")


def err(text: str) -> None:
    print(f"    {c(C.red)}{SYM.err}{c(C.reset)} {c(C.red)}{text}{c(C.reset)}")


def info(text: str) -> None:
    print(f"      {c(C.gray)}{text}{c(C.reset)}")


def hint(text: str) -> None:
    """带前置圆点的提示，比 info 更醒目一点。"""
    print(f"    {c(C.purple)}{SYM.info}{c(C.reset)} {text}")


def link(text: str) -> None:
    print(f"      {c(C.cyan)}{text}{c(C.reset)}")


def die(text: str) -> None:
    blank()
    lines = [f"{c(C.red)}{c(C.bold)}启动失败{c(C.reset)}", ""]
    for row in text.split("\n"):
        lines.append(row)
    panel(lines, color=C.red, bold_first=False)
    pause("按回车键退出...")
    sys.exit(1)


# ================================================================ 动效


class Spinner:
    """在耗时操作旁边转圈；非交互环境自动退化成一行静态提示。"""

    FRAMES = "|/-\\"

    def __init__(self, text: str) -> None:
        self.text = text
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._live = COLOR and sys.stdout.isatty()

    def __enter__(self) -> "Spinner":
        if not self._live:
            print(f"    {c(C.gray)}{SYM.info}{c(C.reset)} {self.text}")
            return self
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            print(f"\r    {c(C.cyan)}{frame}{c(C.reset)} {self.text}", end="", flush=True)
            i += 1
            self._stop.wait(0.1)

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            print("\r\033[K", end="", flush=True)


def progress_bar(done: int, total: int, width: int = 26) -> str:
    """渲染一条进度条字符串（不含换行）。"""
    ratio = (done / total) if total else 0.0
    ratio = max(0.0, min(1.0, ratio))
    filled = int(ratio * width)
    bar = (
        f"{c(C.cyan)}{BOX.bar_on * filled}"
        f"{c(C.gray)}{BOX.bar_off * (width - filled)}{c(C.reset)}"
    )
    mb = (
        f"{done / 1048576:.1f} / {total / 1048576:.1f} MB"
        if total
        else f"{done / 1048576:.1f} MB"
    )
    return f"    {bar}  {c(C.light)}{ratio * 100:3.0f}%{c(C.reset)}  {c(C.gray)}{mb}{c(C.reset)}"


# ================================================================ 交互


def ask(prompt: str, default: str = "") -> str:
    """带默认值的输入，非交互环境下直接返回默认值。"""
    tail = f" {c(C.gray)}[{default}]{c(C.reset)}" if default else ""
    try:
        answer = input(f"    {c(C.purple)}{SYM.arrow}{c(C.reset)} {prompt}{tail}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer or default


def pause(text: str = "按回车键继续...") -> None:
    """等待回车；非交互环境下直接跳过，不会抛异常。"""
    try:
        input(f"\n  {c(C.gray)}{text}{c(C.reset)}")
    except (EOFError, KeyboardInterrupt):
        print()


# ---------------------------------------------------------------- 环境检查


def check_python() -> None:
    # 版本门槛和 README、启动.bat 里写的那句必须一致 ——
    # 三处说法不一样的话，用户会先按 README 装 3.9，再被这里拦下来。
    if sys.version_info < (3, 10):
        die(
            f"需要 Python 3.10 或更高版本，当前是 "
            f"{sys.version_info.major}.{sys.version_info.minor}。\n"
            "\n"
            "请到 https://www.python.org/downloads/ 下载新版，\n"
            "安装时记得勾选「Add Python to PATH」。"
        )
    ok(f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    if sys.version_info < (3, 11):
        hint("版本偏低，能用，但建议 3.11 以上以获得更好的兼容性")


def venv_python() -> Path:
    """虚拟环境里的 python 可执行文件路径（兼容 Windows 与 Linux/macOS）。"""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


# ---------------------------------------------------------------- 快捷方式
#
# Windows 的 .bat 文件本身不能带自定义图标 —— 批处理没有图标资源，
# 资源管理器永远显示那个「窗口 + 齿轮」的通用图标。唯一的正规绕法是
# 做一个快捷方式（.lnk），把图标挂在快捷方式上。
#
# 为什么由启动器来建、而不是打包时预置一个 .lnk：
# .lnk 里存的是**绝对路径**（实测纯相对路径的 .lnk，shell 根本不解析），
# 预置的快捷方式在用户机器上必然指向一个不存在的位置。
# 而启动器运行时拿到的就是真实路径，建出来的永远是对的；
# 每次启动都重建一遍，用户把文件夹挪走后旧快捷方式也能自愈。

SHORTCUT_NAME = "启动机器人.lnk"
ICON_NAME = "launcher.ico"
BAT_NAME = "启动.bat"


def _make_shortcut_once() -> None:
    """实际执行快捷方式创建。任何失败都静默 —— 这是个装饰性功能。"""
    ico = ROOT / ICON_NAME
    bat = ROOT / BAT_NAME
    lnk = ROOT / SHORTCUT_NAME
    if not ico.exists() or not bat.exists():
        return

    # 写成临时 .ps1 再执行，而不是拼 -Command 字符串：
    # 路径里可能有空格、中文甚至单引号，拼字符串迟早出事；
    # utf-8-sig 是 Windows PowerShell 5.1 唯一可靠识别的脚本编码。
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ws = New-Object -ComObject WScript.Shell\n"
        f"$s = $ws.CreateShortcut('{lnk}')\n"
        f"$s.TargetPath = '{bat}'\n"
        f"$s.IconLocation = '{ico},0'\n"
        f"$s.WorkingDirectory = '{ROOT}'\n"
        "$s.Description = 'QQ AI Bot 启动器'\n"
        "$s.Save()\n"
    )
    tmp = Path(tempfile.gettempdir()) / f"qqbot_shortcut_{os.getpid()}.ps1"
    tmp.write_text(script, encoding="utf-8-sig")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(tmp)],
            capture_output=True, timeout=30, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def make_shortcut_async() -> None:
    """后台线程里建快捷方式：换图标这件事不值得让启动流程等它。"""
    if os.name != "nt":
        return

    def _work() -> None:
        try:
            _make_shortcut_once()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_work, daemon=True, name="qqbot-shortcut").start()


def ensure_venv() -> Path:
    """确保虚拟环境存在**且能跑**，返回其中的 python 路径。

    不能只看文件在不在：如果整个文件夹是从别的电脑拷过来的，
    .venv 里的 pyvenv.cfg 指向的是原来那台机器的 Python 安装路径，
    文件都在但一跑就死。所以这里实际执行一次 --version 验证，
    验证不过就整个删掉重建（依赖随后由 install_deps 自动重装）。
    """
    py = venv_python()
    if py.exists():
        broken_reason = ""
        try:
            probe = subprocess.run(
                [str(py), "--version"],
                capture_output=True, timeout=20, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if probe.returncode != 0:
                broken_reason = f"退出码 {probe.returncode}"
        except Exception as exc:  # noqa: BLE001
            broken_reason = repr(exc)
        if not broken_reason:
            ok(f"虚拟环境已就绪（{VENV_DIR.name}/）")
            return py
        warn(f"检测到虚拟环境损坏（{broken_reason}），大概率是文件夹从别的电脑拷过来的，重建中…")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    with Spinner("正在创建虚拟环境…"):
        try:
            import venv

            venv.EnvBuilder(with_pip=True, clear=False).create(str(VENV_DIR))
        except Exception as exc:  # noqa: BLE001
            die(f"创建虚拟环境失败：{exc!r}")

    if not py.exists():
        die("虚拟环境创建后找不到 python，请检查磁盘权限。")
    ok("虚拟环境创建完成")
    return py


def requirements_hash() -> str:
    try:
        return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    except OSError:
        return ""


def ensure_deps(py: Path) -> None:
    """安装 requirements.txt 里的依赖；依赖没变过就跳过。"""
    if not REQUIREMENTS.exists():
        warn("找不到 requirements.txt，跳过依赖安装")
        return

    want = requirements_hash()
    if DEPS_MARKER.exists() and DEPS_MARKER.read_text().strip() == want:
        ok("依赖已是最新（跳过安装）")
        return

    last_error = ""
    for index, mirror in enumerate(PIP_MIRRORS):
        label = "正在安装依赖…" if index == 0 else "换镜像源重试…"
        if mirror:
            hint(f"镜像源：{c(C.gray)}{mirror}{c(C.reset)}")
        with Spinner(label):
            try:
                cmd = [
                    str(py), "-m", "pip", "install", "-r", str(REQUIREMENTS),
                    "--disable-pip-version-check",
                ]
                if mirror:
                    cmd += ["-i", mirror]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            except subprocess.TimeoutExpired:
                last_error = "安装超时"
                continue
        if proc.returncode == 0:
            DEPS_MARKER.write_text(want)
            ok("依赖安装完成")
            return
        last_error = (proc.stderr or proc.stdout or "").strip()[-800:]

    die(
        "依赖安装失败。常见原因：网络不通、需要代理、或 pip 本身损坏。\n"
        "\n"
        "可以手动执行下面这条命令看看具体报错：\n"
        f"{py} -m pip install -r requirements.txt\n"
        "\n"
        f"最后的错误信息：\n{last_error}"
    )


# ---------------------------------------------------------------- .env 读写


def read_env() -> dict:
    data: dict = {}
    if not ENV_FILE.exists():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def new_token() -> str:
    """生成一个 ASCII 随机 Token（含中文会导致 WS 握手失败，所以只取 hex）。"""
    return "qbot_" + secrets.token_hex(8)


def dotenv_quote(text: str) -> str:
    """把多行文本转成 dotenv 能读回来的双引号形式。

    python-dotenv 对双引号值会做转义还原，所以换行写成字面量 \\n 即可。
    反斜杠必须最先转义，否则 Windows 路径里的 \\ 会被当成转义符吃掉。
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\n")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


# 生图（ComfyUI）相关配置：这些项不放启动器网页上，但重写 .env 时必须原样保留，
# 否则用户在 .env 里配好的生图参数会被启动器悄悄重置回默认值。
IMAGE_ENV_KEYS: list[tuple[str, str]] = [
    ("AI_IMAGE_ENABLED", "false"),
    ("AI_IMAGE_API_BASE", "http://127.0.0.1:8188"),
    ("AI_IMAGE_DEFAULT_MODEL", ""),
    ("AI_IMAGE_DEFAULT_NEGATIVE", "低质量, 模糊, 变形, 多余的手指, 文字, 水印"),
    ("AI_IMAGE_STEPS", "20"),
    ("AI_IMAGE_CFG", "7.0"),
    ("AI_IMAGE_SAMPLER", "euler"),
    ("AI_IMAGE_SCHEDULER", "normal"),
    ("AI_IMAGE_WIDTH", "512"),
    ("AI_IMAGE_HEIGHT", "512"),
    ("AI_IMAGE_MAX_WIDTH", "1024"),
    ("AI_IMAGE_MAX_HEIGHT", "1024"),
    ("AI_IMAGE_BATCH", "1"),
    ("AI_IMAGE_TIMEOUT", "180"),
    ("AI_IMAGE_POLL", "0.8"),
    ("AI_IMAGE_SAVE_DIR", "data/images"),
    ("AI_IMAGE_KEEP", "200"),
    ("AI_IMAGE_COOLDOWN", "30"),
    ("AI_IMAGE_DAILY_LIMIT", "20"),
    ("AI_IMAGE_ANNOUNCE", "true"),
    ("AI_IMAGE_NL_TRIGGER", "false"),
    ("AI_IMAGE_MODELS_TTL", "300"),
    ("AI_IMAGE_MAX_PROMPT", "300"),
]

IMAGE_ENV_HEADER = """# ---------- 本地 ComfyUI 生图（可选）----------
# 让 ComfyUI 在本机跑起来，把 AI_IMAGE_ENABLED 改成 true，
# 群里发「/画 一只猫」就能出图；发 /画图模型 看本机支持哪些模型。
# 启动器不会改动下面这些行，可以放心手改。
# 完整参数说明见同目录 .env.example。
"""


def dotenv_value(value: str) -> str:
    """需要时给值加引号。带 # 的值不加引号会被 dotenv 当成注释截断。"""
    text = str(value)
    if any(ch in text for ch in '#"') or text != text.strip():
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def build_image_env_block(old: dict) -> str:
    """生成生图配置块。

    取值优先级：.env 里已有的 > 默认值。
    注意判的是「键存不存在」而不是「值是否为空」—— 用户手动把某项清空
    （例如不想要默认负向词）也应该被尊重，不能被默认值塞回来。
    """
    lines = [IMAGE_ENV_HEADER.rstrip()]
    for key, default in IMAGE_ENV_KEYS:
        value = old[key] if key in old else default
        lines.append(f"{key}={dotenv_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


ENV_HEADER = """# ============================================================
# 本文件由启动器自动生成。
# 想手工调整各项参数（冷却、每日额度、联网开关等），
# 请看同目录 .env.example 里的完整说明，改完重启即可。
# ============================================================

# ---------- NoneBot2 基础 ----------
DRIVER=~fastapi
HOST=127.0.0.1
PORT={port}
# 必须与 NapCat「反向 WS」里填的 Token 完全一致（启动器会自动同步两边）
# 只能用 ASCII 字符，中文会让 WS 握手直接失败
ONEBOT_ACCESS_TOKEN={token}
COMMAND_START=["/"]

# ---------- 机器人身份 ----------
# 机器人登录用的 QQ 号。这个值用于 NapCat 的快速登录，
# 不填也不影响使用（照样可以扫码登录）。
BOT_QQ={qq}

# ---------- 大模型 ----------
AI_API_BASE={base}
AI_API_KEY={key}
AI_MODEL={model}
AI_BOT_NAME={botname}

# ---------- 人设 ----------
# 由启动器网页里填写。改这里或重新跑一遍启动器都行。
AI_SYSTEM_PROMPT={persona}

# ---------- 常用开关（完整参数见 .env.example）----------
# 关掉思考模式，群聊延迟能降到 1/3 以下，强烈建议保持 true
AI_DISABLE_THINKING=true
AI_TEMPERATURE=0.8
AI_MAX_TOKENS=400
# 联网搜索需要先在模型厂商控制台开通「联网服务插件」，
# 没开通也不会报错，程序会自动降级成普通对话
AI_WEB_SEARCH_ENABLED=false
AI_WEB_SEARCH_DAILY_LIMIT=30
AI_FILE_SEARCH_ENABLED=true
AI_ESSENCE_ENABLED=true
AI_MEMBER_ENABLED=true
AI_WELCOME_ENABLED=true
AI_VISION_ENABLED={vision}
AI_STICKER_ENABLED=false
AI_COOLDOWN=5
AI_DAILY_LIMIT=200
AI_GROUP_WHITELIST=[]
AI_REPLY_QUOTE=true

{image_block}"""


def write_env(form: dict, old: dict) -> dict:
    """按网页表单生成 .env，返回生成后的配置字典。

    Token 沿用已有的（没有才新建）—— 每次启动都换 Token 会让 NapCat 里
    已经配好的那份对不上，白白多一次排查。
    """
    token = old.get("ONEBOT_ACCESS_TOKEN", "")
    if not token or not token.isascii() or any(m in token for m in PLACEHOLDER_MARKERS):
        token = new_token()

    base = (form.get("base") or "").strip().rstrip("/")
    model = (form.get("model") or "").strip()
    # 纯文本模型收到图片会直接 400，所以按模型名粗略判断能不能识图
    vision = (
        "false"
        if re.search(r"(deepseek|glm-4-flash|qwen-turbo)", model, re.I)
        else "true"
    )

    text = ENV_HEADER.format(
        port=PORT_NONEBOT,
        token=token,
        qq=(form.get("qq") or "").strip(),
        base=base,
        key=(form.get("key") or "").strip(),
        model=model,
        botname=(form.get("botname") or "").strip(),
        persona=dotenv_quote((form.get("persona") or "").strip()),
        vision=vision,
        image_block=build_image_env_block(old),
    )
    ENV_FILE.write_text(text, encoding="utf-8")

    env = read_env()
    env["_form"] = form  # type: ignore[assignment]
    return env


# ---------------------------------------------------------------- NapCat


def looks_like_napcat(path: Path) -> bool:
    """判断一个目录是不是 NapCat 根目录。"""
    if not path.is_dir():
        return False
    has_runtime = (path / "napcat" / "napcat.mjs").exists() or (path / "napcat.mjs").exists()
    has_launcher = (path / "napcat.bat").exists() or (path / "node.exe").exists()
    return has_runtime and has_launcher


def napcat_config_dir(napcat_dir: Path) -> Path:
    """推出 NapCat 实际读取配置的目录。

    NapCat 的规则是「配置文件放在 napcat.mjs 所在目录的 config/ 下」，
    所以要先找到 napcat.mjs 在哪一层。
    """
    if (napcat_dir / "napcat" / "napcat.mjs").exists():
        return napcat_dir / "napcat" / "config"
    return napcat_dir / "config"


def find_napcat() -> Path | None:
    """在常见位置找已安装的 NapCat。"""
    candidates = [
        ROOT / "napcat",
        ROOT.parent / "NapCat",
        ROOT.parent / "napcat",
        Path.home() / "NapCat",
        Path("C:/NapCat"),
        Path("D:/NapCat"),
    ]
    env_hint = os.environ.get("NAPCAT_DIR")
    if env_hint:
        candidates.insert(0, Path(env_hint))
    for path in candidates:
        try:
            if looks_like_napcat(path):
                return path.resolve()
        except OSError:
            continue
    return None


def download_napcat(on_progress=None) -> Path | None:
    """下载并解压 NapCat 到 ./napcat。失败返回 None。"""
    target = ROOT / "napcat"
    zip_path = ROOT / NAPCAT_ASSET

    for url in NAPCAT_URLS:
        try:
            _download(url, zip_path, on_progress)
        except Exception as exc:  # noqa: BLE001
            if on_progress:
                on_progress(f"这个下载源失败了（{exc}），换下一个")
            continue
        try:
            with zipfile.ZipFile(zip_path) as zf:
                bad = zf.testzip()
                if bad:
                    raise RuntimeError(f"压缩包损坏：{bad}")
                zf.extractall(target)
            zip_path.unlink(missing_ok=True)
            if looks_like_napcat(target):
                return target.resolve()
        except Exception as exc:  # noqa: BLE001
            if on_progress:
                on_progress(f"解压失败：{exc}")
            continue

    return None


def _download(url: str, dest: Path, on_progress=None) -> None:
    """带进度回调的下载。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last_report = 0.0
        live = COLOR and sys.stdout.isatty()
        with dest.open("wb") as fp:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fp.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_report > 0.3:
                    last_report = now
                    if live:
                        print("\r\033[K" + progress_bar(done, total), end="", flush=True)
                    if on_progress:
                        pct = f"{done * 100 // total}%" if total else "…"
                        on_progress(f"正在下载 NapCat {pct}（{done / 1048576:.1f} MB）")
        if live:
            print("\r\033[K" + progress_bar(done, total))
        elif total:
            print(f"    已下载 {done / 1048576:.1f} / {total / 1048576:.1f} MB")
    if total and done != total:
        raise RuntimeError(f"下载不完整：{done}/{total} 字节")


def write_napcat_config(napcat_dir: Path, env: dict, qq: str = "") -> str:
    """写入 onebot11.json（反向 WS）与 webui.json，返回 WebUI 地址。"""
    token = env.get("ONEBOT_ACCESS_TOKEN", "")
    port = env.get("PORT", str(PORT_NONEBOT))
    conf_dir = napcat_config_dir(napcat_dir)
    conf_dir.mkdir(parents=True, exist_ok=True)

    onebot = {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "name": "nonebot",
                    "enable": True,
                    "url": f"ws://127.0.0.1:{port}/onebot/v11/ws",
                    "messagePostFormat": "array",
                    "reportSelfMessage": False,
                    "reconnectInterval": 5000,
                    "token": token,
                    "debug": False,
                    "heartInterval": 30000,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }

    webui_path = conf_dir / "webui.json"
    if webui_path.exists():
        try:
            webui = json.loads(webui_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            webui = {}
    else:
        webui = {}
    webui.setdefault("host", "127.0.0.1")
    webui.setdefault("port", PORT_NAPCAT_WEBUI)
    webui.setdefault("token", "napcatwebui")
    webui.setdefault("loginRate", 3)
    # 填了 QQ 号就让 NapCat 先试快速登录，不行它会自动降级回二维码，不会卡住
    if qq.isdigit():
        webui["autoLoginAccount"] = qq

    (conf_dir / "onebot11.json").write_text(
        json.dumps(onebot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    webui_path.write_text(
        json.dumps(webui, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return f"http://127.0.0.1:{webui.get('port')}/webui?token={webui.get('token')}"


# ---------------------------------------------------------------- 进程与端口


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def free_port(start: int) -> int:
    """从 start 开始找一个没被占用的端口。"""
    for port in range(start, start + 50):
        if not port_in_use(port):
            return port
    return start


def spawn(title: str, cmd: list[str], cwd: Path, extra_env: dict | None = None) -> subprocess.Popen:
    """新开一个控制台窗口运行命令，方便用户看到日志与二维码。"""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    kwargs: dict = {"cwd": str(cwd), "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    proc = subprocess.Popen(cmd, **kwargs)
    ok(f"已启动「{c(C.light)}{title}{c(C.reset)}」（PID {c(C.gray)}{proc.pid}{c(C.reset)}）")
    return proc


def wait_port(port: int, timeout: float) -> bool:
    """等端口被监听，超时返回 False。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(port):
            return True
        time.sleep(0.5)
    return False


# ================================================================ 网页服务

STEPS = ["写入配置文件", "准备 NapCat 协议端", "启动机器人核心", "打开扫码登录页"]

STATE_LOCK = threading.Lock()
STATE: dict = {
    "state": "idle",     # idle | running | done | error
    "step": 0,
    "steps": STEPS,
    "text": "等待填写…",
    "log": [],
    "webui": "",
    "message": "",
    "error": "",
}


def _set(**kw) -> None:
    with STATE_LOCK:
        STATE.update(kw)


def _push_log(line: str) -> None:
    """给网页进度页追加一行日志，同时打到终端。"""
    with STATE_LOCK:
        STATE["log"] = (STATE["log"] + [line])[-8:]
        STATE["text"] = line
    print(f"    {c(C.gray)}{SYM.info}{c(C.reset)} {line}")


# ---------------------------------------------------------------- 模型连通性


def test_model(base: str, key: str, model: str, timeout: int = 30) -> dict:
    """真的打一次模型接口，验证地址 / Key / 模型名是否可用。"""
    base = (base or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        return {"ok": False, "error": "接口地址要以 http:// 或 https:// 开头"}
    if not model:
        return {"ok": False, "error": "模型名称没填"}
    if not key:
        return {"ok": False, "error": "API Key 没填"}

    url = base + "/chat/completions"
    started = time.time()

    def call(extra: dict) -> tuple[int, str]:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "回复两个字：正常"}],
            "max_tokens": 256,
        }
        payload.update(extra)
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    try:
        status, body = call({})
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"连不上这个地址（{exc.reason}），检查网络或代理"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"请求失败：{exc}"}

    if status != 200:
        detail = _extract_error(body) or body[:200]
        tips = {
            401: "API Key 不对，或者这个 Key 不属于这个平台",
            403: "Key 没权限，或者账号未完成认证",
            404: "接口地址不对 —— 注意 Base URL 一般要以 /v1 结尾",
            429: "请求太频繁，或者账户额度已用尽",
        }
        return {
            "ok": False,
            "error": f"HTTP {status}：{detail}" + (f"（{tips[status]}）" if status in tips else ""),
        }

    reply = _extract_reply(body)
    if not reply:
        # 有些模型默认开着思考，会把 max_tokens 全吃在思考上，正文就空了。
        # 关掉思考再来一次，这跟插件里用的是同一招。
        try:
            status2, body2 = call({"thinking": {"type": "disabled"}})
            if status2 == 200:
                reply = _extract_reply(body2)
        except Exception:  # noqa: BLE001
            pass

    ms = int((time.time() - started) * 1000)
    if not reply:
        return {
            "ok": True,
            "reply": "（接口通了，但模型没返回正文）",
            "ms": ms,
        }
    return {"ok": True, "reply": reply[:40], "ms": ms}


def _extract_reply(body: str) -> str:
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # 少数平台返回分段数组
        return "".join(
            seg.get("text", "") for seg in content if isinstance(seg, dict)
        ).strip()
    return ""


def _extract_error(body: str) -> str:
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("error", "message", "msg", "detail"):
        value = data.get(key)
        if isinstance(value, str):
            return value[:200]
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            return value["message"][:200]
    return ""


# ---------------------------------------------------------------- 启动流水线


def run_pipeline(py: Path, form: dict, skip_napcat: bool = False) -> None:
    """后台线程：写配置 → 准备 NapCat → 起机器人核心 → 起 NapCat。"""
    try:
        # ---- 1. 写入配置文件
        _set(state="running", step=0, text="正在写入 .env …")
        old = read_env()
        env = write_env(form, old)
        _push_log("配置已写入 .env")
        qq = (form.get("qq") or "").strip()

        # ---- 2. 准备 NapCat
        napcat_dir: Path | None = None
        if skip_napcat:
            _push_log("按参数要求跳过 NapCat")
        else:
            _set(step=1, text="正在查找 NapCat …")
            napcat_dir = find_napcat()
            if napcat_dir:
                _push_log(f"找到 NapCat：{napcat_dir}")
            else:
                _push_log("本机没有 NapCat，开始下载（约 120MB，第一次会比较久）")
                napcat_dir = download_napcat(on_progress=_push_log)
                if napcat_dir:
                    _push_log(f"NapCat 已就绪：{napcat_dir}")
                else:
                    _push_log("NapCat 自动下载失败，本次只启动机器人核心")

        webui = ""
        if napcat_dir:
            webui = write_napcat_config(napcat_dir, env, qq)
            _push_log("NapCat 的 OneBot 配置已写入（两边 Token 已对齐）")

        # ---- 3. 启动机器人核心
        _set(step=2, text="正在启动机器人核心 …")
        port = int(env.get("PORT", PORT_NONEBOT) or PORT_NONEBOT)
        if port_in_use(port):
            _push_log(f"端口 {port} 已被占用，可能已经有一个机器人在跑")

        no_proxy = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        if getattr(sys, "frozen", False):
            # 打包版没有独立的 python，用 exe 自身的 --run-bot 模式充当机器人核心
            spawn("机器人核心", [str(py), "--run-bot"], ROOT, no_proxy)
        else:
            spawn("机器人核心", [str(py), "bot.py"], ROOT, no_proxy)

        _set(text=f"等待机器人核心就绪（127.0.0.1:{port}）…")
        if wait_port(port, 30):
            _push_log(f"机器人核心已监听 127.0.0.1:{port}")
        else:
            _push_log("没等到机器人核心起来，看它的窗口里有没有报错")

        # ---- 4. 启动 NapCat
        if napcat_dir is None:
            _set(
                state="done",
                step=4,
                text="已启动（未启用 NapCat）",
                webui="",
                message=(
                    "机器人核心已经跑起来了，但没有 NapCat 就收不到 QQ 消息。\n"
                    "想先验证流程，可以另开一个终端执行：\n"
                    f"{py} mock_napcat.py"
                ),
            )
            return

        _set(step=3, text="正在启动 NapCat 协议端 …")
        launcher = napcat_dir / "napcat.bat"
        if launcher.exists():
            cmd = ["cmd", "/c", str(launcher)]
        else:
            cmd = [str(napcat_dir / "node.exe"), "index.js"]
        spawn("NapCat 协议端", cmd, napcat_dir)

        _set(text="等待 NapCat 的网页控制台就绪…")
        if wait_port(PORT_NAPCAT_WEBUI, 60):
            _push_log(f"NapCat 网页控制台已就绪：{webui}")
        else:
            _push_log("NapCat 的网页控制台还没起来，稍等几秒刷新一下")

        _set(
            state="done",
            step=4,
            text="全部就绪",
            webui=webui,
            message="在打开的 NapCat 页面里扫码登录，登录完机器人就能在群里说话了。",
        )

    except Exception as exc:  # noqa: BLE001
        import traceback

        _set(state="error", error=f"{exc}", text="启动失败")
        _push_log(f"启动失败：{exc}")
        traceback.print_exc()


# ---------------------------------------------------------------- HTTP


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "QQBotLauncher"

    # ---- 工具

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _send_file(self, target: Path, ctype: str) -> None:
        """带 Range 支持地发文件（背景图/大文件都能顺畅加载）。"""
        size = target.stat().st_size
        rng = self.headers.get("Range") or ""
        start, end, partial = 0, size - 1, False
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                elif b:  # bytes=-N 表示最后 N 字节
                    start = max(size - int(b), 0)
                if 0 <= start <= end < size:
                    partial = True
                else:
                    start, end, partial = 0, size - 1, False
            except ValueError:
                start, end, partial = 0, size - 1, False

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with target.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 路由

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/progress":
            with STATE_LOCK:
                self._json(dict(STATE))
            return

        if path in ("/", "/index.html"):
            path = "/index.html"

        root = WEB_DIR.resolve()
        target = (WEB_DIR / path.lstrip("/")).resolve()
        try:
            inside = str(target).startswith(str(root))
        except OSError:
            inside = False
        if not inside or not target.is_file():
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send_file(target, ctype)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        form = self._read_json()

        if path == "/api/test":
            self._json(
                test_model(form.get("base", ""), form.get("key", ""), form.get("model", ""))
            )
            return

        if path == "/api/save":
            with STATE_LOCK:
                if STATE["state"] == "running":
                    self._json({"ok": False, "error": "已经在启动中了，稍等一下"})
                    return

            missing = [
                label
                for key, label in (
                    ("qq", "QQ 号"),
                    ("botname", "机器人名字"),
                    ("model", "模型名称"),
                    ("base", "接口地址"),
                    ("key", "API Key"),
                    ("persona", "机器人人设"),
                )
                if not (form.get(key) or "").strip()
            ]
            if missing:
                self._json({"ok": False, "error": "还差：" + "、".join(missing)})
                return
            if not re.fullmatch(r"\d{5,12}", (form.get("qq") or "").strip()):
                self._json({"ok": False, "error": "QQ 号要填 5~12 位数字"})
                return
            if not (form.get("base") or "").strip().startswith(("http://", "https://")):
                self._json({"ok": False, "error": "接口地址要以 http:// 或 https:// 开头"})
                return

            _set(state="running", step=0, text="正在启动…", log=[], error="", message="", webui="")
            threading.Thread(
                target=run_pipeline,
                args=(LAUNCH_PY, form, LAUNCH_SKIP_NAPCAT),
                daemon=True,
            ).start()
            self._json({"ok": True})
            return

        self._json({"ok": False, "error": "unknown endpoint"}, 404)

    def log_message(self, *args) -> None:  # noqa: D102
        pass  # 静音：日志走我们自己的终端输出


def start_web(port: int, open_browser: bool) -> ThreadingHTTPServer:
    """起网页服务（后台线程），返回 server 对象。"""
    if not (WEB_DIR / "index.html").exists():
        die(
            "找不到 web/index.html，发布包不完整。\n"
            "\n"
            "请确认 web/ 目录和 launcher.py 在同一个文件夹里。"
        )
    srv = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    blank()
    panel(
        [
            f"{c(C.green)}{c(C.bold)}配置页面已就绪{c(C.reset)}",
            "",
            "浏览器里填完信息，点「保存并启动」就行。",
            "没自动打开的话，手动访问下面这个地址：",
        ],
        color=C.green,
        bold_first=False,
    )
    blank()
    link(url)
    blank()

    if open_browser:
        try:
            webbrowser.open(url)
            ok("已尝试打开浏览器")
        except Exception as exc:  # noqa: BLE001
            warn(f"没能自动打开浏览器（{exc}），请手动访问上面的地址")
    return srv


# ================================================================ 终端问答模式


DEFAULT_PERSONA = (
    "你是这个 QQ 群里的 AI 助手。说话友好、耐心，像群里一个靠谱的朋友。"
    "回答简洁，一般不超过 100 字。不知道的就说不知道，不要编。"
    "不使用 Markdown 语法，因为 QQ 不渲染这些格式。"
)


def configure_env_cli() -> dict:
    """终端问答模式：生成 .env。"""
    if not ENV_FILE.exists():
        ok("还没有 .env，稍后会按你的输入生成")
    else:
        ok(".env 已存在，本次输入会覆盖模型与人设相关项")

    env = read_env()
    blank()
    panel(
        [
            f"{c(C.yellow)}填写机器人配置{c(C.reset)}",
            "",
            "直接回车表示沿用方括号里的默认值。",
            "API Key 只存在你自己的电脑上，不会上传到任何地方。",
        ],
        color=C.yellow,
        bold_first=False,
    )
    blank()

    form = {
        "qq": ask("机器人 QQ 号", env.get("BOT_QQ", "")),
        "botname": ask("机器人名字", env.get("AI_BOT_NAME", "") or "小助手"),
        "base": ask(
            "接口地址 Base URL",
            env.get("AI_API_BASE") or "https://api.xiaomimimo.com/v1",
        ),
        "model": ask("模型名称", env.get("AI_MODEL") or "mimo-v2.6-flash"),
        "key": ask("API Key", env.get("AI_API_KEY", "")),
        "persona": ask("人设（一句话也行）", DEFAULT_PERSONA),
        "vendor": "cli",
    }
    blank()
    return write_env(form, env)


# ================================================================ 主流程


def banner() -> None:
    blank()
    panel(
        [
            "",
            f"{c(C.cyan)}{c(C.bold)}QQ 群 AI 对话机器人{c(C.reset)}",
            f"{c(C.gray)}图形化启动器 / NoneBot2 + NapCat{c(C.reset)}",
            "",
        ],
        color=C.cyan,
        bold_first=False,
    )


def summary(env: dict, webui: str, napcat_ready: bool) -> None:
    blank()
    panel(
        [
            f"{c(C.green)}{c(C.bold)}启动完成{c(C.reset)}",
            "",
            f"{c(C.gray)}机器人名字{c(C.reset)}  {env.get('AI_BOT_NAME', '(未配置)')}",
            f"{c(C.gray)}模型{c(C.reset)}        {env.get('AI_MODEL', '(未配置)')}",
            f"{c(C.gray)}NapCat{c(C.reset)}      {'已就绪' if napcat_ready else '未启用'}",
        ],
        color=C.green,
        bold_first=False,
    )

    blank()
    print(f"  {c(C.bold)}{c(C.light)}访问地址{c(C.reset)}")
    rule()
    if webui:
        info(f"NapCat 扫码页  {c(C.cyan)}{webui}{c(C.reset)}")
    info(f"机器人核心    {c(C.cyan)}http://127.0.0.1:{PORT_NONEBOT}{c(C.reset)}")

    blank()
    print(f"  {c(C.bold)}{c(C.light)}接下来{c(C.reset)}")
    rule()
    info(f"{c(C.cyan)}1{c(C.reset)}  在 NapCat 页面里用手机 QQ 扫码并确认登录")
    info(f"    {c(C.gray)}二维码约 2 分钟过期，过期了在页面里点刷新{c(C.reset)}")
    info(f"{c(C.cyan)}2{c(C.reset)}  把机器人拉进一个群，在群里 @ 它说话试试")
    info(f"{c(C.cyan)}3{c(C.reset)}  问它「群里有没有 XX」，它会去翻群文件和群相册")
    info(f"{c(C.cyan)}4{c(C.reset)}  发张图片 @ 它，多模态模型能直接看图回答")

    blank()
    panel(
        [
            f"{c(C.yellow)}注意{c(C.reset)}",
            "",
            "QQ 客户端和 NapCat 不能同时登录同一个号。",
            "如果扫码报「当前账号已登录，无法重复登录」，",
            "先在手机或电脑 QQ 上把那个号退掉再扫。",
        ],
        color=C.yellow,
        bold_first=False,
    )

    blank()
    info(f"{c(C.gray)}改配置：编辑 .env（完整参数说明见 .env.example），然后重新运行启动器{c(C.reset)}")
    info(f"{c(C.gray)}改人设：重新跑一遍启动器，在网页里改{c(C.reset)}")
    blank()


def main() -> int:
    global LAUNCH_PY, LAUNCH_SKIP_NAPCAT

    parser = argparse.ArgumentParser(description="QQ AI 机器人启动器")
    parser.add_argument("--cli", action="store_true", help="不用网页，改回终端问答")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--web-port", type=int, default=PORT_WEB_DEFAULT, help="网页端口")
    parser.add_argument("--check-only", action="store_true", help="只检查环境与依赖，不启动")
    parser.add_argument("--skip-napcat", action="store_true", help="只启动机器人核心")
    args = parser.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    if COLOR:
        print("\033]0;QQ AI Bot\007", end="")

    banner()

    # 换图标这事和启动流程无关，丢后台线程，别让它挡路
    make_shortcut_async()

    if getattr(sys, "frozen", False):
        # 打包版：Python 与全部依赖已内置在 exe 里，跳过环境检查与装依赖
        ok("内置运行时就绪（打包版无需安装 Python 与依赖）")
        py = Path(sys.executable)
    else:
        step(1, 4, "检查 Python 环境")
        check_python()

        step(2, 4, "准备虚拟环境与依赖")
        py = ensure_venv()
        ensure_deps(py)

    LAUNCH_PY = py
    LAUNCH_SKIP_NAPCAT = args.skip_napcat

    if args.check_only:
        blank()
        ok(f"{c(C.green)}环境检查完成{c(C.reset)}（--check-only，不启动服务）")
        blank()
        return 0

    # ---- 终端问答模式
    if args.cli:
        step(3, 4, "填写配置")
        env = configure_env_cli()
        step(4, 4, "准备 NapCat 并启动服务")
        run_pipeline(py, env.get("_form") or {}, skip_napcat=args.skip_napcat)
        with STATE_LOCK:
            snap = dict(STATE)
        if snap["state"] == "error":
            err(snap["error"])
        summary(env, snap.get("webui", ""), bool(snap.get("webui")))
        pause("按回车键关闭本窗口（服务会继续在各自的窗口里运行）...")
        return 0

    # ---- 图形化模式
    port = free_port(args.web_port)
    if port != args.web_port:
        hint(f"端口 {args.web_port} 被占用，改用 {port}")

    start_web(port, open_browser=not args.no_browser)

    print(f"  {c(C.gray)}这个窗口先别关 —— 机器人核心和 NapCat 的日志在它们各自的窗口里。{c(C.reset)}")
    print(f"  {c(C.gray)}按 Ctrl+C 可以结束本启动器。{c(C.reset)}")
    blank()

    announced = False
    try:
        while True:
            time.sleep(1)
            with STATE_LOCK:
                state = STATE["state"]
                webui = STATE["webui"]
                message = STATE["message"]

            if state == "idle":
                announced = False
                continue
            if state not in ("done", "error") or announced:
                continue

            announced = True
            color = C.green if state == "done" else C.red
            title = "启动完成" if state == "done" else "启动失败"
            blank()
            rule("━", color=color)
            print(f"  {c(C.bold)}{c(color)}{title}{c(C.reset)}")
            rule("━", color=color)
            if message:
                for row in message.split("\n"):
                    info(row)
            if state == "error":
                err(STATE.get("error", "未知错误"))

            if webui:
                blank()
                print(f"  {c(C.bold)}{c(C.light)}扫码地址{c(C.reset)}")
                link(webui)
                try:
                    webbrowser.open(webui)
                    ok("已尝试打开扫码页面")
                except Exception:  # noqa: BLE001
                    pass

            blank()
            info(f"{c(C.gray)}本窗口可以最小化，但别关 —— 关掉网页就访问不到了。{c(C.reset)}")
            blank()

    except KeyboardInterrupt:
        blank()
        print(
            f"  {c(C.yellow)}已退出启动器"
            f"（机器人核心与 NapCat 仍在各自的窗口里运行）。{c(C.reset)}"
        )
        blank()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        blank()
        print(f"  {c(C.yellow)}已取消。{c(C.reset)}")
        sys.exit(130)
