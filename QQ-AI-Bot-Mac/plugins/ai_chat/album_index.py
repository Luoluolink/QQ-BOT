"""群相册内容索引：存图时生成一句话描述，之后按内容找图。

QQ 相册本身只能按相册名/文件名找，按「图片内容」找是官方都没有的能力。
实现思路：album.py 存图成功后，把图片丢给识图模型生成不超过 25 字的
内容描述（"雪山日落""聊天记录截图"），连同相册名/URL/存图人一起落盘
data/album_index.json。之后：

    /找图 雪山       —— 命令检索
    @bot 那张雪山的图在哪个相册   —— 对话时自动注入相关条目

设计取舍：
- 描述生成是「尽力而为」：模型不支持识图 / 超时 / 报错都只留空描述，
  索引照建（还能按相册名和日期找），绝不为这个挡住存图主流程；
- 检索继续用二字组打分（和 memory.py 同一套思路），零新依赖；
- 图片 URL 是 QQ CDN 的带 rkey 地址，会过期——命中后重发图片是
  「尽力而为」，过期就只报相册位置和描述。

注：描述生成用自己的 AsyncOpenAI 客户端而不是复用 __init__ 的
_create_completion，因为那是「对话链路」的函数，挂进来会有循环导入；
这里就是个一次性的多模态短请求。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot.exception import FinishedException
from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.params import CommandArg
from nonebot.rule import to_me

from .config import Config

cfg = get_plugin_config(Config)

_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "album_index.json"

_groups: dict[str, list[dict[str, Any]]] | None = None

# 描述生成的超时（识图比纯文本慢，给足余量）
_DESCRIBE_TIMEOUT = 45.0


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


# ================================================================ 写入


def record(
    gid: int,
    album_id: str,
    album_name: str,
    url: str,
    by: str,
    max_entries: int = 300,
) -> dict[str, Any] | None:
    """登记一张已存进相册的图（描述先留空）。URL 重复不重复记。返回条目或 None。"""
    items = _load().setdefault(str(gid), [])
    if any(e.get("url") == url for e in items):
        return None
    entry: dict[str, Any] = {
        "url": url,
        "album_id": str(album_id),
        "album_name": album_name,
        "desc": "",
        "by": by,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    items.append(entry)
    if len(items) > max_entries:
        del items[: len(items) - max_entries]  # 挤掉最旧的
    _save()
    return entry


def set_desc(gid: int, url: str, desc: str) -> None:
    for e in _load().get(str(gid), []):
        if e.get("url") == url:
            e["desc"] = desc
            _save()
            return


async def generate_desc(image_payload: str) -> str:
    """让识图模型给图片写一句话描述。任何失败都返回空串。"""
    if not cfg.ai_vision_enabled or not cfg.ai_api_key:
        return ""
    if image_payload.startswith("base64://"):
        img_url = "data:image/jpeg;base64," + image_payload[len("base64://") :]
    elif image_payload.startswith("http"):
        img_url = image_payload
    else:
        return ""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "用不超过25个字概括这张图片的主要内容，只输出描述本身，不要任何前缀和标点开头",
                },
                {"type": "image_url", "image_url": {"url": img_url}},
            ],
        }
    ]
    from .ai_client import make_client

    client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=cfg.ai_model,
                messages=messages,
                max_tokens=60,
                extra_body={"thinking": {"type": "disabled"}} if cfg.ai_disable_thinking else None,
            ),
            timeout=_DESCRIBE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 相册图片描述生成失败（索引照建）: {exc!r}")
        return ""
    desc = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    return desc[:60]


async def background_index(
    gid: int, album_id: str, album_name: str, payload: str, by: str
) -> None:
    """存图后的后台任务：登记 + 生成描述。绝不抛异常影响主流程。"""
    url = payload if payload.startswith("http") else ""
    try:
        entry = record(gid, album_id, album_name, url, by, cfg.ai_album_index_max_per_group)
        if entry is None:
            return
        if not url:
            # 本地图转的 base64 没有稳定 URL，只记相册位置，不生成描述
            return
        desc = await generate_desc(payload)
        if desc:
            set_desc(gid, url, desc)
            logger.info(f"[ai_chat] 相册索引: {desc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 相册索引任务失败: {exc!r}")


# ================================================================ 检索


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", "", text.lower())
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def search(gid: int, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """按关键词找图：描述/相册名命中即返回，二字组打分排序，子串直接命中优先。"""
    kw = keyword.strip().lower()
    if not kw:
        return []
    items = _load().get(str(gid), [])
    scored: list[tuple[int, dict[str, Any]]] = []
    for e in items:
        hay = f"{e.get('desc') or ''} {e.get('album_name') or ''}".lower()
        if kw in hay:
            scored.append((1000, e))
            continue
        overlap = _bigrams(kw) & _bigrams(hay)
        if overlap:
            scored.append((len(overlap), e))
    scored.sort(key=lambda pair: -pair[0])
    return [e for _, e in scored[:limit]]


def describe(gid: int, text: str, limit: int = 3) -> str:
    """对话时注入：消息里在找图（图/照片/相册 + 内容词）才给资料段。"""
    t = text.strip()
    if not t:
        return ""
    low = t.lower()
    if not ("图" in low or "照片" in low or "相册" in low or "图片" in low):
        return ""
    # 去掉意图词后剩下的当作内容关键词；太短的词（如"图"本身）不查
    kw = (
        t.replace("相册", " ")
        .replace("照片", " ")
        .replace("图片", " ")
        .replace("找图", " ")
        .replace("那张", " ")
        .replace("哪张", " ")
        .replace("在哪", " ")
        .replace("那个", " ")
        .strip(" 的？?是了有没在")
    )
    if len(kw) < 2:
        return ""
    hits = search(gid, kw, limit)
    if not hits:
        return ""
    lines = ["【群相册索引】以下图片按内容描述匹配（如与问题相关可参考）："]
    for i, e in enumerate(hits, 1):
        desc = e.get("desc") or "（无内容描述）"
        lines.append(
            f"{i}. {desc}｜相册「{e.get('album_name') or '?'}」｜"
            f"{e.get('time', '')} 存｜{e.get('by', '')}"
        )
    lines.append("回答时可告诉提问者图片在哪个相册。")
    return "\n".join(lines)


# ================================================================ 指令


findimg = on_command("找图", rule=to_me(), priority=10, block=True)


@findimg.handle()
async def _handle_findimg(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    kw = args.extract_plain_text().strip()
    if not kw:
        await findimg.finish("用法：/找图 关键词 —— 比如 /找图 雪山，我按图片内容帮你找在哪个相册")
    hits = search(event.group_id, kw, limit=5)
    if not hits:
        await findimg.finish(f"相册索引里没有和「{kw}」相关的图（我只索引了用我存进相册的图）")
    lines = [f"找到 {len(hits)} 张相关图："]
    for i, e in enumerate(hits, 1):
        desc = e.get("desc") or "（无描述）"
        lines.append(f"{i}. {desc}｜相册「{e.get('album_name') or '?'}」｜{e.get('time', '')} 存")
    text = "\n".join(lines)
    # 尽力而为地补发第一张图；URL 过期就只报位置（FinishedException 必须放行，
    # 那是 nonebot 结束 handler 的正常机制，不能当错误吞掉）
    try:
        await bot.send(event, Message(text))
        first = hits[0].get("url") or ""
        if first:
            try:
                await bot.send(event, MessageSegment.image(first))
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[ai_chat] /找图 图片重发失败（URL 可能过期）: {exc!r}")
    except FinishedException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] /找图 发送失败: {exc!r}")
    await findimg.finish()
