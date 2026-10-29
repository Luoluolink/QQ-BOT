"""群聊长期记忆（第一档功能）。

和对话历史的区别：
- 历史（__init__ 里的 _history）是「最近 8 轮说了什么」，随对话滚动，
  超出就忘，重启就清；
- 记忆（本模块）是「明确要长期记住的事实」，落盘 data/group_memory.json，
  重启不丢，每次对话自动按相关性注入上下文。

设计上刻意保持简单：纯 JSON 存储 + 二字组（bigram）相关性打分，
不引入向量库 / 嵌入模型。理由写在 config.py 的注释里：
群聊记忆条目通常几十条以内，关键词匹配已经够用，
多装一个依赖就多一分「装不上跑不起来」的风险。

用法：
    /记住 张三的生日是 1 月 1 日   —— 任何人可添加
    /记忆                          —— 列出本群全部记忆
    /忘记 张三                     —— 管理员删除匹配条目
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any

from nonebot import get_plugin_config, logger

from .ai_client import chat_kwargs, make_client
from .config import Config

cfg = get_plugin_config(Config)

# 项目根目录下的 data/（已在 .gitignore 里忽略，和 web_search_usage.json 同目录）
_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "group_memory.json"
)

# 进程内缓存：读操作全走缓存不打磁盘；写操作改完缓存立即落盘
_groups: dict[str, list[dict[str, Any]]] | None = None


def _load() -> dict[str, list[dict[str, Any]]]:
    global _groups
    if _groups is None:
        data: dict[str, Any] = {}
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            # 文件不存在或内容损坏都不算错误，从空开始记
            data = {}
        raw_groups = data.get("groups") or {}
        # 兼容脏数据：只保留结构正确的条目，坏文件不至于让插件崩溃
        _groups = {}
        for key, value in raw_groups.items():
            if not isinstance(value, list):
                continue
            clean = [
                e
                for e in value
                if isinstance(e, dict) and isinstance(e.get("text"), str) and e["text"]
            ]
            if clean:
                _groups[str(key)] = clean
    return _groups


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"groups": _groups or {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # 落盘失败只影响「重启后还在」，不影响本轮对话，不抛出
        pass


def count(group_id: int) -> int:
    return len(_load().get(str(group_id), []))


def add(group_id: int, text: str, by: str, max_entries: int = 50) -> str:
    """添加一条记忆。返回结果说明（'ok' / 'dup' / 'full'）。

    重复判定用「去掉空白后完全一致」：宁可让用户换种说法多存一条，
    也不把「张三生日1月1日」和「张三 生日是 1 月 1 日」误判成重复。
    """
    text = " ".join(text.split())
    items = _load().setdefault(str(group_id), [])
    if any(e["text"] == text for e in items):
        return "dup"
    items.append(
        {
            "text": text,
            "by": by,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    # 超出上限挤掉最旧的（列表按时间天然有序）
    while len(items) > max_entries:
        items.pop(0)
    _save()
    return "ok"


def remove(group_id: int, keyword: str) -> int:
    """删除正文中包含关键词的条目，返回删掉几条。"""
    keyword = " ".join(keyword.split()).lower()
    if not keyword:
        return 0
    items = _load().get(str(group_id), [])
    kept = [e for e in items if keyword not in e["text"].lower()]
    removed = len(items) - len(kept)
    if removed:
        if kept:
            _groups[str(group_id)] = kept
        else:
            _groups.pop(str(group_id), None)
        _save()
    return removed


def clear(group_id: int) -> int:
    """清空本群全部记忆，返回删掉几条。"""
    items = _load().pop(str(group_id), [])
    if items:
        _save()
    return len(items)


def list_text(group_id: int, limit: int = 30) -> str:
    """给用户看的记忆列表（编号 + 内容 + 记录人）。"""
    items = _load().get(str(group_id), [])
    if not items:
        return "这个群我还没有记住任何东西，用 /记住 XXX 教我～"
    start = max(0, len(items) - limit)
    lines = []
    for i, e in enumerate(items[start:], start=start + 1):
        lines.append(f"{i}. {e['text']}（{e.get('by', '?')} {e.get('time', '')}）")
    header = f"本群共 {len(items)} 条记忆：\n" if len(items) <= limit else (
        f"本群共 {len(items)} 条，只显示最近 {limit} 条：\n"
    )
    return header + "\n".join(lines)


def _bigrams(s: str) -> set[str]:
    """把文本切成二字组集合，做轻量相关性匹配。

    用二字组而不是单字：「生日」和「日出」共享「日」但不是一回事；
    共享「生日」这个二字组才是真相关。
    """
    s = "".join(s.split())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def describe(group_id: int, text: str, limit: int = 8) -> str:
    """把相关记忆拼成注入给模型的上下文。没有记忆返回空串。

    选法：和这条消息相关的（共享二字组）优先；一条都不相关时
    取最近的几条兜底——最近记住的事大概率正是群里在聊的。
    """
    items = _load().get(str(group_id), [])
    if not items:
        return ""
    query = _bigrams(text.lower())
    hits = []
    for e in items:
        grams = _bigrams(e["text"].lower())
        if query and grams:
            overlap = len(query & grams)
            if overlap > 0:
                hits.append((overlap, e))
    hits.sort(key=lambda pair: -pair[0])
    picked = [e for _, e in hits[:limit]]
    if not picked:
        picked = items[-limit:]
    lines = [f"- {e['text']}" for e in picked]
    return (
        "【长期记忆】以下是之前明确要求记住的事实，回答相关问题时必须以它们为准，"
        "记忆里没有的就说不知道，绝不能编造或混淆：\n" + "\n".join(lines)
    )


# ---------------------------------------------------------------- 自动记忆提取
#
# 「/记住」的入口被斜杠命令总闸关掉后，记忆必须自己长出来：
# 群聊攒够一批就后台调一次模型，提炼「值得长期记住的事实」进同一个库。
# 三重节流：条数门槛 + 冷却 + 每日上限——提取是锦上添花，不能变成账单刺客。

_pending: dict[int, deque[str]] = defaultdict(lambda: deque(maxlen=16))
_last_auto: dict[int, float] = {}
_daily: dict[tuple[str, str], int] = {}
_inflight: set[int] = set()


def note(group_id: int, speaker: str, text: str) -> None:
    """把一句群聊记进提取缓冲。由聊天回复流程顺手调用，不做任何重活。"""
    t = " ".join(text.split())
    if t:
        _pending[group_id].append(f"{speaker}：{t[:80]}")


async def maybe_auto_extract(group_id: int) -> None:
    """攒够一批就后台提取一次。所有门槛不满足直接返回，绝不阻塞聊天。"""
    if not cfg.ai_memory_auto_enabled or group_id in _inflight:
        return
    buf = _pending.get(group_id)
    if not buf or len(buf) < cfg.ai_memory_auto_batch:
        return
    now = time.time()
    if now - _last_auto.get(group_id, 0) < cfg.ai_memory_auto_cooldown:
        return
    today = date.today().isoformat()
    if _daily.get((str(group_id), today), 0) >= cfg.ai_memory_auto_daily:
        return
    lines = list(buf)
    _inflight.add(group_id)
    try:
        facts = await _extract_facts(group_id, lines)
        for fact in facts:
            add(group_id, fact, by="自动记忆")
        _last_auto[group_id] = time.time()
        _daily[(str(group_id), today)] = _daily.get((str(group_id), today), 0) + 1
        buf.clear()
        if facts:
            logger.info(f"[ai_chat] 自动记忆 +{len(facts)}（群 {group_id}）：{facts}")
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 自动记忆提取失败（不影响聊天）: {exc!r}")
    finally:
        _inflight.discard(group_id)


async def _extract_facts(group_id: int, lines: list[str]) -> list[str]:
    """让模型从群聊片段里挑 0~3 条值得长期记住的事实。"""
    client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    prompt = (
        "下面是一段 QQ 群聊天记录。如果里面有值得长期记住的、关于具体某个人的事实"
        "（喜好、生日、宠物、正在做什么、重要计划、显著经历……），提炼成最多 3 条一句话事实。\n"
        "要求：每条以说话人标记开头（照抄记录里冒号前的部分）；只记事实，寒暄和玩梗不要记；"
        "和已有记忆重复的不要；没有值得记的就输出 []。\n"
        "只输出 JSON 数组，格式如 [\"事实1\", \"事实2\"]，不要输出任何其他文字。\n\n"
        f"已有记忆（不要与这些重复）：\n{list_text(group_id, 30) or '（暂无）'}\n\n"
        "聊天记录：\n" + "\n".join(lines)
    )
    resp = await client.chat.completions.create(
        **chat_kwargs(
            [{"role": "user", "content": prompt}],
            cfg,
            max_tokens=800,   # 模型开着思考时，300 会被思考吃光导致正文为空
            temperature=0.2,
        )
    )
    reply = (resp.choices[0].message.content or "").strip()
    reply = re.sub(r"^```(?:json)?|```$", "", reply, flags=re.M).strip()
    data = json.loads(reply)
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(" ".join(item.split())[:120])
    return out[:3]
