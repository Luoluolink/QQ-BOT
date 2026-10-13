"""识图：把 QQ 消息里的图片抽出来，转成多模态模型能吃的格式。

为什么之前「发图片收不到」：
    原实现用的是 event.get_plaintext()，这个方法只取 text 消息段，
    图片段直接被丢掉；紧接着还有一句 `if not text: return`，
    所以纯图片消息连处理流程都进不去 —— 不是模型不支持，是代码没把图递过去。

图片怎么给模型：
    标准 OpenAI 多模态格式，content 从字符串变成数组：
        [{"type": "text", "text": "这是什么"},
         {"type": "image_url", "image_url": {"url": "https://..."}}]

url 还是 base64：
    优先用 url（省带宽、省时间）。QQ 图片 CDN 地址一般可以匿名访问，
    但 rkey 会过期，也可能被模型厂商的网络挡住，所以失败时自动退回 base64。
"""

from __future__ import annotations

import asyncio
import base64
import os
import urllib.request
from urllib.parse import urlparse

from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.log import logger

# 图片段类型。mface 是 QQ 商城表情，本质也是图，但常常拿不到 url，所以默认不算
IMAGE_TYPES = ("image",)

# base64 回退时的体积上限。超过就放弃，避免一个几百 MB 的图把请求撑爆
MAX_BASE64_BYTES = 8 * 1024 * 1024

# 下载图片的超时（秒）
DOWNLOAD_TIMEOUT = 20.0


# ---------------------------------------------------------------- 抓取


async def _get_image(bot: Bot, ref: str) -> dict:
    """调 NapCat 的 get_image，拿 url / base64 / 本地路径。

    file 和 file_id 是同一接口的两种入参形态，哪个能过用哪个。
    """
    if not ref:
        return {}
    for key in ("file", "file_id"):
        try:
            raw = await bot.call_api("get_image", **{key: ref})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_chat] get_image({key}) 失败: {exc!r}")
            continue
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        if isinstance(raw, dict) and raw:
            return raw
    return {}


async def collect_images(bot: Bot, event: MessageEvent, limit: int = 3) -> list[dict]:
    """从消息里抽出图片，最多 limit 张。

    返回 [{"url": ..., "ref": ..., "path": ...}]：
        url  给模型直接下载用
        ref  回头取 base64 用
        path 消息段里如果直接带了本地路径就先留着，省一次接口调用
    """
    found: list[dict] = []
    for seg in event.message:
        if len(found) >= limit:
            break
        if seg.type not in IMAGE_TYPES:
            continue

        data = seg.data if isinstance(seg.data, dict) else {}
        url = str(data.get("url") or "")
        ref = str(data.get("file") or data.get("file_id") or "")

        if not url and ref:
            info = await _get_image(bot, ref)
            url = str(info.get("url") or "")

        if not url:
            logger.info(f"[ai_chat] 有一张图片拿不到下载地址，跳过（file={ref[:48]!r}）")
            continue

        found.append({"url": url, "ref": ref, "path": str(data.get("path") or "")})

    return found


# ---------------------------------------------------------------- 转成模型输入


def _sniff_mime(raw: bytes) -> str:
    """按魔数猜图片类型。猜不出按 jpeg 处理（绝大多数 QQ 图片都是 jpg）。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/jpeg"


def _mime_from_name(name: str) -> str:
    ext = os.path.splitext(urlparse(name).path)[1].lower()
    return {
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def _encode(raw: bytes, hint: str = "") -> str:
    """把图片字节编码成 data URL。"""
    mime = _mime_from_name(hint) if hint else "image/jpeg"
    if mime == "image/jpeg":
        # 文件名里看不出具体格式时用魔数确认 —— QQ 缓存下来的图常常没有扩展名
        mime = _sniff_mime(raw)
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _read_local(path: str) -> bytes:
    """读本机图片文件。

    NapCat 和 NoneBot 一般跑在同一台机器上，get_image 给的本地路径可以直接读，
    比走一遍 HTTP 快得多，也不受网络影响。
    """
    if not path:
        return b""
    try:
        if not os.path.isfile(path):
            return b""
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_BASE64_BYTES:
            return b""
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        logger.debug(f"[ai_chat] 读取本地图片失败 {path!r}: {exc!r}")
        return b""


def _download_blocking(url: str) -> bytes:
    """下载图片。用标准库而不是第三方 HTTP 客户端，免得再多一个依赖。"""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as resp:
        # 多读 1 字节，用来判断是否超限
        return resp.read(MAX_BASE64_BYTES + 1)


async def as_data_url(bot: Bot, image: dict) -> str:
    """把一张图片转成 data:image/...;base64,... 形式。失败返回空串。

    按「代价从低到高」试三条路：
        1. 消息段里直接带了本地路径 —— 直接读文件，最快
        2. 问 get_image 要现成的 base64，或它给的本地路径
        3. 最后才下载 url
    """
    local = _read_local(image.get("path") or "")
    if local:
        return _encode(local, image.get("path") or "")

    ref = image.get("ref") or ""
    if ref:
        info = await _get_image(bot, ref)
        if isinstance(info, dict):
            raw_b64 = info.get("base64")
            if isinstance(raw_b64, str) and raw_b64:
                if raw_b64.startswith("data:"):
                    return raw_b64
                hint = str(info.get("file_name") or info.get("file") or ref)
                return _encode(base64.b64decode(raw_b64), hint)

            fetched = _read_local(str(info.get("file") or ""))
            if fetched:
                return _encode(fetched, str(info.get("file_name") or info.get("file") or ""))

    url = image.get("url") or ""
    if not url:
        return ""
    try:
        raw = await asyncio.to_thread(_download_blocking, url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 下载图片失败，无法回退 base64: {exc!r}")
        return ""

    if not raw:
        return ""
    if len(raw) > MAX_BASE64_BYTES:
        logger.warning(
            f"[ai_chat] 图片超过 {MAX_BASE64_BYTES / 1048576:.0f}MB 上限，放弃 base64 回退"
        )
        return ""
    return _encode(raw)


async def build_content(
    bot: Bot,
    text: str,
    images: list[dict],
    default_prompt: str = "看看这张图",
    mode: str = "url",
) -> str | list[dict]:
    """拼出 user 消息的 content。

    没有图片时直接返回字符串 —— 保持纯文本路径不变，省 token 也省得
    某些不支持多模态的模型因为收到数组而报错。
    """
    prompt = text.strip() or default_prompt
    if not images:
        return prompt

    parts: list[dict] = [{"type": "text", "text": prompt}]
    for image in images:
        url = image.get("url") or ""
        if mode == "base64":
            url = await as_data_url(bot, image) or url
        if not url:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})

    # 所有图片都没拼进去，就退化成纯文本，别给模型发一个只有 text 的数组
    if len(parts) == 1:
        return prompt
    return parts


def history_text(text: str, images: list[dict]) -> str:
    """存进上下文历史时用的文本。

    绝不能把带图片的 content 数组原样塞进历史 —— 那样每一轮都会把图片
    重新发一遍，token 和延迟都会爆炸。这里只留一个占位标记。
    """
    body = text.strip()
    marks = "[图片]" * len(images)
    if body and marks:
        return f"{body} {marks}"
    return body or marks
