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

import asyncio
import time as _time
from collections import defaultdict, deque
from typing import DefaultDict, Deque

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.rule import Rule

from . import vision
from .config import Config
from .tools import at_me, speaker_tag

cfg = get_plugin_config(Config)

# 环形缓冲长度：背景注入 6 条、主动发言 8 条、漫画选段最多 40 条，60 条余量够
_BUF_LEN = 60

_recent: DefaultDict[int, Deque[tuple[float, str]]] = defaultdict(
    lambda: deque(maxlen=_BUF_LEN)
)


def record(group_id: int, speaker: str, text: str) -> None:
    """记一条普通群聊（带发言人标记；长文截断，背景只要话题不要全文）。"""
    text = (text or "").strip()
    if not text:
        return
    import time as _time  # noqa: PLC0415

    _recent[group_id].append((_time.time(), f"【{speaker}】{text[:80]}"))


def recent_lines(group_id: int, limit: int = 8) -> list[str]:
    """取最近 limit 条（时间正序，不限时间范围）。"""
    buf = _recent.get(group_id)
    if not buf:
        return []
    return [line for _, line in list(buf)[-limit:]]


def recent_lines_since(group_id: int, seconds: float, limit: int = 40) -> list[str]:
    """取最近 seconds 秒内的记录（时间正序）——漫画选段的时间窗用。"""
    import time as _time  # noqa: PLC0415

    buf = _recent.get(group_id)
    if not buf:
        return []
    cutoff = _time.time() - seconds
    return [line for ts, line in buf if ts >= cutoff][-limit:]


def describe(group_id: int, count: int = 6) -> str:
    """拼成注入给模型的背景块。没有记录返回空串。

    过滤掉【我】开头的行（机器人自己的回复）——背景是"别人在聊什么"，
    混入自己刚说过的话会诱发自我复读。漫画选段用的是另一条不过滤的路径。
    """
    lines = [
        ln for ln in recent_lines(group_id, count)
        if not ln.startswith("【我】")
    ]
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
    from . import reminders as _reminders  # noqa: PLC0415
    from . import qzone as _qzone  # noqa: PLC0415

    _reminders.start_checker()   # 幂等：只在第一次真正创建检查任务
    _qzone.start_auto_loop()   # 空间自动心情，同款幂等懒启动
    sender = event.sender
    speaker = speaker_tag(
        getattr(sender, "card", "") or "", getattr(sender, "nickname", "") or "",
        event.user_id,
    )
    record(event.group_id, speaker, event.get_plaintext())

    # 图片记忆：这张图后台生成描述存进上下文，隔空提问才接得住
    if cfg.ai_vision_enabled and any(s.type == "image" for s in event.get_message()):
        asyncio.create_task(_remember_image(bot, event, speaker))


# ---------------------------------------------------------------- 图片记忆
#
# 群友发的图（不 @ 机器人）后台用视觉模型生成一句话描述存进上下文，
# 之后「评价一下刚才那张图」才有东西可评。节流 + 每日上限防账单失控。

_img_last: dict[int, float] = {}
_img_daily: dict[tuple[str, str], int] = {}


async def _remember_image(bot: Bot, event: GroupMessageEvent, speaker: str) -> None:
    gid = event.group_id
    now = _time.time()
    if now - _img_last.get(gid, 0) < 8:      # 节流：两组图至少隔 8 秒
        return
    today = _time.strftime("%Y-%m-%d")
    if _img_daily.get((str(gid), today), 0) >= 30:
        return
    _img_last[gid] = now
    try:
        images = await vision.collect_images(bot, event, limit=1)
        desc = await vision.describe_images(bot, images, mode=cfg.ai_vision_image_mode)
        if not desc:
            return
        _img_daily[(str(gid), today)] = _img_daily.get((str(gid), today), 0) + 1
        record(gid, speaker, f"[图片] {desc}")
        logger.info(f"[ai_chat] 图片记忆：{desc[:60]}")
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 图片记忆失败（不影响聊天）: {exc!r}")
