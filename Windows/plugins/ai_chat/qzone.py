"""QQ空间发帖：让机器人能把自己的心情/想法/作品发到自己的空间。

API：NapCat 扩展 set_qq_mood（发表说说）。带图时把图片落盘后以本地
路径传给 pics（NapCat 和机器人在同一台机器上，直接读文件）；
路径失败退 file:// URI，再失败退纯文本——说说宁可少图不能不发。

频控：每日上限（默认 3 条），防刷屏防风控。失败时把原因告诉调用方——
发空间是用户主动要求的动作，不是自发的模型输出，失败不该装死。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from datetime import date, datetime
from pathlib import Path

from nonebot import get_bot, get_plugin_config, logger
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


async def handle_mood_request(bot: Bot, group_id: int = 0) -> str:
    """「发条说说」的完整流程（群聊/私聊共用）：构思 → 发帖 → 回复文案。"""
    if quota_left() <= 0:
        return "今天发空间的次数用完了，明天再来吧"
    text = await compose_mood(bot, group_id)
    if not text:
        return "心情还没酝酿好，等会儿再试一次？"
    ok, note = await post_mood(bot, text)
    if ok:
        return f"发到空间了：\n{text}"
    return f"空间发失败了（{note}），内容我先留着：\n{text}"


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
    """发一条说说：走 qzone-bridge 桥接服务的 send_msg（OneBot v11 兼容）。

    带图用 CQ 码 base64 内嵌（桥接支持图片上传到空间相册）。
    返回 (成功?, 失败原因或补充说明)。
    """
    text = (text or "").strip()[:500]
    if not text and image is None:
        return False, "内容是空的"
    if not _take_quota():
        return False, "今天发空间的次数用完了，明天再来吧"

    message = text
    if image is not None:
        message = f"{text}[CQ:image,file=base64://{base64.b64encode(image).decode()}]"

    import asyncio  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    def _post_bridge() -> dict:
        req = urllib.request.Request(
            f"{cfg.ai_qzone_bridge}/send_msg",
            data=_json.dumps({"message": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))

    try:
        body = await asyncio.to_thread(_post_bridge)
    except Exception as exc:  # noqa: BLE001
        return False, f"空间桥接连不上（{exc!r:.80}）"
    retcode = body.get("retcode")
    if body.get("status") == "ok" or retcode in (0, 1):
        logger.info(f"[ai_chat] 空间说说已发（bridge 带图={image is not None}）: {text[:40]}")
        return True, "带图" if image is not None else "纯文字"
    if retcode == 1401 or "未登录" in str(body.get("message", "")):
        return False, "空间桥接还没登录（需要机器人账号的空间 Cookie）"
    return False, str(body.get("message") or body)[:120]


async def comic_worth_sharing(bot: Bot, board: dict) -> bool:
    """让模型判断这张漫画值不值得发空间——「觉得好的」由模型把关。"""
    from .ai_client import chat_kwargs, make_client  # noqa: PLC0415

    lines = []
    for i, p in enumerate(board.get("panels", [])[:4], 1):
        if isinstance(p, dict):
            lines.append(f"第{i}格：{p.get('who', '')} {p.get('bubble', '')}")
    prompt = (
        "一张四格漫画的分镜如下。它要被发到QQ空间公开可见，"
        "你觉得它有趣到值得分享吗？\n"
        f"主题：{board.get('topic', '')}\n" + "\n".join(lines) +
        "\n只输出 yes 或 no。"
    )
    try:
        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        resp = await client.chat.completions.create(
            **chat_kwargs(
                [{"role": "user", "content": prompt}],
                cfg,
                max_tokens=300,
                temperature=0.1,
            ),
            timeout=30,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        return "yes" in answer
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 漫画分享判定失败（默认不发）: {exc!r}")
        return False


async def share_comic(bot: Bot, image: bytes, topic: str) -> tuple[bool, str]:
    """漫画自动分享：围绕主题生成配文后发帖。"""
    caption = await compose_mood(
        bot, 0, hint=f"刚画好了一张四格漫画，主题是「{topic}」，想分享一下"
    )
    caption = caption or f"刚出炉的四格漫画：「{topic}」"
    return await post_mood(bot, caption, image=image)


# ---------------------------------------------------------------- 自动发心情
#
# 每天在随机时段自动发一条心情说说——像真人，不是整点打卡。
# 状态持久化：重启后当天已发过就不再发；到点时机器不在就窗口内补发。

_AUTO_STATE_PATH = _DATA_DIR / "qzone_auto.json"
_auto_task: asyncio.Task | None = None
_auto_state: dict = {}


def _load_auto_state() -> None:
    global _auto_state
    try:
        raw = json.loads(_AUTO_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            _auto_state = raw
    except (OSError, ValueError):
        _auto_state = {}


def _save_auto_state() -> None:
    try:
        _AUTO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _AUTO_STATE_PATH.write_text(
            json.dumps(_auto_state, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def _pick_random_time() -> str:
    """今天剩余的白天时段（10:00-22:00）里挑一个随机时刻。"""
    now = datetime.now()
    if now.hour >= 22:
        return ""    # 今天窗口过了，明天再排
    h = random.randint(max(now.hour, 10), 21)
    m = random.randint(0, 59)
    if h == now.hour:
        m = max(m, now.minute + 2)
        if m >= 60:
            h, m = min(h + 1, 21), 0
    return f"{h:02d}:{m:02d}"


async def _auto_tick(bot: Bot) -> None:
    today = date.today().isoformat()
    if _auto_state.get("day") != today or "scheduled" not in _auto_state:
        _auto_state["day"] = today
        _auto_state["posted"] = False
        _auto_state["tries"] = 0
        _auto_state["scheduled"] = _pick_random_time()
        _save_auto_state()
        logger.info(
            f"[ai_chat] 今日空间心情排在 {_auto_state.get('scheduled') or '（窗口已过，明天）'}"
        )
    if _auto_state.get("posted") or not _auto_state.get("scheduled"):
        return
    if _auto_state.get("tries", 0) >= 2:
        return   # 今天试了两次都没成，放弃（接口坏了就别每分钟去撞）
    if not cfg.ai_qzone_auto_mood:
        return
    now = datetime.now()
    if now.strftime("%H:%M") < _auto_state["scheduled"]:
        return
    if now.hour >= 22 and now.minute > 30:
        _auto_state["scheduled"] = ""   # 窗口 closing，挪明天重排
        _save_auto_state()
        return
    _auto_state["tries"] = _auto_state.get("tries", 0) + 1
    _save_auto_state()
    text = await compose_mood(bot, 0)
    if not text:
        return   # 没写出来：下个 tick 再试（tries 已计）
    ok, note = await post_mood(bot, text)
    if ok:
        _auto_state["posted"] = True
        _save_auto_state()
    logger.info(f"[ai_chat] 每日空间心情自动发布: {ok} {note} {text[:40]}")


async def _auto_loop() -> None:
    while True:
        try:
            bot = get_bot()
            await _auto_tick(bot)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ai_chat] 空间自动心情异常（不影响聊天）: {exc!r}")
        await asyncio.sleep(60)


def start_auto_loop() -> None:
    """幂等启动（主循环第一条群消息时调，同 reminders.start_checker）。"""
    global _auto_task
    if _auto_task is None or _auto_task.done():
        _load_auto_state()
        _auto_task = asyncio.get_running_loop().create_task(_auto_loop())
        logger.info("[ai_chat] 空间自动心情循环已启动")
