"""表情包：从本地目录挑一张图发出去。

为什么用本地目录而不是在线图库：
    不需要额外依赖，不怕图库挂掉或限流，也不会把群里的聊天内容发到第三方。
    把自己收藏的图丢进 stickers/ 目录就能用，支持 jpg / png / gif / webp。

触发方式有三种：
    1. 有人明确要 —— 说「表情包」「发个表情」之类，必定随机发一张
    2. 语义匹配 —— 用户说的话命中某个情绪场景（晚安/太棒了/生气…），
       发对应的表情。文件命名即语义：stickers/ 下的图片文件名（不含扩展名）
       在 STICKER_MOODS 表里，就会在该场景被触发。
       换一套表情包 = 丢图进目录 + 在表里加一行，不用改逻辑。
    3. 随机附带 —— 按 AI_STICKER_PROBABILITY 的概率跟着回复一起发（默认关）

目录结构：stickers/ 根目录直接放图，或分子目录放（stickers/jibril/xxx.png），
两种都能被扫到。语义匹配按文件名匹配，与在哪个子目录无关。
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

# 语义表情表：文件名（不含扩展名）→ 触发词。
# 用户消息里出现触发词，就发对应文件。顺序即优先级（前面的先匹配）。
# 注意：词太短会误伤（比如「走」会命中「走进」），四字以上场景优先；
# 单词场景（哼、困、怒）靠上下文独特性，实测误触率可接受。
STICKER_MOODS: dict[str, tuple[str, ...]] = {
    "ok": ("好的", "好吧", "行吧", "可以", "收到", "同意", "妥了", "嗯嗯", "当然"),
    "question": ("真的吗", "真的假的", "是吗", "有没有", "多少", "几点"),
    "huh": ("啊？", "诶？", "哈？", "啥意思", "什么意思", "不明白", "听不懂", "怎么说"),
    "interesting": ("有意思", "有趣", "好玩", "好奇", "展开讲讲", "展开说说", "细说"),
    "yay": ("太棒", "厉害", "牛啊", "牛哇", "恭喜", "优秀", "yyds", "好强", "真强"),
    "sparkle": ("喜欢", "爱你", "谢谢", "感谢", "开心", "可爱", "么么", "心动"),
    "bored": ("无聊", "没意思", "没劲", "枯燥", "乏味"),
    "whatever": ("随便", "无所谓", "都行", "都可以", "看你"),
    "hmph": ("哼", "不理你", "傲娇", "略略略"),
    "angry": ("生气", "气死", "可恶", "混蛋", "烦死", "讨厌", "火大", "怒"),
    "sleepy": ("好困", "想睡", "打瞌睡", "好累", "疲惫", "累了"),
    "zzz": ("晚安", "睡了", "睡觉", "入睡", "睡着"),
    "bye": ("再见", "拜拜", "溜了", "告辞", "先撤", "我走"),
    "surprised": ("卧槽", "天啊", "震惊", "竟然", "居然", "吃惊", "惊了", "离谱"),
    "seeyou": ("下次", "回头", "改天", "一会儿见", "明天见"),
}

# 项目根目录（qq-ai-bot/），用于解析相对路径的贴纸目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_dir(raw: str) -> Path:
    """把配置里的目录解析成绝对路径，相对路径按项目根目录算。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def list_stickers(directory: Path) -> list[Path]:
    """列出目录下所有可用图片（含一层子目录，按名字排序保证稳定）。"""
    if not directory.is_dir():
        return []
    files = [
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(files)


def pick_sticker(directory: Path) -> Path | None:
    """随机挑一张，目录为空或不存在时返回 None。"""
    files = list_stickers(directory)
    return random.choice(files) if files else None


def match_sticker(text: str, directory: Path) -> Path | None:
    """按用户消息的语义挑表情：命中 STICKER_MOODS 的场景词就发对应文件。

    返回 None 表示没有命中任何场景（调用方决定走随机或跳过）。
    目录里没有对应文件时也返回 None（比如只用了一半的表）。
    """
    if not text:
        return None
    for name, words in STICKER_MOODS.items():
        if any(word in text for word in words):
            # 语义表里的文件名，允许带常见扩展名；目录里可能没有，逐个试
            for suffix in ("", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
            # 根目录没有就找子目录里同名的
            for p in list_stickers(directory):
                if p.stem == name:
                    return p
    return None


def is_sticker_request(text: str) -> bool:
    return any(word in text for word in STICKER_TRIGGERS)
