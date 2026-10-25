"""群聊滚动摘要（digest）：把群里的闲聊持续压缩成一份短摘要。

解决的问题：机器人对群上下文的感知原本只有两块——
_recent 环形缓冲（最近 8 条，仅搭话时用）和 _history（只记与机器人的对话）。
群友之间聊了什么它完全不知道，所以搭话经常接不上梗，被 @ 时也不知道
「刚才那个游戏」是什么。本模块补上这块：群聊滚动压缩成一份 200 字上下的
摘要，搭话和被 @ 时注入，让机器人有「群友视角」。

和其它记忆体系的分工：
- digest（本模块）  —— 「这个群最近在聊什么」，粗粒度、连续、自动维护
- history.py         —— 「刚才谁说过 XX 具体是哪句」，细粒度、按需检索
- memory.py          —— 「明确交代过的长期事实」，/记住 手动存

成本：每群默认累计 30 条新消息且距上次压缩 ≥3 分钟才压一次，
一次压缩 ≈ 输入 1.5k token + 输出 200 字，日成本几分钱量级。
摘要落盘 data/group_digest.json，重启不丢；未压缩的缓冲在内存，重启重攒。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import DefaultDict, Deque, Dict

from nonebot import get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)

# 项目根目录下的 data/（与 group_memory.json 同目录，已 gitignore）
_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "group_digest.json"
)

# ---------- 进程内状态 ----------
# 每群当前摘要（None = 还没有）
_digest: Dict[int, str] = {}
# 每群未压缩的新消息缓冲（带发言人前缀，和 _recent 同格式）
_pending: DefaultDict[int, Deque[str]] = defaultdict(lambda: deque(maxlen=120))
# 每群上次压缩时间 / 今日已压次数 / 今日日期
_last_compress: Dict[int, float] = {}
_usage: DefaultDict[int, int] = defaultdict(int)
_usage_day: str = date.today().isoformat()
# 落盘节流：距上次写盘超过这个秒数才真正写（压缩成功后立即写一次）
_last_save: float = 0.0


def _roll_day_if_needed() -> None:
    global _usage_day
    today = date.today().isoformat()
    if today != _usage_day:
        _usage.clear()
        _usage_day = today


def _load() -> None:
    """启动时把盘上的摘要读进内存。文件不存在/损坏都静默从空开始。"""
    global _last_save
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        groups = (raw or {}).get("groups") or {}
        for key, value in groups.items():
            if isinstance(key, str) and key.lstrip("-").isdigit() and isinstance(value, str) and value:
                _digest[int(key)] = value
    except (OSError, ValueError):
        pass
    _last_save = time.time()


def _save(force: bool = False) -> None:
    """摘要落盘。默认节流（10 秒内不重复写），force 跳过节流。"""
    global _last_save
    now = time.time()
    if not force and now - _last_save < 10.0:
        return
    _last_save = now
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"groups": {str(k): v for k, v in _digest.items()}}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)  # 原子替换，避免写一半损坏
    except OSError as exc:
        logger.warning(f"[ai_chat] 群摘要落盘失败: {exc!r}")


_load()


def observe(group_id: int, speaker: str, text: str, now: float) -> None:
    """记一条群消息（无条件调用，和 proactive 的 _recent 记录放一起）。

    只记群友的闲聊，不判断是否值得压缩——判断在压缩时做。
    """
    if not text:
        return
    _pending[group_id].append(f"【{speaker}】{text[:80]}")


def current(group_id: int) -> str:
    """本群当前摘要，没有则返回空串。"""
    return _digest.get(group_id, "")


def _ready(group_id: int, now: float) -> bool:
    """是否到了该压缩的时候：缓冲攒够 batch 条，且距上次压缩超过 interval 秒。"""
    if len(_pending[group_id]) < cfg.ai_digest_batch:
        return False
    return now - _last_compress.get(group_id, 0.0) >= cfg.ai_digest_interval


def _build_messages(group_id: int) -> list[dict]:
    """组装压缩请求：旧摘要 + 待压缩的新消息。"""
    old = _digest.get(group_id, "")
    new_lines = "\n".join(_pending[group_id])
    system = (
        "你是群聊记录整理员。把「已有摘要」和「新聊天记录」融合成一份新摘要。\n"
        "要求：\n"
        "1. 凝练的中文白话，200 字以内\n"
        "2. 保留：正在聊的话题、每个人的立场或关系、没聊完的悬念、群里的梗和笑点\n"
        "3. 忽略：打招呼、复读、无信息量的口水\n"
        "4. 用「今晚」「刚才」这类自然时间感，不要罗列时间戳\n"
        "5. 只输出摘要本身，不要任何解释和前缀"
    )
    user = (
        (f"已有摘要：\n{old}\n\n" if old else "（暂无已有摘要）\n\n")
        + f"新聊天记录：\n{new_lines}\n\n"
        "请输出融合后的新摘要。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# 每群一把锁：maybe_compress 是 create_task 出来的后台任务，消息密集时
# 可能同时起两个，都把同一批 pending 快照走、都去 popleft——第二个就会
# 在空队列上抛 IndexError（实测 bot.err.log 里刷过一串）。锁住整个压缩
# 过程，并发任务串行后自然过期退出。
_locks: DefaultDict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


async def compress(group_id: int, now: float | None = None) -> str | None:
    """到期则压缩：调模型把 pending 并进摘要。返回新摘要，没必要压或失败返回 None。

    设计为后台任务（调用方 asyncio.create_task），失败只警告不抛出——
    摘要压缩是增强功能，不能因为它挂了影响正常聊天。
    """
    now = now or time.time()
    async with _locks[group_id]:
        return await _compress_locked(group_id, now)


async def _compress_locked(group_id: int, now: float) -> str | None:
    if not cfg.ai_digest_enabled or not _ready(group_id, now):
        return None
    _roll_day_if_needed()
    if _usage[group_id] >= cfg.ai_digest_daily_limit:
        return None

    batch = list(_pending[group_id])
    try:
        from .ai_client import chat_kwargs, make_client

        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        resp = await client.chat.completions.create(
            **chat_kwargs(
                _build_messages(group_id),
                cfg,
                max_tokens=500,
                temperature=0.3,
            )
        )
    except Exception as exc:  # noqa: BLE001
        # 不清 pending：下次触发时会带着这批消息重试，顶多多压一次
        logger.warning(f"[ai_chat] 群 {group_id} 摘要压缩失败（保留待压消息）: {exc!r}")
        return None

    summary = (resp.choices[0].message.content or "").strip()
    if not summary:
        return None
    # 防呆：模型偶尔会把思考过程也吐出来，超长一律截断（宁可丢尾也别把
    # 几百字废话注进上下文）
    if len(summary) > cfg.ai_digest_max_chars:
        summary = summary[: cfg.ai_digest_max_chars - 1] + "…"

    _digest[group_id] = summary
    # 防御性弹出：快照与弹出之间理论上有别的入口动过队列（锁内不该发生，
    # 但坏数据宁可少弹也不能崩）
    for _ in batch:
        if not _pending[group_id]:
            break
        _pending[group_id].popleft()
    _last_compress[group_id] = now
    _usage[group_id] += 1
    _save(force=True)
    logger.info(
        f"[ai_chat] 群 {group_id} 摘要已更新（第 {_usage[group_id]} 次，"
        f"{len(summary)} 字）: {summary[:80]}"
    )
    return summary


def maybe_compress(group_id: int) -> None:
    """非阻塞入口：到了压缩时机就起个后台任务去做，绝不拖慢当前回复。"""
    if not cfg.ai_digest_enabled:
        return
    if not _ready(group_id, time.time()):
        return
    asyncio.create_task(compress(group_id))


def stats(group_id: int) -> dict:
    """给 /状态 指令看的用量数据。"""
    return {
        "used": _usage.get(group_id, 0),
        "has_digest": bool(_digest.get(group_id)),
        "pending": len(_pending.get(group_id, ())),
        "speakers_hint": cfg.ai_digest_batch,
    }
