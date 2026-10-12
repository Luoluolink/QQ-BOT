"""群互动功能：戳一戳、表情回应、群签到、群荣誉、AI 语音。

设计要点：
- 戳一戳被戳回来有冷却 + 每日上限（它是机器人「主动发动作」，容易被风控盯上）
- 表情回应直接支持 emoji 字符（👍❤️🔥），也兼容中文别名和数字 ID
- AI 语音走 QQ 群聊自带的「AI 热榜语音」通道（NapCat get_ai_characters /
  send_group_ai_record），音色列表按群缓存一小时，文本由吉普莉尔说
- 全部动作失败都只打日志 + 温和提示，绝不把群聊搞挂
"""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

from nonebot import get_plugin_config, logger, on_command, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, NoticeEvent
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config
from .tools import args_text, resolve_emoji_id, reply_message_id, target_user_id, truncate

__plugin_meta__ = PluginMetadata(
    name="群互动（戳一戳 / 表情回应 / 签到 / 荣誉 / AI语音）",
    description="戳一戳互动、给消息贴表情回应、群签到、查询群荣誉、让吉普莉尔用 QQ AI 语音说话。",
    usage=(
        "/戳 @某人 —— 戳一下（也可回复某人的消息后发 /戳）\n"
        "/回应 👍 —— 回复某条消息，给它贴表情回应（支持 emoji、中文别名、数字ID）\n"
        "/签到 —— 群签到打卡\n"
        "/荣誉 —— 查看本群龙王、聊天的火等荣誉榜\n"
        "/语音 文本 —— 让吉普莉尔用 QQ AI 语音说话\n"
        "/语音列表 —— 查看可用的 AI 音色"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

cfg = get_plugin_config(Config)

# 被戳回应的限流状态：{group_id: (上次回应时间, 当日计数, 当日日期)}
_poke_state: Dict[int, Tuple[float, int, str]] = {}
# AI 音色缓存：{group_id: (时间戳, [(id, 名称), ...])}
_voice_cache: Dict[int, Tuple[float, List[Tuple[str, str]]]] = {}


# ---------------------------------------------------------------- 戳一戳：主动戳

poke_cmd = on_command("戳", aliases={"戳一戳", "poke"}, priority=10, block=True)


@poke_cmd.handle()
async def handle_poke(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    if target is None:
        await poke_cmd.finish("用法：/戳 @某人，或回复某人的消息后发 /戳")
    if target == int(event.self_id):
        await poke_cmd.finish("戳我自己做什么呀，我可是会痛的……不过你硬要的话（戳回去）")
    try:
        await bot.call_api(
            "group_poke", group_id=event.group_id, user_id=target
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 发送戳一戳失败: {exc!r}")
        await poke_cmd.finish("戳失败了，对方可能屏蔽了戳一戳～")
    # 成功时不需要额外文字，动作本身就是消息


# ---------------------------------------------------------------- 戳一戳：被戳回应

poke_notice = on_notice(priority=60, block=False)


@poke_notice.handle()
async def handle_poked(bot: Bot, event: NoticeEvent) -> None:
    """有人戳机器人时回戳一下。

    为什么不接模型回复：被戳是群里最容易刷起来的互动，一群人轮着戳的话
    每次都调模型既烧钱又刷屏，回戳 + 偶尔说一句话已经足够有来有回。
    """
    data = event.model_dump()
    if data.get("notice_type") != "notify" or data.get("sub_type") != "poke":
        return
    if not cfg.ai_poke_enabled or not cfg.ai_poke_reply_enabled:
        return
    try:
        target = int(data.get("target_id") or 0)
        poker = int(data.get("user_id") or 0)
        group_id = int(data.get("group_id") or 0)
    except (TypeError, ValueError):
        return
    if target != int(event.self_id) or not group_id or not poker:
        return

    now = time.time()
    today = time.strftime("%Y-%m-%d")
    last_ts, count, day = _poke_state.get(group_id, (0.0, 0, today))
    if day != today:
        count = 0
    if now - last_ts < cfg.ai_poke_cooldown or count >= cfg.ai_poke_daily_limit:
        return

    _poke_state[group_id] = (now, count + 1, today)
    try:
        await bot.call_api("group_poke", group_id=group_id, user_id=poker)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 回戳失败: {exc!r}")


# ---------------------------------------------------------------- 表情回应

react_cmd = on_command("回应", aliases={"表情回应", "react"}, priority=10, block=True)


@react_cmd.handle()
async def handle_react(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target_msg_id = reply_message_id(event)
    emoji_arg = args_text(args)
    if target_msg_id is None or not emoji_arg:
        await react_cmd.finish(
            "用法：回复一条消息后发 /回应 👍\n"
            "支持 emoji（👍❤️🔥🎉…）、中文别名（赞/心/火/哭…）或数字 ID"
        )
    emoji_id = resolve_emoji_id(emoji_arg)
    if emoji_id is None:
        await react_cmd.finish(f"没认出「{truncate(emoji_arg, 20)}」是哪个表情，直接发 emoji 或数字 ID 试试")
    try:
        await bot.call_api(
            "set_msg_emoji_like",
            message_id=target_msg_id,
            emoji_id=emoji_id,
            set=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 表情回应失败: {exc!r}")
        await react_cmd.finish("贴表情失败了，这个表情可能不支持回应～")
    # 成功不说话，表情本身已经是回应


# ---------------------------------------------------------------- 群签到

sign_cmd = on_command("签到", aliases={"打卡", "sign"}, priority=10, block=True)


@sign_cmd.handle()
async def handle_sign(bot: Bot, event: GroupMessageEvent) -> None:
    try:
        await bot.call_api("send_group_sign", group_id=event.group_id)
        await sign_cmd.finish("打卡完成～今天也在知识的殿堂留下了足迹")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 群签到失败: {exc!r}")
        await sign_cmd.finish("签到失败了，可能今天已经签过或 QQ 服务端没响应")


# ---------------------------------------------------------------- 群荣誉

HONOR_TYPES: Dict[str, str] = {
    "龙王": "talkative",
    "聊天之火": "performer",
    "火焰": "performer",
    "炽焰": "legend",
    "传说": "legend",
    "冒尖": "strong_newbie",
    "新人": "strong_newbie",
    "快乐之源": "emotion",
    "快乐": "emotion",
    "全部": "all",
}
HONOR_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("current_talkative", "当前龙王"),
    ("talkative_list", "龙王榜"),
    ("performer_list", "聊天之火"),
    ("legend_list", "群聊炽焰"),
    ("strong_newbie_list", "冒尖之交"),
    ("emotion_list", "快乐之源"),
)

honor_cmd = on_command("荣誉", aliases={"群荣誉", "honor"}, priority=10, block=True)


@honor_cmd.handle()
async def handle_honor(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    arg = args_text(args)
    honor_type = HONOR_TYPES.get(arg, "all") if arg else "all"
    try:
        resp = await bot.call_api(
            "get_group_honor_info", group_id=event.group_id, type=honor_type
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 查询群荣誉失败: {exc!r}")
        await honor_cmd.finish("查荣誉榜失败了，QQ 服务端暂时没响应～")

    lines = _render_honor(resp or {})
    if not lines:
        await honor_cmd.finish("这个群还没有任何荣誉记录，多聊聊就会有的～")
    await honor_cmd.finish("\n".join(lines))


def _render_honor(resp: dict) -> List[str]:
    """防御式渲染荣誉榜：谁在榜上就列谁，字段缺失一律跳过。"""
    lines: List[str] = []
    for key, title in HONOR_SECTIONS:
        items = resp.get(key)
        if not items:
            continue
        if key == "current_talkative":
            nickname = items.get("nickname") or items.get("user_id") or "？"
            lines.append(f"当前龙王：{nickname}")
            continue
        if not isinstance(items, list):
            continue
        names = []
        for item in items[:5]:
            if isinstance(item, dict):
                name = item.get("nickname") or str(item.get("user_id") or "？")
                desc = (item.get("description") or "").strip()
                names.append(f"{name}（{desc}）" if desc else name)
        if names:
            lines.append(f"{title}：" + "、".join(names))
    return lines


# ---------------------------------------------------------------- AI 语音

voice_cmd = on_command("voice", aliases={"语音"}, priority=10, block=True)
voices_cmd = on_command("语音列表", aliases={"音色列表"}, priority=10, block=True)


async def _get_characters(bot: Bot, group_id: int) -> List[Tuple[str, str]]:
    """拉取本群可用的 AI 音色，缓存 1 小时。返回 [(id, 名称), ...]。"""
    cached = _voice_cache.get(group_id)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    resp = await bot.call_api("get_ai_characters", group_id=group_id)
    characters: List[Tuple[str, str]] = []
    for category in resp if isinstance(resp, list) else []:
        for ch in (category or {}).get("characters") or []:
            cid = str(ch.get("character_id") or "").strip()
            name = str(ch.get("character_name") or cid).strip()
            if cid:
                characters.append((cid, name))
    _voice_cache[group_id] = (time.time(), characters)
    return characters


@voices_cmd.handle()
async def handle_voices(bot: Bot, event: GroupMessageEvent) -> None:
    try:
        characters = await _get_characters(bot, event.group_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 拉取 AI 音色失败: {exc!r}")
        await voices_cmd.finish("拿不到 AI 音色列表，这个群可能没开放 AI 语音功能")
    if not characters:
        await voices_cmd.finish("这个群没有可用的 AI 音色")
    names = "、".join(name for _, name in characters[:20])
    await voices_cmd.finish(
        f"共 {len(characters)} 个音色：{names}\n用法：/语音 音色名 你要说的话"
    )


@voice_cmd.handle()
async def handle_voice(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    raw = args_text(args)
    if not raw:
        await voice_cmd.finish("用法：/语音 你要说的话（或 /语音 音色名 你要说的话）")

    character = cfg.ai_voice_character
    text = raw
    try:
        characters = await _get_characters(bot, event.group_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 拉取 AI 音色失败: {exc!r}")
        characters = []

    # 参数第一个空格前的部分若恰好是音色名/ID，就当作音色选择
    first, _, rest = raw.partition(" ")
    for cid, name in characters:
        if first and (first == name or first == cid):
            character, text = cid, rest.strip()
            break
    if not character and characters:
        character = characters[0][0]
    if not text:
        await voice_cmd.finish("说了半天不知道要说什么呢，用法：/语音 你要说的话")
    if not character:
        await voice_cmd.finish("这个群没有可用的 AI 音色，发不了语音～")

    try:
        await bot.call_api(
            "send_group_ai_record",
            group_id=event.group_id,
            character=character,
            text=text[:300],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 发送 AI 语音失败: {exc!r}")
        await voice_cmd.finish("AI 语音发送失败了（QQ 对这个功能有限额，可能今天用完了）")
