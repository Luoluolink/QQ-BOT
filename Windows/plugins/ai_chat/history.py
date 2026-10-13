"""群历史消息检索：把最近的消息拉下来建本地索引，支持关键词搜索。

用途：「机器人，刚才谁说过 XX」这类翻聊天记录的需求。

实现要点：
- NapCat 的 get_group_msg_history 按 message_seq 分页，message_seq=0 表示最新一页；
  往前翻就取当前页里最小的 message_seq 再减一。
- 只建内存索引（带 TTL），不落盘 —— 群消息本来就容易拉到，缓存过期重拉即可。
- 和群资源检索同一铁律：给模型看的资料必须真实存在，绝不编造。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from .tools import fmt_time, truncate


@dataclass
class HistoryEntry:
    seq: int
    ts: int
    user_id: int
    name: str
    text: str


@dataclass
class HistoryIndex:
    group_id: int
    entries: List[HistoryEntry] = field(default_factory=list)
    error: str = ""
    fetched_at: float = 0.0


_cache: Dict[int, HistoryIndex] = {}


def get_cached(group_id: int, ttl: int) -> HistoryIndex | None:
    """TTL 内直接用缓存；过期返回 None。"""
    index = _cache.get(group_id)
    if index and not index.error and time.time() - index.fetched_at < ttl:
        return index
    return None


def _segments_to_text(message: list) -> str:
    """把原始消息段数组拼成可读文本，@ 还原成 @昵称 的样子。"""
    parts: List[str] = []
    for seg in message or []:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            t = (data.get("text") or "").strip()
            if t:
                parts.append(t)
        elif seg_type == "at":
            parts.append(f"@{data.get('nickname') or data.get('qq') or ''}".strip())
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
    return "".join(parts).strip()


async def build_index(
    bot: Bot, group_id: int, max_fetch: int = 300
) -> HistoryIndex:
    """从最新一条往前拉 max_fetch 条群消息，建一个可搜索的索引。

    分页说明：第一页传 message_seq=0（NapCat 语义是「从最新开始」），
    之后取本页最小 seq - 1 继续。任何一页拉空或拉不到 seq 就提前收工。
    """
    index = HistoryIndex(group_id=group_id)
    seen_seqs: set[int] = set()
    page_size = 100
    seq = 0  # 0 = 从最新开始

    try:
        while len(index.entries) < max_fetch:
            resp = await bot.call_api(
                "get_group_msg_history",
                group_id=group_id,
                message_seq=seq,
                count=page_size,
            )
            messages = (resp or {}).get("messages") or []
            if not messages:
                break

            page_seqs: List[int] = []
            for m in messages:
                raw_seq = m.get("message_seq") or m.get("message_id") or 0
                try:
                    raw_seq = int(raw_seq)
                except (TypeError, ValueError):
                    continue
                if raw_seq in seen_seqs:
                    continue
                seen_seqs.add(raw_seq)
                page_seqs.append(raw_seq)

                sender = m.get("sender") or {}
                name = (
                    sender.get("card")
                    or sender.get("nickname")
                    or str(sender.get("user_id") or "未知")
                )
                text = _segments_to_text(m.get("message") or [])
                if not text:
                    text = (m.get("raw_message") or "").strip() or "[不支持的消息类型]"
                index.entries.append(
                    HistoryEntry(
                        seq=raw_seq,
                        ts=int(m.get("time") or 0),
                        user_id=int(sender.get("user_id") or 0),
                        name=name,
                        text=text,
                    )
                )

            # 翻页游标：本页最小 seq - 1。拿不到游标就说明翻不动了。
            if not page_seqs:
                break
            nxt = min(page_seqs) - 1
            if nxt <= 0 or (seq and nxt >= seq):
                break
            seq = nxt

        index.entries.reverse()  # 时间正序，展示和检索都更直观
        index.fetched_at = time.time()
        logger.info(
            f"[ai_chat] 群 {group_id} 历史索引完成：{len(index.entries)} 条"
        )
    except Exception as exc:  # noqa: BLE001
        index.error = f"拉取群历史消息失败：{exc!r}"
        logger.warning(f"[ai_chat] {index.error}")
    return index


async def get_index(
    bot: Bot, group_id: int, ttl: int = 300, force: bool = False,
    max_fetch: int = 300,
) -> HistoryIndex:
    """带缓存的入口。force=True 时无视 TTL 强制重建。"""
    if not force:
        cached = get_cached(group_id, ttl)
        if cached is not None:
            return cached
    index = await build_index(bot, group_id, max_fetch=max_fetch)
    if not index.error:
        _cache[group_id] = index
    return index


def invalidate(group_id: int) -> None:
    """让某个群的缓存作废（重建成员名单之类场景用不上，但留着保险）。"""
    _cache.pop(group_id, None)


def search(index: HistoryIndex, keyword: str, limit: int = 15) -> List[HistoryEntry]:
    """不区分大小写的子串匹配，按时间正序返回。"""
    kw = keyword.strip().lower()
    if not kw:
        return []
    hits = [e for e in index.entries if kw in e.text.lower()]
    return hits[-limit:]


def render_hits(hits: List[HistoryEntry]) -> str:
    """把检索结果渲染成群聊友好的多行文本。"""
    lines: List[str] = []
    for e in hits:
        lines.append(f"{fmt_time(e.ts)} {e.name}：{truncate(e.text, 150)}")
    return "\n".join(lines)
