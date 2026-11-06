"""QQ空间发帖：让机器人能把自己的心情/想法/作品发到自己的空间。

API：NapCat 扩展 set_qq_mood（发表说说）。带图时把图片落盘后以本地
路径传给 pics（NapCat 和机器人在同一台机器上，直接读文件）；
路径失败退 file:// URI，再失败退纯文本——说说宁可少图不能不发。

频控：每日上限（默认 3 条），防刷屏防风控。失败时把原因告诉调用方——
发空间是用户主动要求的动作，不是自发的模型输出，失败不该装死。
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot

from .config import Config

cfg = get_plugin_config(Config)

_USAGE_DAY = date.today().isoformat()
_USAGE = 0

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def _roll_day() -> None:
    global _USAGE_DAY, _USAGE
    today = date.today().isoformat()
    if today != _USAGE_DAY:
        _USAGE_DAY, _USAGE = today, 0


def quota_left() -> int:
    _roll_day()
    return max(0, cfg.ai_qzone_daily_limit - _USAGE)


def _take_quota() -> bool:
    global _USAGE
    if quota_left() <= 0:
        return False
    _USAGE += 1
    return True


async def compose_mood(bot: Bot, group_id: int = 0, hint: str = "") -> str:
    """让模型以人设写一条空间说说（此刻心情/想法）。失败返回空串。"""
    from .ai_client import chat_kwargs, make_client  # noqa: PLC0415
    from .tools import strip_speaker_prefix  # noqa: PLC0415

    now = datetime.now()
    ctx = ""
    if group_id:
        try:
            from . import digest as _digest  # noqa: PLC0415

            summary = _digest.current(group_id)
            if summary:
                ctx = f"\n群里最近的话题（仅供参考，别复述成流水账）：{summary[:150]}"
        except Exception:  # noqa: BLE001
            pass
    if hint:
        ctx += f"\n这条说说要围绕：{hint}"
    prompt = (
        f"现在是 {now.strftime('%Y-%m-%d %H:%M')}。你是吉普莉尔。"
        "写一条你要发到QQ空间的说说：此刻的心情或一个突然的想法，"
        "80字以内，符合你的人设口吻，自然真实像随手发的，"
        "不要话题标签、不要@任何人、不要提「系统」「AI」「模型」这类词。"
        f"{ctx}\n只输出说说正文，不要引号。"
    )
    try:
        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        resp = await client.chat.completions.create(
            **chat_kwargs(
                [{"role": "user", "content": prompt}],
                cfg,
                max_tokens=800,   # 思考型模型会吃预算
                temperature=0.9,
            ),
            timeout=45,
        )
        text = strip_speaker_prefix((resp.choices[0].message.content or "").strip())
        text = text.strip("“”\"『』「」").strip()
        return text[:300]
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 说说生成失败（静默）: {exc!r}")
        return ""


async def post_mood(bot: Bot, text: str, image: bytes | None = None) -> tuple[bool, str]:
    """发一条说说。带图先落盘，路径失败退 file:// 再退纯文本。

    返回 (成功?, 失败原因或补充说明)。
    """
    text = (text or "").strip()[:500]
    if not text and image is None:
        return False, "内容是空的"
    if not _take_quota():
        return False, "今天发空间的次数用完了，明天再来吧"

    tmp: Path | None = None
    attempts: list[dict] = []
    if image is not None:
        try:
            tmp = _DATA_DIR / f"qzone_{int(time.time())}.png"
            tmp.write_bytes(image)
            attempts.append({"content": text, "pics": [str(tmp)]})
            attempts.append({"content": text, "pics": [tmp.as_uri()]})
        except OSError as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 说说配图落盘失败，改发纯文本: {exc!r}")
    attempts.append({"content": text})

    last_err = ""
    for params in attempts:
        try:
            await bot.call_api("set_qq_mood", **params)
            logger.info(
                f"[ai_chat] 空间说说已发（带图={'pics' in params}）: {text[:40]}"
            )
            return True, "带图" if params.get("pics") else "纯文字"
        except Exception as exc:  # noqa: BLE001
            last_err = repr(exc)
            logger.warning(f"[ai_chat] set_qq_mood 尝试失败: {last_err[:160]}")
    if tmp is not None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return False, last_err[:120]
