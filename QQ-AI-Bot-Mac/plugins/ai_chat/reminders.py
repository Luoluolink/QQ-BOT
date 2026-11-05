"""提醒功能：自然语言设置定时提醒，到点在群里（或私聊）喊人。

设计：
- 存储 data/reminders.json：每条 {time, content, group_id, requester, created}
- 创建：意图路由把「提醒我明天8点开会」分类为 reminder_add，
  这里用一次小模型调用把自然语言时间解析成绝对时间——
  比正则可靠（「明天早上8点」「半小时后」「下周三下午」都能算对）
- 检查器：每 30 秒扫一遍到期提醒，用 get_bot 发送后删除
- 上限：每群同时 20 条，防刷屏

所有失败静默：提醒是辅助功能，不能影响聊天。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot import get_bot, get_plugin_config, logger

from .ai_client import chat_kwargs, make_client
from .config import Config

cfg = get_plugin_config(Config)

_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "reminders.json"
_check_task: asyncio.Task | None = None
_items: dict[str, list[dict[str, Any]]] | None = None


def _load() -> dict[str, list[dict[str, Any]]]:
    global _items
    if _items is None:
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            _items = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _items = {}
    return _items


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(_items or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def count(group_id: int) -> int:
    return len(_load().get(str(group_id), []))


def list_text(group_id: int) -> str:
    """列出本群当前的提醒（给 /提醒 或能力清单用）。"""
    items = _load().get(str(group_id), [])
    if not items:
        return ""
    lines = [f"- {it['time']} {it['content']}（{it['requester']}）" for it in items]
    return "\n".join(lines)


def add(group_id: int, when: str, content: str, requester: str) -> str:
    """添加提醒。when 是已解析的 YYYY-MM-DD HH:MM。返回结果说明。"""
    items = _load().setdefault(str(group_id), [])
    if len(items) >= 20:
        return "这个群的提醒已经攒了 20 条，先等几条到期或者让管理员清理一下"
    try:
        due = datetime.strptime(when, "%Y-%m-%d %H:%M")
    except ValueError:
        return "时间没解析对，换个明确一点的说法？例如：明天早上8点"
    if due.timestamp() <= datetime.now().timestamp():
        return "这个时间已经过去了……"
    content_clean = " ".join(content.split())[:100]
    items.append(
        {
            "time": when,
            "content": content_clean,
            "group_id": group_id,
            "requester": requester,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    items.sort(key=lambda e: e["time"])
    _save()
    return f"记下了，{when} 我会提醒你「{content_clean}」"


def _due_items() -> list[dict]:
    """取出所有到期的提醒并从存储删除。"""
    now = datetime.now()
    due: list[dict] = []
    for key, items in _load().items():
        keep = []
        for it in items:
            try:
                if datetime.strptime(it["time"], "%Y-%m-%d %H:%M") <= now:
                    due.append(it)
                    continue
            except (KeyError, ValueError):
                continue
            keep.append(it)
        _items[key] = keep
    if due:
        _save()
    return due


async def _parse_when(text: str, bot: Bot) -> str:
    """用模型把自然语言时间解析成 YYYY-MM-DD HH:MM。失败抛 ValueError。"""
    from datetime import datetime as dt

    now = dt.now()
    client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    resp = await client.chat.completions.create(
        **chat_kwargs(
            [
                {
                    "role": "user",
                    "content": (
                        f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（星期"
                        f"{'一二三四五六日'[now.weekday()]}）。"
                        "把下面这句话里的提醒时间换算成绝对时间，"
                        "只输出 YYYY-MM-DD HH:MM 格式的结果，不要任何其他文字。\n"
                        f"用户说：{text[:120]}"
                    ),
                }
            ],
            cfg,
            max_tokens=600,
            temperature=0.0,
        ),
        timeout=30,
    )
    m = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", resp.choices[0].message.content or "")
    if not m:
        raise ValueError("时间解析失败")
    return m.group(0)


async def create_reminder(
    bot: Bot, group_id: int, requester: str, request_text: str
) -> str:
    """自然语言建提醒：解析时间 → 存储。返回给用户的回复。"""
    if not cfg.ai_reminders_enabled:
        return "提醒功能现在关着呢"
    if not request_text.strip():
        return "提醒什么呀？给我个具体的事"
    try:
        when = await _parse_when(request_text, bot)
    except Exception:  # noqa: BLE001
        return "时间没解析出来，换个明确一点的说法？例如：明天早上8点提醒我开会"
    return add(group_id, when, request_text, requester)


_briefing_sent: dict[str, str] = {}   # 群号 → 已发晨报的日期


async def _morning_briefings() -> None:
    """每日晨报：有摘要记录的群在配置时间发一条「昨天聊了什么 + 问候」。

    群的来源是 digest 的存储键（有摘要说明群里真聊过天），
    时间来自 AI_BRIEFING_TIME（默认 08:30），每天只发一次。
    """
    if not cfg.ai_briefing_enabled:
        return
    from . import digest as digest_mod  # noqa: PLC0415

    today = datetime.now().strftime("%Y-%m-%d")
    hh, mm = (cfg.ai_briefing_time.split(":") + ["0"])[:2]
    target = f"{today} {int(hh):02d}:{int(mm):02d}"
    for key, summary in list(digest_mod._digest.items()):
        if not summary:
            continue
        if _briefing_sent.get(key) == today:
            continue
        try:
            due_dt = datetime.strptime(target, "%Y-%m-%d %H:%M")
        except ValueError:
            return
        if datetime.now() < due_dt:
            continue
        _briefing_sent[key] = today
        try:
            bot = get_bot()
        except Exception:  # noqa: BLE001
            return
        try:
            head = summary[:140]
            await bot.send_msg(
                message_type="group", group_id=int(key),
                message=f"早上好。昨晚群里聊到：{head}……具体的等大家醒了再聊。",
            )
            logger.info(f"[ai_chat] 晨报已发（群 {key}）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 晨报发送失败: {exc!r}")


async def _checker_loop() -> None:
    """每 30 秒扫一遍到期提醒并投递。进程生命周期内常驻。"""
    while True:
        try:
            await _morning_briefings()
            for item in _due_items():
                try:
                    bot = get_bot()
                except Exception:
                    await asyncio.sleep(5)
                    continue
                try:
                    await bot.send_msg(
                        message_type="group",
                        group_id=int(item["group_id"]),
                        message=f"[CQ:at,qq={item['requester']}] 提醒：{item['content']}",
                    )
                    logger.info(f"[ai_chat] 提醒已送达: {item['content'][:40]}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_chat] 提醒投递失败: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 提醒检查器异常: {exc!r}")
        await asyncio.sleep(30)


def start_checker() -> None:
    """启动检查器循环（在事件循环内的第一条群消息时调用，幂等）。

    为什么不在 on_startup 启动：nonebot 的启动钩子跑在工作线程里，
    那个线程没有事件循环，create_task 直接炸。主循环里的第一条
    群消息触发这里，之后常驻。"""
    global _check_task
    if _check_task is None or _check_task.done():
        _check_task = asyncio.get_running_loop().create_task(_checker_loop())
        logger.info("[ai_chat] 提醒检查器已启动")

