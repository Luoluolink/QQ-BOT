"""各子模块共用的小工具函数。

这里只放「纯逻辑、不依赖机器人实例」的辅助，方便单独做单元测试。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Message


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


def reply_quote_text(event: Event, max_len: int = 150) -> str:
    """把被引用（回复）的消息还原成「【引用 XX 的消息】内容」。

    为什么必须单独取：members.render_text / get_plaintext 都只认 text 和 at
    段，reply 段会被整个丢掉——用户「引用某条消息再 @机器人」时，模型就
    看不到引用了什么，「他说得对吗」这种话完全接不上。引用为空返回空串。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return ""
    sender = getattr(reply, "sender", None)
    user_id = getattr(sender, "user_id", None) if sender else None
    if user_id:
        name = speaker_tag(
            getattr(sender, "card", "") or "",
            getattr(sender, "nickname", "") or "",
            user_id,
        )
    else:
        name = "某人"

    msg = getattr(reply, "message", None)
    text = ""
    if msg is not None:
        try:
            text = msg.extract_plain_text().strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            # 纯图片引用抽不出正文，补个占位让模型知道引用的是图
            try:
                if any(seg.type == "image" for seg in msg):
                    text = "[图片]"
            except Exception:  # noqa: BLE001
                pass
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return f"【引用 {name} 的消息】{text}"


def target_user_id(event: Event, args: Message, self_id: int) -> Optional[int]:
    """解析「操作对象是谁」：优先命令里 @ 的人，其次被引用消息的发送者。

    这样 /戳 @某人、/戳（回复某人消息）两种用法都行。
    """
    # 函数内导入：tools 被 members 反向依赖（speaker_tag），模块级会成环
    from . import members  # noqa: PLC0415

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


def at_me(event: Event, self_id: str | int) -> bool:
    """@ 机器人在消息**任意位置**都算 to_me。

    OneBot v11 适配器的 _check_at_me 只认「第一段是 @机器人」或「最后一段是
    @机器人」，而「[图片] @机器人 文字」这种 @ 夹在中间的消息会被漏掉，
    导致 to_me=False、所有需要 @ 的匹配器全部沉默。这里补上全位置判断。
    """
    try:
        from nonebot.adapters.onebot.v11 import MessageEvent
    except ImportError:  # pragma: no cover
        return False
    if not isinstance(event, MessageEvent):
        return False
    if getattr(event, "to_me", False):
        return True
    return any(
        seg.type == "at" and str(seg.data.get("qq", "")) == str(self_id)
        for seg in event.message
    )


def speaker_tag(card: str, nickname: str, user_id) -> str:
    """统一的说话人标记：名片 > 昵称，末尾恒定追加 QQ 号做唯一锚点。

    为什么必须带号码：聊天历史靠【】标记区分谁说了什么，而
    （1）群名片随时会改——同一个人今天叫张三明天叫李四，光用名字
    模型就接不上了；（2）两个人的昵称可能撞车（都叫「无」/emoji 名），
    模型把 A 的话安到 B 头上。名字后挂个永不变的号码，
    改名不断档、重名不分家。
    """
    name = (card or nickname or "").strip()
    return f"{name}({user_id})" if name else f"QQ{user_id}"


def split_reply_bubbles(reply: str, max_bubbles: int = 3) -> list[str]:
    """把一条回复拆成 1~3 条短消息，模拟真人连发的节奏。

    真人发消息是「哈哈这个我知道」一条、「我还知道个冷知识」又一条——
    不是把三句话挤进一个大段落。按句末标点切分，过短的碎片并回前一条；
    只有一句的不拆。上限 max_bubbles 防止刷屏，
    超出部分并回最后一条。
    """
    text = (reply or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?…~～])\s*", text) if p.strip()]
    if len(parts) <= 1:
        return [text]
    merged: list[str] = []
    for part in parts:
        if merged and (len(part) <= 3 or len(merged[-1]) <= 3):
            merged[-1] += part        # 碎片（语气词、短应答）并回前一条
        else:
            merged.append(part)
    if len(merged) <= 1:
        return [text]
    bubbles = merged[:max_bubbles]
    rest = "".join(merged[max_bubbles:])
    if rest:
        bubbles[-1] += rest
    return bubbles
