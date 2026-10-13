"""表情包：从本地目录随机挑一张图发出去。

为什么用本地目录而不是在线图库：
    不需要额外依赖，不怕图库挂掉或限流，也不会把群里的聊天内容发到第三方。
    把自己收藏的图丢进 stickers/ 目录就能用，支持 jpg / png / gif / webp。

触发方式有两种：
    1. 有人明确要 —— 说「表情包」「发个表情」之类，必定发一张
    2. 随机附带 —— 按 AI_STICKER_PROBABILITY 的概率跟着回复一起发
"""

from __future__ import annotations

import random
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 出现这些词就认为对方在要表情包
STICKER_TRIGGERS = (
    "表情包", "表情", "发个图", "来张图", "发张图", "来个图", "斗图",
    "发个表情", "来点图", "整个活",
)

# 项目根目录（qq-ai-bot/），用于解析相对路径的贴纸目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_dir(raw: str) -> Path:
    """把配置里的目录解析成绝对路径，相对路径按项目根目录算。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def list_stickers(directory: Path) -> list[Path]:
    """列出目录下所有可用图片，不递归子目录。"""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def pick_sticker(directory: Path) -> Path | None:
    """随机挑一张，目录为空或不存在时返回 None。"""
    files = list_stickers(directory)
    return random.choice(files) if files else None


def is_sticker_request(text: str) -> bool:
    return any(word in text for word in STICKER_TRIGGERS)
