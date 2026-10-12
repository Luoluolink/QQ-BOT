"""各子模块共用的小工具函数。

这里只放「纯逻辑、不依赖机器人实例」的辅助，方便单独做单元测试。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Message

from . import members


def reply_message_id(event: Event) -> Optional[int]:
    """取被引用（回复）消息的 message_id；当前消息不是回复就返回 None。

    NoneBot 的 OneBot v11 适配器会把 reply 段解析成 event.reply，
    里面带着被回复那条消息的完整信息。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return None
    try:
        return int(reply.message_id)
    except (TypeError, ValueError):
        return None


def reply_sender_id(event: Event) -> Optional[int]:
    """取被引用消息发送者的 QQ 号；不是回复或解析不出就返回 None。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return None
    sender = getattr(reply, "sender", None)
    user_id = getattr(sender, "user_id", None) if sender else None
    try:
        return int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        return None


def target_user_id(event: Event, args: Message, self_id: int) -> Optional[int]:
    """解析「操作对象是谁」：优先命令里 @ 的人，其次被引用消息的发送者。

    这样 /戳 @某人、/戳（回复某人消息）两种用法都行。
    """
    ats = members.extract_ats(args, self_id)
    if ats:
        return int(ats[0])
    return reply_sender_id(event)


def args_text(args: Message) -> str:
    """取命令参数里的纯文本（丢掉 @、图片等段）。"""
    return args.extract_plain_text().strip()


def fmt_time(ts: int | float) -> str:
    """把 unix 时间戳转成 MM-DD HH:MM，群聊展示用不着年份。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "时间未知"


def truncate(text: str, limit: int = 120) -> str:
    """展示用截断，太长的消息只留开头。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def resolve_emoji_id(arg: str) -> Optional[str]:
    """把用户输入转成表情回应需要的 emoji_id。

    QQ 的消息表情回应用的是「表情码点」：直接发一个 emoji（如 👍）
    就按它的 Unicode 码点传（👍 → 128077）。也兼容：
    - 纯数字：当作表情 ID 原样传（ NapCat 也认经典小黄脸 ID）
    - 中文别名：查表换成对应 emoji 再取码点
    """
    arg = arg.strip()
    if not arg:
        return None
    if arg.isdigit():
        return arg
    emoji = EMOJI_ALIASES.get(arg, arg)
    for ch in emoji:
        if _is_emoji_char(ch):
            return str(ord(ch))
    return None


def _is_emoji_char(ch: str) -> bool:
    """判断单个字符是不是 emoji/符号（排除中日韩等普通文字）。

    覆盖常见 emoji 码位：符号区、箭头区、扩展表情区、变体选择符、零宽连接符。
    汉字落在 CJK 区（0x4E00+），不会误伤 —— 「你好」这种输入不会被当成表情。
    """
    cp = ord(ch)
    return (
        cp >= 0x1F000
        or 0x2190 <= cp <= 0x2BFF
        or cp in (0xFE0F, 0x200D)
    )


EMOJI_ALIASES: dict[str, str] = {
    "赞": "👍",
    "好": "👍",
    "好耶": "👍",
    "棒": "👍",
    "6": "👍",
    "点赞": "👍",
    "踩": "👎",
    "不行": "👎",
    "心": "❤️",
    "红心": "❤️",
    "爱": "❤️",
    "哭": "😭",
    "笑": "😄",
    "哈哈": "😄",
    "乐": "😄",
    "惊讶": "😲",
    "惊": "😲",
    "火": "🔥",
    "燃": "🔥",
    "玫瑰": "🌹",
    "花": "🌹",
    "蛋糕": "🎂",
    "干杯": "🍻",
    "星星": "⭐",
    "赞爆": "👍",
    "吐": "🤮",
    "思考": "🤔",
    "纪念": "🎉",
    "庆祝": "🎉",
}
