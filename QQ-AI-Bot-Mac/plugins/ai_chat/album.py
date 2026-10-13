"""群相册：@机器人 + 图片 + 「存相册」→ 把图存进群相册。

触发条件（三条都要满足，宁缺毋滥）：
1. 群消息 @ 了机器人；
2. 消息里带图片段；
3. 文字里同时出现「相册」和「存/收/传」这类意图词。

为什么必须带意图词：识图功能也是 @ + 图片（直接看图聊天），
如果把所有 @+图片都拿去存相册，识图就废了。所以：
    @机器人 看看这张图      → 走识图对话
    @机器人 存相册 [图片]    → 存进群相册
    @机器人 [图片] 收进相册  → 存进群相册

实现（NapCat 扩展接口，仅 NapCat 支持）：
- get_qun_album_list        拿群相册列表，取第一个相册
- upload_image_to_qun_album file 支持 URL / 本地路径，这里优先直接传
  消息里的图片 URL；没有 URL 就 get_image 换 base64 兜底。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Dict, Tuple

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.rule import Rule, to_me

from .config import Config
from .tools import at_me

cfg = get_plugin_config(Config)

# 「相册」和「存/收/传」必须同时出现才算存图意图
ALBUM_RE = re.compile(r"相册")
ALBUM_VERB_RE = re.compile(r"存|收|传|保存|放进|丢进|上交")


async def _album_rule(bot: Bot, event: MessageEvent) -> bool:
    """@机器人（任意位置）+ 带图 + 文字里有「相册」+「存/收/传」。"""
    if not cfg.ai_album_enabled:
        return False
    if not isinstance(event, GroupMessageEvent):
        return False
    if not at_me(event, bot.self_id):
        return False
    text = event.get_plaintext()
    if not (ALBUM_RE.search(text) and ALBUM_VERB_RE.search(text)):
        return False
    return any(seg.type == "image" for seg in event.message)


album = on_message(rule=Rule(_album_rule), priority=10, block=True)

# 每群每日限额：{(group_id, "YYYY-MM-DD"): 已存张数}
_daily: Dict[Tuple[int, str], int] = {}


def _read_local_sync(path: str) -> bytes:
    """读本地图片文件（NapCat 和机器人同机，get_image 给的路径可直接读）。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


async def _image_payload(bot: Bot, seg) -> str:
    """把图片段变成 upload 接口能吃的 file 参数。

    优先级：消息里的 URL > get_image 换出来的 URL > 本地路径转 base64://。
    全失败返回空串。
    """
    url = seg.data.get("url") or ""
    if str(url).startswith("http"):
        return str(url)

    fid = seg.data.get("file") or seg.data.get("file_id") or ""
    if not fid:
        return ""
    try:
        info = await asyncio.wait_for(bot.call_api("get_image", file=fid), timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning(f"[ai_chat] get_image({fid}) 超时")
        info = None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] get_image({fid}) 失败: {exc!r}")
        info = None
    if not isinstance(info, dict):
        return ""
    data = info.get("data", info) or {}
    url2 = data.get("url") or ""
    if str(url2).startswith("http"):
        return str(url2)
    path = data.get("path") or data.get("file") or ""
    raw = await asyncio.to_thread(_read_local_sync, str(path)) if path else b""
    if raw:
        return "base64://" + base64.b64encode(raw).decode()
    return ""


def _pick_album(result) -> tuple[str, str]:
    """从 get_qun_album_list 的返回里挑第一个相册，返回 (album_id, album_name)。

    NapCat 4.18+ 返回 {"album_list": [...], "attach_info": ..., "has_more": ...}，
    列表包在 album_list 键里；老实现可能直接给数组，两种都兼容。
    """
    data = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(data, dict):
        data = data.get("album_list") or data.get("albums") or data.get("list") or []
    items = data if isinstance(data, list) else []
    for it in items:
        if not isinstance(it, dict):
            continue
        aid = str(it.get("album_id") or it.get("id") or "")
        if aid:
            return aid, str(it.get("album_name") or it.get("name") or "")
    return "", ""


@album.handle()
async def _handle_save(bot: Bot, event: GroupMessageEvent) -> None:
    gid = event.group_id
    today = time.strftime("%Y-%m-%d")

    # 顺手清掉过期计数，防止字典越长越大
    if len(_daily) > 256:
        for key in [k for k in _daily if k[1] != today]:
            _daily.pop(key, None)

    used = _daily.get((gid, today), 0)
    if used >= cfg.ai_album_daily_limit:
        await album.finish("今天的相册额度用完啦（防刷屏），明天再来存吧～")

    imgs = [seg for seg in event.message if seg.type == "image"][
        : cfg.ai_album_max_images
    ]
    if not imgs:  # 规则里已保证有图，这里纯防御
        return

    # ---- 找相册：取列表里第一个 ----
    try:
        # 必须套 wait_for：NapCat 不支持这个扩展接口时可能永不回应，
        # 不加超时 handler 会静默挂死（不回复也不报错），还会挡住后面的对话匹配器
        picked = await asyncio.wait_for(
            bot.call_api("get_qun_album_list", group_id=str(gid)), timeout=10.0
        )
    except asyncio.TimeoutError:
        logger.warning("[ai_chat] 获取群相册列表超时(10s)：NapCat 可能不支持该接口")
        await album.finish("拿不到群相册列表（接口超时），可能这个 NapCat 版本还不支持相册功能～")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 获取群相册列表失败: {exc!r}")
        await album.finish("拿不到群相册列表，可能这个 NapCat 版本还不支持相册功能～")
    album_id, album_name = _pick_album(picked)
    if not album_id:
        await album.finish("这个群还没有相册，先在群里手动建一个，再来让我存图吧～")

    # ---- 逐张上传 ----
    ok, fail = 0, 0
    for seg in imgs:
        payload = await _image_payload(bot, seg)
        if not payload:
            fail += 1
            continue
        try:
            await asyncio.wait_for(
                bot.call_api(
                    "upload_image_to_qun_album",
                    group_id=str(gid),
                    album_id=album_id,
                    album_name=album_name,
                    file=payload,
                ),
                timeout=30.0,
            )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 上传群相册失败: {exc!r}")
            fail += 1

    if ok:
        _daily[(gid, today)] = used + ok
        text = f"已把 {ok} 张图存进群相册「{album_name or '默认相册'}」啦 ✅"
        if fail:
            text += f"\n另有 {fail} 张没存上，可能是图片失效了"
        await album.finish(text)
    else:
        await album.finish(
            "图没存上……可能是图片链接失效或者相册满了，重新发一次试试？"
        )
