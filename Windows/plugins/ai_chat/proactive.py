"""主动发言（第一档功能，保守版）。

群里没 @ 机器人、但聊天内容命中关键词时，按概率插一句话。

为什么限流这么严：这是「主动发消息」，和入群欢迎一样是 QQ 风控最敏感的
行为模式；而且没被 @ 就搭话，插得不是时候容易招人烦。所以四道闸门缺一不可，
任何一道不过就直接沉默（连日志都不打，避免刷屏）：
1. 关键词命中（默认只认「机器人/AI」相关词，不会对日常闲聊插嘴）
2. 消息长度 ≥ ai_proactive_min_len（防止「AI」两个字在口水消息里误命中）
3. 概率 + 同群冷却（默认 35% × 5 分钟）
4. 每群每日上限（默认 12 次）

上下文是自带的环形缓冲：本 matcher 收得到群里的全部消息（包括没 @ 的），
顺手存最近 8 条，插话时连同触发消息一起给模型，让它接得上话题。
"""

from __future__ import annotations

import random
import time
from collections import defaultdict, deque
from datetime import date
from typing import DefaultDict, Deque, Dict

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .config import Config
from .tools import at_me

__plugin_meta__ = PluginMetadata(
    name="主动发言",
    description="群里聊到机器人相关话题时按概率自然插话；四道限流闸门，防刷屏防封号。",
    usage="无需指令。在 .env 里配置 AI_PROACTIVE_KEYWORDS 控制触发词，"
    "AI_PROACTIVE_ENABLED=false 可整体关闭。",
    type="application",
    supported_adapters={"~onebot.v11"},
)

cfg = get_plugin_config(Config)

# 每群最近几条群聊（含发言人标记），插话时给模型当上下文
_recent: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=8))

# 限流状态
_last_send: Dict[int, float] = {}
_usage: DefaultDict[int, int] = defaultdict(int)
_usage_day: str = date.today().isoformat()


def _roll_day_if_needed() -> None:
    global _usage_day
    today = date.today().isoformat()
    if today != _usage_day:
        _usage.clear()
        _usage_day = today


def stats(group_id: int) -> dict:
    """给 /状态 指令看的用量数据。"""
    return {"used": _usage.get(group_id, 0), "last": _last_send.get(group_id, 0.0)}


def _rule(bot: Bot, event: Event) -> bool:
    """触发前的廉价判断（不打模型、不落盘）。

    真正的闸门（概率/冷却/额度）放在 handler 里做，rule 里只做
    「这条消息到底可不可能触发」的结构判断：群里、没 @ 我、不是命令。
    """
    return (
        isinstance(event, GroupMessageEvent)
        and cfg.ai_proactive_enabled
        and not at_me(event, bot.self_id)
        and not event.get_plaintext().lstrip().startswith(("/", "！", "?", "？"))
    )


proactive = on_message(rule=Rule(_rule), priority=60, block=False)


@proactive.handle()
async def handle_proactive(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = event.group_id
    now = time.time()

    if cfg.ai_group_whitelist and group_id not in cfg.ai_group_whitelist:
        return

    sender = event.sender
    name = (getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip()
    speaker = name or f"QQ{event.user_id}"
    text = event.get_plaintext().strip()

    # 自己的消息（理论上 NapCat 不上报，双保险）或空文本不记不触发
    if event.user_id == int(event.self_id) or not text:
        return

    # 先记录上下文：不管触不触发，群聊内容都值得存进环形缓冲
    _recent[group_id].append(f"【{speaker}】{text[:60]}")

    # ---- 闸门 1：关键词 ----
    lowered = text.lower()
    if not any(k.lower() in lowered for k in cfg.ai_proactive_keywords):
        return
    # ---- 闸门 2：消息长度 ----
    if len(text) < cfg.ai_proactive_min_len:
        return
    # ---- 闸门 3：概率 ----
    if random.random() >= cfg.ai_proactive_probability:
        return
    # ---- 闸门 4：冷却 + 每日额度 ----
    if now - _last_send.get(group_id, 0.0) < cfg.ai_proactive_cooldown:
        return
    _roll_day_if_needed()
    if _usage[group_id] >= cfg.ai_proactive_daily_limit:
        return

    _last_send[group_id] = now
    _usage[group_id] += 1
    logger.info(
        f"[ai_chat] 主动发言触发（群 {group_id}，今日第 {_usage[group_id]} 次）: "
        f"{text[:60]}"
    )

    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    context_lines = list(_recent[group_id])[:-1]  # 触发消息单独放，不重复
    context_block = "\n".join(context_lines) if context_lines else "（暂无其他记录）"
    messages = [
        {
            "role": "system",
            "content": (
                f"今天的日期是 {today.isoformat()}（星期{weekday}）。\n\n"
                + cfg.ai_system_prompt
                + "\n\n【当前情境】群友们在聊天，没有人 @ 你，但消息提到了与你相关的词。"
                "你可以插话，但必须克制：只说一句、40 字以内、直接接住当前话题。"
                "你只是恰好在旁听到了，绝不要提到「系统提示」「触发词」这类后台概念。"
                "如果实在没什么值得说的，就回复一个字：嗯"
            ),
        },
        {
            "role": "user",
            "content": (
                "最近的群聊记录：\n"
                f"{context_block}\n\n"
                f"刚出现的消息：\n【{speaker}】{text[:100]}"
            ),
        },
    ]

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=cfg.ai_api_key, base_url=cfg.ai_api_base)
        kwargs: dict = {
            "model": cfg.ai_model,
            "messages": messages,
            "temperature": cfg.ai_temperature,
            "max_tokens": 120,
        }
        if cfg.ai_disable_thinking:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = await client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 主动发言生成失败（静默跳过）: {exc!r}")
        return

    reply = (resp.choices[0].message.content or "").strip()
    # 「嗯」是内部约定的放弃信号；风控文案绝不能发出去
    if not reply or reply in ("嗯", "。") or "high risk" in reply.lower():
        return
    if len(reply) > 120:
        reply = reply[:119] + "…"

    try:
        await bot.send(event, Message(reply))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 主动发言发送失败: {exc!r}")
