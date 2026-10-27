"""群聊环境上下文：把没 @ 机器人的群消息记进环形缓冲，对话时作为背景注入。

为什么需要：机器人只在被 @ 时才收到消息，@ 之前群里聊了什么它一无所知。
用户聊了一阵再 @ 它「那你觉得呢」「他说的对吗」，没有背景就接不上话，
这也是「聊天不自然、衔接不上」的最大来源。这里维护每群最近若干条普通
群聊（带发言人标记），在 _build_context 里作为「背景资料」交给模型——
它不是对话历史的一部分，只是让模型知道大家正在聊什么。

记录的过滤规则（和主动发言的触发判断一致）：
- 只记群消息；@ 机器人的不记（那些本来就在对话历史里，记了会重复）
- 命令（/ 开头等）不记
- 机器人自己的消息不记
- 只存内存，重启即清——背景要的只是「最近」，不值得落盘

和 proactive 的关系：主动发言插话时的上下文也从这里取（原来它自己维护
一份 _recent），一份缓冲两处用，行为不变。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import DefaultDict, Deque

from nonebot import get_plugin_config, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule

from .config import Config
from .tools import at_me, speaker_tag

cfg = get_plugin_config(Config)

# 环形缓冲长度：背景注入默认 6 条、主动发言取 8 条，20 条余量足够
_BUF_LEN = 20

_recent: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=_BUF_LEN))


def record(group_id: int, speaker: str, text: str) -> None:
    """记一条普通群聊（带发言人标记；长文截断，背景只要话题不要全文）。"""
    text = (text or "").strip()
    if not text:
        return
    _recent[group_id].append(f"【{speaker}】{text[:80]}")


def recent_lines(group_id: int, limit: int = 8) -> list[str]:
    """取最近 limit 条（时间正序）。"""
    buf = _recent.get(group_id)
    if not buf:
        return []
    return list(buf)[-limit:]


def describe(group_id: int, count: int = 6) -> str:
    """拼成注入给模型的背景块。没有记录返回空串。"""
    lines = recent_lines(group_id, count)
    if not lines:
        return ""
    return (
        "【最近群聊背景】以下是最近群里其他成员的发言，不是对你说的，"
        "仅供你了解大家正在聊什么。回答时可自然衔接话题，但不要逐条复述，"
        "也不要主动回应与你被问的内容无关的部分：\n" + "\n".join(lines)
    )


def _rule(bot: Bot, event: Event) -> bool:
    """只记：群消息、没 @ 机器人、不是命令、群在白名单内。"""
    if not isinstance(event, GroupMessageEvent):
        return False
    if not cfg.ai_ambient_enabled:
        return False
    if cfg.ai_group_whitelist and event.group_id not in cfg.ai_group_whitelist:
        return False
    if at_me(event, bot.self_id):
        return False
    return not event.get_plaintext().lstrip().startswith(("/", "！", "?", "？"))


# priority=65 刻意排在主动发言(60)之后：主动发言 handler 取背景时，
# 本条触发消息还没进缓冲，正好和「触发消息单独放」的旧行为一致
recorder = on_message(rule=Rule(_rule), priority=65, block=False)


@recorder.handle()
async def _handle_record(bot: Bot, event: GroupMessageEvent) -> None:
    if event.user_id == int(event.self_id):
        return
    sender = event.sender
    speaker = speaker_tag(
        getattr(sender, "card", "") or "", getattr(sender, "nickname", "") or "",
        event.user_id,
    )
    record(event.group_id, speaker, event.get_plaintext())
