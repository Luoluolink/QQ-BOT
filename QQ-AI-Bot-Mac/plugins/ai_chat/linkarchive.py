"""链接收藏库：群里的链接自动归档 + 检索 + 同步进在线文档。

群聊里最有价值的资产就是散落的链接，但发完就沉底了。本模块做三件事：

1. 归档：监听群消息，抽 URL，抓标题，去重后存 data/link_archive.json；
2. 检索：/链接 关键词 命令查收藏；对话时问「之前谁发过 xx 的链接」
   自动把相关条目注入上下文；
3. 上文档：新条目触发 docsync.append_entry，按群绑定文档的现有格式
   追加进去（没绑文档/没配 API 就只存本地，静默跳过）。

设计取舍：
- 标题抓取超时 8 秒、失败就用域名兜底，绝不为抓标题拖住消息处理；
- 同一 URL 在同群只收一次（b23.tv 短链展开后按真实地址判重）；
- b23.tv / t.bilibili.com 这类短链会先展开拿真实标题。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot import get_plugin_config, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.params import CommandArg
from nonebot.rule import Rule, to_me

from . import crawler, docsync
from .config import Config

cfg = get_plugin_config(Config)

_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "link_archive.json"

_groups: dict[str, list[dict[str, Any]]] | None = None
# 每群已收录 URL 的内存集合（启动时从盘里重建），判重用
_seen: dict[str, set[str]] = {}


def _load() -> dict[str, list[dict[str, Any]]]:
    global _groups
    if _groups is None:
        raw_groups: Any = {}
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_groups = data.get("groups") or {}
        except (OSError, ValueError):
            raw_groups = {}
        _groups = {}
        for key, value in raw_groups.items():
            if not isinstance(value, list):
                continue
            clean = [
                e
                for e in value
                if isinstance(e, dict) and isinstance(e.get("url"), str) and e["url"]
            ]
            if clean:
                _groups[str(key)] = clean
        _seen.clear()
        for key, items in _groups.items():
            _seen[key] = {e["url"] for e in items}
    return _groups


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps({"groups": _groups or {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def count(gid: int) -> int:
    return len(_load().get(str(gid), []))


# ================================================================ 抓取


def normalize_url(raw: str) -> str:
    """去掉尾部标点、统一 http；短链域名保留原样（判重靠展开后的标题不可靠，
    这里直接把 b23.tv 当作独立条目，标题抓取时自然跟随重定向）。"""
    url = raw.strip().rstrip("。，,;；!！?？)）】」》>…·")
    if not url:
        return ""
    return url


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    return m.group(1).removeprefix("www.") if m else url[:30]


def _title_blocking(url: str, timeout: float) -> str:
    """抓网页 <title>。crawler._page_title_sync 会跟随重定向，b23.tv 直接用。"""
    try:
        title = crawler._page_title_sync(url, timeout, "")
    except Exception:  # noqa: BLE001
        return ""
    return (title or "").strip()


async def fetch_title(url: str) -> str:
    timeout = cfg.ai_linkarchive_timeout
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_title_blocking, url, timeout),
            timeout=timeout + 2.0,
        )
    except asyncio.TimeoutError:
        return ""


# ================================================================ 归档


def add(gid: int, url: str, title: str, by: str, max_entries: int = 500) -> dict[str, Any] | None:
    """登记一条链接。重复返回 None，成功返回条目。"""
    items = _load().setdefault(str(gid), [])
    seen = _seen.setdefault(str(gid), set())
    if url in seen:
        return None
    entry: dict[str, Any] = {
        "url": url,
        "title": title or _domain(url),
        "by": by,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    items.append(entry)
    if len(items) > max_entries:
        removed = items[: len(items) - max_entries]
        del items[: len(items) - max_entries]
        seen -= {e["url"] for e in removed}
    seen.add(url)
    _save()
    return entry


async def _archive_urls(event: GroupMessageEvent, urls: list[str]) -> None:
    gid = event.group_id
    sender = event.sender
    by = (getattr(sender, "card", "") or getattr(sender, "nickname", "") or "").strip() or f"QQ{event.user_id}"
    for raw in urls:
        url = normalize_url(raw)
        if not url or url in _seen.get(str(gid), set()):
            continue
        title = await fetch_title(url)
        entry = add(gid, url, title, by, cfg.ai_linkarchive_max_per_group)
        if entry is None:
            continue
        logger.info(f"[ai_chat] 链接收藏: {entry['title'][:40]} <- {url[:60]}")
        # 同步进在线文档前先过过滤闸（本地收藏库不受影响，永远全收）：
        # whitelist 模式下只有白名单域名才写文档，manual 模式全靠管理员手动收录。
        # 没绑定/没配置会在 append_entry 里返回状态，静默跳过。
        ok, reason = docsync.should_sync(gid, url)
        if not ok:
            logger.info(f"[ai_chat] 链接未同步在线文档（{reason}）: {url[:60]}")
            continue
        try:
            status = await docsync.append_entry(gid, entry["title"], url, by)
            if status not in ("ok", "dup", "未绑定", "未配置"):
                logger.warning(f"[ai_chat] 链接同步在线文档失败: {status}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 链接同步在线文档异常: {exc!r}")


# ================================================================ 检索


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text.lower())
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def search(gid: int, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    kw = keyword.strip().lower()
    if not kw:
        return []
    items = _load().get(str(gid), [])
    scored: list[tuple[int, int, dict[str, Any]]] = []  # (分数, 序号, 条目)
    for i, e in enumerate(items):
        hay = f"{e.get('title') or ''} {e.get('url') or ''}".lower()
        if kw in hay:
            scored.append((1000, i, e))
            continue
        overlap = _bigrams(kw) & _bigrams(hay)
        if overlap:
            scored.append((len(overlap), i, e))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]))  # 同分取最新
    return [e for _, _, e in scored[:limit]]


def recent(gid: int, limit: int = 10) -> list[dict[str, Any]]:
    return list(_load().get(str(gid), []))[-limit:]


def describe(gid: int, text: str, limit: int = 4) -> str:
    """对话注入：用户在翻旧链接时给出相关收藏。"""
    t = text.strip().lower()
    if not t:
        return ""
    if not ("链接" in t or "网址" in t or "发过" in t or "分享过" in t or "收藏" in t):
        return ""
    kw = re.sub(
        r"链接|网址|发过|分享过|收藏|之前|那个|哪个|谁|吗|？|\?|看看|翻翻|翻|都|过|来|去|想|要|找|的|了",
        " ",
        text,
    ).strip()
    if len(kw) < 2:
        # 没给内容词就给最近几条
        hits = recent(gid, limit)
    else:
        hits = search(gid, kw, limit)
    if not hits:
        return ""
    lines = ["【链接收藏库】本群以前发过的相关链接："]
    for i, e in enumerate(hits, 1):
        lines.append(
            f"{i}. {e.get('title', '')[:40]}｜{e.get('by', '')} 于 {e.get('time', '')} 发｜{e['url']}"
        )
    return "\n".join(lines)


# ================================================================ 监听 + 指令

# 抓 URL 用 crawler 的现成正则
_URL_RE = crawler._URL_PAT

# 这些域名九成是机器人自己发的分享卡片/音乐链接之外的场景，照收；
# 但 QQ 图片 CDN 的图片消息不算链接分享
_SKIP_URL_PAT = re.compile(r"https?://(gchat\.qpic\.cn|multimedia\.nt\.qq\.com\.cn|gchat\.qq\.com)")


def _rule(event: GroupMessageEvent) -> bool:
    if not cfg.ai_linkarchive_enabled:
        return False
    text = event.get_plaintext()
    if not text or text.lstrip().startswith(("/", "！")):
        return False
    for m in _URL_RE.finditer(text):
        u = m.group(0)
        if not _SKIP_URL_PAT.search(u):
            return True
    return False


collector = on_message(rule=Rule(_rule), priority=55, block=False)


@collector.handle()
async def _handle_collect(event: GroupMessageEvent) -> None:
    urls = [
        m.group(0)
        for m in _URL_RE.finditer(event.get_plaintext())
        if not _SKIP_URL_PAT.search(m.group(0))
    ]
    if urls:
        try:
            await _archive_urls(event, urls[:5])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 链接收藏任务失败: {exc!r}")


link_cmd = on_command("链接", rule=to_me(), priority=10, block=True)


@link_cmd.handle()
async def _handle_link(event: GroupMessageEvent, args = CommandArg()) -> None:
    kw = args.extract_plain_text().strip()
    if kw:
        hits = search(event.group_id, kw, limit=5)
        if not hits:
            await link_cmd.finish(f"收藏里没有和「{kw}」相关的链接")
        lines = [f"找到 {len(hits)} 条相关链接："]
    else:
        hits = recent(event.group_id, limit=10)
        if not hits:
            await link_cmd.finish("收藏库还是空的——群里发个链接试试，我会自动收进来")
        lines = [f"最近 {len(hits)} 条收藏："]
    for i, e in enumerate(hits, 1):
        lines.append(f"{i}. {e.get('title', '')[:40]}\n   {e['url']}\n   {e.get('by', '')} · {e.get('time', '')}")
    await link_cmd.finish("\n".join(lines))
