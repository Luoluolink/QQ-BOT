"""主动发言（动态概率版）。

群里没 @ 机器人时，按「基准概率 × 活跃度乘数」插一句话。

为什么限流这么严：这是「主动发消息」，和入群欢迎一样是 QQ 风控最敏感的
行为模式；而且没被 @ 就搭话，插得不是时候容易招人烦。所以多道闸门缺一不可，
任何一道不过就直接沉默（连日志都不打，避免刷屏）：
1. 话题相关：命中关键词，或走通用通道（非关键词消息 × generic_ratio）
2. 消息长度 ≥ ai_proactive_min_len（防止「AI」两个字在口水消息里误命中）
3. 动态概率：群里人少时调高（暖场），人多时调低（防刷屏）
4. 同群冷却 + 每群每日上限

活跃度怎么算：每条群消息都记一条 (时间戳, 发言人)，统计最近
ai_proactive_window 秒内的独立发言人数，按档位乘概率。人数越少乘数越高。

上下文由 ambient 模块的共享环形缓冲提供（每群最近若干条群聊，含没 @ 的），
插话时连同触发消息一起给模型，让它接得上话题。这个缓冲同时服务于
@ 机器人的正常对话（作为「最近群聊背景」注入），一份两用。
"""

from __future__ import annotations

import asyncio
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

from . import ambient, digest, followup, imagegen
from .config import Config
from .tools import at_me

__plugin_meta__ = PluginMetadata(
    name="主动发言",
    description="群里聊到相关话题（或任意消息低倍率）时按动态概率自然插话；"
    "人少时概率高、人多时概率低，多道限流闸门防刷屏防封号。",
    usage="无需指令。在 .env 里配置 AI_PROACTIVE_KEYWORDS 控制触发词，"
    "AI_PROACTIVE_GENERIC_RATIO 控制非关键词消息的触发倍率，"
    "AI_PROACTIVE_ENABLED=false 可整体关闭。",
    type="application",
    supported_adapters={"~onebot.v11"},
)

cfg = get_plugin_config(Config)

# 群活跃度滑动窗口：{群号: deque[(时间戳, user_id)]}。
# 每条群消息记一笔（含机器人自己被过滤前的也记不了——self 消息 NapCat 不上报），
# 用来统计「最近 ai_proactive_window 秒内有几个不同的人说过话」。
_activity: DefaultDict[int, Deque[tuple[float, int]]] = defaultdict(
    lambda: deque(maxlen=300)
)

# 活跃度分档：(独立发言人数上限, 概率乘数)。
# 人少 → 乘数高（群里冷清时多暖场）；人多 → 乘数低（插嘴容易变成刷屏）。
_ACTIVITY_TIERS = ((2, 2.0), (5, 1.2), (10, 0.8), (10**9, 0.4))

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


def _speakers_in_window(group_id: int, now: float) -> int:
    """统计活跃度窗口内有多少个不同的群友发过言。"""
    window = cfg.ai_proactive_window
    return len(
        {uid for ts, uid in _activity[group_id] if now - ts <= window}
    )


def _looks_like_bot(group_id: int, user_id: int, now: float) -> bool:
    """这个账号像不像机器人：60 秒内发言超过 ai_proactive_bot_msg_per_min 条就是。

    为什么需要：群拉进来的 bot 说的话也会进我们的流水，不防的话
    「A 的 bot 说一句 → 我们接一句 → A 的 bot 再接 → …」会无限互刷，
    既烦人又极易触发 QQ 风控。真人连续 1 分钟发 5 条以上极少见，
    真有这种刷屏者，不接他的话也没损失。阈值设 0 = 关闭本检测。
    """
    threshold = cfg.ai_proactive_bot_msg_per_min
    if threshold <= 0:
        return False
    recent = [
        ts for ts, uid in _activity[group_id]
        if uid == user_id and now - ts <= 60.0
    ]
    return len(recent) >= threshold


def _dynamic_probability(group_id: int, now: float) -> float:
    """按群里热闹程度算实际触发概率 = 基准概率 × 活跃度乘数。

    1~2 人在聊 → ×2.0（冷清，暖场）；3~5 人 → ×1.2；
    6~10 人 → ×0.8；10 人以上 → ×0.4（热闹，压低防刷屏）。
    深夜（1~7 点）再乘 ai_proactive_quiet_mult——没人气的时段搭话瘆人。
    """
    speakers = _speakers_in_window(group_id, now)
    mult = next(m for cap, m in _ACTIVITY_TIERS if speakers <= cap)
    probability = min(1.0, cfg.ai_proactive_probability * mult)
    hour = time.localtime(now).tm_hour
    if 1 <= hour < 7:
        probability *= cfg.ai_proactive_quiet_mult
    return probability


def stats(group_id: int) -> dict:
    """给 /状态 指令看的用量数据。"""
    return {
        "used": _usage.get(group_id, 0),
        "last": _last_send.get(group_id, 0.0),
        "speakers": _speakers_in_window(group_id, time.time()),
    }


