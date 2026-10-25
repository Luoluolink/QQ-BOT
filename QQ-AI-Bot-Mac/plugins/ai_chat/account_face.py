"""账号收藏表情：发机器人 QQ 账号里收藏的表情包。

和本地 stickers/ 目录互补：
- 本地目录：自己丢进去的图，走 base64 发送（NapCat 在沙盒里读不了本地路径）
- 账号收藏：QQ 里收藏的表情，用 NapCat 的 fetch_custom_face 拉 URL 列表，
  发送时把 URL 交给 NapCat 自己下载——它本来就在沙盒外，拉 QQ CDN
  没有代理问题，也不用把每张图 base64 一遍撑大消息体

fetch_custom_face 不是所有 NapCat 版本都有：没有这个 action 时
call_api 会抛异常，这里全部吞掉、记录日志后返回空列表，
调用方自动退回本地表情，不影响主流程。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.adapters.onebot.v11.message import MessageSegment

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot

# 用户明确要点「账号收藏的表情」时命中的词。
# 注意这些词都含「表情」，stickers.STICKER_TRIGGERS 也会命中，
# 由调用方按优先级决定用哪边的图。
FACE_TRIGGERS = (
    "收藏表情", "收藏的表情", "收藏表情包", "扣扣表情",
    "QQ表情", "qq表情", "账号表情", "你收藏的", "你的收藏",
)

# 收藏列表缓存：拉一次管 CACHE_TTL 秒。
# 收藏表情的 CDN URL 有时效，缓存太久 URL 过期 NapCat 下载会失败，
# 10 分钟是个稳妥的折中。
CACHE_TTL = 600.0

_cache: list[str] = []
_cache_at: float = 0.0
# 并发只放一个请求进去：群里同时来两条「发个收藏表情」时，
# 后一条等前一条拉完，直接复用结果，不打两次 API
_lock = asyncio.Lock()


def is_face_request(text: str) -> bool:
    """用户是否在明确索要账号收藏表情。"""
    return bool(text) and any(word in text for word in FACE_TRIGGERS)


def _extract_urls(raw) -> list[str]:
    """从 call_api 返回值里抠出 URL 列表。

    NoneBot 的 OneBot v11 适配器正常情况下已经把 data 字段拆出来了，
    raw 直接就是 URL 数组；个别 NapCat 版本可能多裹一层 dict，
    或者数组元素是 {"url": ...} 字典，都兼容一下。
    """
    if isinstance(raw, dict):
        raw = raw.get("data", raw)
    if not isinstance(raw, list):
        return []
    urls: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.startswith("http"):
            urls.append(item)
        elif isinstance(item, dict):
            url = item.get("url") or item.get("file") or ""
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)
    return urls


async def fetch_faces(bot: Bot, count: int = 200, *, ttl: float = CACHE_TTL) -> list[str]:
    """拉取账号收藏表情 URL 列表（带 TTL 缓存）。

    失败时返回陈旧缓存（可能为空）而不是抛异常——
    发表情是锦上添花，不能因为它把整条回复带崩。
    """
    global _cache, _cache_at
    if _cache and time.time() - _cache_at < ttl:
        return _cache
    async with _lock:
        # 双重检查：等锁期间可能已经被别的请求拉过了
        if _cache and time.time() - _cache_at < ttl:
            return _cache
        try:
            raw = await bot.call_api("fetch_custom_face", count=count)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[ai_chat] 拉取账号收藏表情失败（NapCat 不支持该 API 或未登录？）: {exc!r}"
            )
            return _cache
        urls = _extract_urls(raw)
        if urls:
            _cache = urls
            _cache_at = time.time()
            logger.info(f"[ai_chat] 账号收藏表情拉取成功：{len(urls)} 个")
        elif not _cache:
            logger.info("[ai_chat] 账号收藏表情为空或返回格式异常")
        return _cache


def pick_face() -> str | None:
    """从缓存里随机挑一个 URL，没有缓存返回 None。"""
    return random.choice(_cache) if _cache else None


async def random_face(bot: Bot, count: int = 200) -> str | None:
    """拉一次收藏列表并随机挑一个 URL（对外主入口）。"""
    await fetch_faces(bot, count=count)
    return pick_face()


def face_segment(url: str) -> MessageSegment:
    """把收藏表情 URL 转成图片消息段，直接交给 NapCat 下载发送。"""
    return MessageSegment.image(url)