def _rule(bot: Bot, event: Event) -> bool:
    """触发前的廉价判断（不打模型、不落盘）。

    真正的闸门（概率/冷却/额度）放在 handler 里做，rule 里只做
    「这条消息到底可不可能触发」的结构判断：群里、没 @ 我、不是命令、
    不在连续对话中（对方正跟机器人说话，由正常回复路径接，别插嘴）。
    """
    return (
        isinstance(event, GroupMessageEvent)
        and cfg.ai_proactive_enabled
        and not at_me(event, bot.self_id)
        and not followup.active(event.group_id, event.user_id)
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

    # 先记录活跃度：群聊原文由 ambient 的记录器（priority=65）统一进共享缓冲，
    # 这里不再重复记；@ 机器人的消息 ambient 也不记（在对话历史里）
    _activity[group_id].append((now, event.user_id))
    # 没 @ 机器人的群消息在这里记账，@ 它的在 handle_chat 里记，
    # 两边合起来喂给滚动摘要，机器人才能知道群里完整在聊什么。
    if cfg.ai_digest_enabled:
        digest.observe(group_id, speaker, text, now)
        digest.maybe_compress(group_id)

    # ---- 闸门 0：疑似机器人（bot 互聊防火墙，见 _looks_like_bot 注释）----
    # 放在最前面：连关键词和概率都不必走。注意消息照常记账（_activity），
    # 否则统计不出对方的刷屏特征。
    if _looks_like_bot(group_id, event.user_id, now):
        return

    # ---- 闸门 1：话题相关（关键词命中，或走通用通道的低倍率）----
    lowered = text.lower()
    hit_keyword = any(k.lower() in lowered for k in cfg.ai_proactive_keywords)
    if not hit_keyword and cfg.ai_proactive_generic_ratio <= 0:
        return
    # ---- 闸门 2：消息长度 ----
    if len(text) < cfg.ai_proactive_min_len:
        return
    # ---- 闸门 3：动态概率（人少高、人多低；非关键词消息再乘 generic_ratio）----
    speakers = _speakers_in_window(group_id, now)
    probability = _dynamic_probability(group_id, now)
    if not hit_keyword:
        probability *= cfg.ai_proactive_generic_ratio
    if random.random() >= probability:
        return
    # ---- 闸门 4：冷却 + 每日额度 ----
    if now - _last_send.get(group_id, 0.0) < cfg.ai_proactive_cooldown:
        return
    _roll_day_if_needed()
    if _usage[group_id] >= cfg.ai_proactive_daily_limit:
        return

    _last_send[group_id] = now
    _usage[group_id] += 1

    # ---- 即兴生图分流：概率命中就改走画图，这次不产文字 ----
    # 算作一次主动发言（冷却/日限照记），防止画图上瘾；
    # 生图是异步的，这边直接返回，图生成好会自己发出来。
    if (
        cfg.ai_imagegen_enabled
        and cfg.ai_imagegen_api_key
        and random.random() < cfg.ai_imagegen_probability
    ):
        logger.info(
            f"[ai_chat] 即兴生图触发（群 {group_id}，今日第 {_usage[group_id]} 次）: "
            f"{text[:40]}"
        )
        topic = f"{digest.current(group_id)} {text}".strip()[:200]
        # 即兴生图永远是画机器人自己的表情包，固定角色模式
        asyncio.create_task(
            imagegen.generate_and_send(bot, event, topic, mode="character")
        )
        return

    logger.info(
        f"[ai_chat] 主动发言触发（群 {group_id}，今日第 {_usage[group_id]} 次，"
        f"窗口内 {speakers} 人，概率 {probability:.2f}，"
        f"关键词{'命中' if hit_keyword else '未命中'}）: {text[:60]}"
    )

    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    # ambient 记录器（priority=65）晚于本 handler（60）执行，触发消息还没进
    # 缓冲，正好不重复——触发消息在下面单独拼进提示词
    context_lines = ambient.recent_lines(group_id, limit=8)
    context_block = "\n".join(context_lines) if context_lines else "（暂无其他记录）"
    # 滚动摘要：比 8 条原文更长的群聊记忆，让插话接得住更早的话题
    digest_ctx = digest.current(group_id) if cfg.ai_digest_enabled else ""
    digest_block = (
        f"这个群最近在聊（摘要）：\n{digest_ctx}\n\n" if digest_ctx else ""
    )
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
                f"{digest_block}"
                "最近的群聊记录：\n"
                f"{context_block}\n\n"
                f"刚出现的消息：\n【{speaker}】{text[:100]}"
            ),
        },
    ]

    try:
        from .ai_client import chat_kwargs, make_client

        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        # 120 会被 reasoning 吃到空回复，按 500 给（插话本身只要一两句）
        resp = await client.chat.completions.create(
            **chat_kwargs(
                messages,
                cfg,
                max_tokens=500,
                temperature=cfg.ai_temperature,
            )
        )
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
