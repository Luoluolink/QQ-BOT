"""群管理功能（第三档）：踢人、禁言、撤回、设精华、头衔、名片、授予管理员。

重要前提：这些接口全部要求「机器人自己是群管理员」。用户目前**没有**给
机器人管理员身份，所以每个命令执行前都会先自查角色，没有权限时用
吉普莉尔的口吻说明情况 —— 命令先备好，之后群主一给身份就能直接用。

自查为什么必须做：不查的话 NapCat 会返回 ActionFailed 这种原始报错，
群里看到一堆堆栈非常难看；提前查角色还能缓存 5 分钟，少打一次接口。
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config
from .tools import args_text, reply_message_id, target_user_id

__plugin_meta__ = PluginMetadata(
    name="群管理（踢人 / 禁言 / 撤回 / 精华 / 头衔）",
    description="需要机器人拥有群管理员身份。当前未授予时命令会礼貌提示，不会报错刷屏。",
    usage=(
        "/踢 @某人 —— 将其移出群聊\n"
        "/禁言 @某人 10 —— 禁言 10 分钟（0.5 表示半分钟，最多 43200）\n"
        "/解禁 @某人 —— 解除禁言\n"
        "/全体禁言 开 或 /全体禁言 关\n"
        "/撤回 —— 回复某条消息将其撤回\n"
        "/设精华 —— 回复某条消息设为精华；/取消精华 取消\n"
        "/头衔 @某人 头衔文字 —— 设置专属头衔\n"
        "/名片 @某人 名片文字 —— 设置群名片\n"
        "/管理员 @某人 给 或 /管理员 @某人 收 —— 授予/收回管理员"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

cfg = get_plugin_config(Config)

# 机器人自身角色缓存：{group_id: (查询时间, 角色)}
_own_role_cache: Dict[int, Tuple[float, str]] = {}

ADMIN_NEEDED = (
    "这套命令需要先给我管理员身份哦。\n"
    "请群主在群设置 → 群管理 → 设置管理员里把我设为管理员，之后再来找我～"
)


async def _own_role(bot: Bot, group_id: int) -> str:
    """查机器人自己在本群的角色（member / admin / owner），缓存 5 分钟。"""
    cached = _own_role_cache.get(group_id)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    info = await bot.call_api(
        "get_group_member_info", group_id=group_id, user_id=int(bot.self_id)
    )
    role = (info or {}).get("role") or "member"
    _own_role_cache[group_id] = (time.time(), role)
    return role


async def _require_bot_admin(bot: Bot, group_id: int, matcher) -> Optional[str]:
    """机器人没管理员身份时返回提示文案；有权限返回 None。

    用 matcher.finish 而不是 return：调用处拿到提示直接结束事件。
    """
    try:
        role = await _own_role(bot, group_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 查询自身角色失败: {exc!r}")
        await matcher.finish("查不到我自己的群身份，稍后再试试～")
    if role not in ("admin", "owner"):
        await matcher.finish(ADMIN_NEEDED)
    return None


def _extract_arg_text(args: Message) -> str:
    """取命令参数文本（/头衔 @某人 新头衔 里的「新头衔」）。"""
    return args_text(args)


kick_cmd = on_command("踢", aliases={"踢出", "kick"}, priority=10, block=True)


@kick_cmd.handle()
async def handle_kick(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    if target is None:
        await kick_cmd.finish("用法：/踢 @某人")
    if target == int(event.self_id):
        await kick_cmd.finish("我不能踢我自己，这是原则问题")
    await _require_bot_admin(bot, event.group_id, kick_cmd)
    try:
        await bot.call_api(
            "set_group_kick", group_id=event.group_id, user_id=target, reject_add_request=False
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 踢人失败: {exc!r}")
        await kick_cmd.finish("踢人失败了，可能对方是管理员或群主")
    await kick_cmd.finish("已移出群聊")


ban_cmd = on_command("禁言", aliases={"闭嘴", "ban"}, priority=10, block=True)


@ban_cmd.handle()
async def handle_ban(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    if target is None:
        await ban_cmd.finish("用法：/禁言 @某人 分钟数（默认 10 分钟）")
    await _require_bot_admin(bot, event.group_id, ban_cmd)

    # 时长：取参数里第一个数字；@ 段不带文本所以从纯文本里提
    minutes = 10.0
    digits = ""
    for ch in args_text(args):
        if ch.isdigit() or ch == ".":
            digits += ch
        elif digits:
            break
    if digits:
        try:
            minutes = float(digits)
        except ValueError:
            pass
    seconds = int(max(0.5, min(minutes, 43200)) * 60)  # 上限 30 天
    try:
        await bot.call_api(
            "set_group_ban",
            group_id=event.group_id,
            user_id=target,
            duration=seconds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 禁言失败: {exc!r}")
        await ban_cmd.finish("禁言失败了，对方可能也是管理员")
    await ban_cmd.finish(f"已禁言 {minutes:g} 分钟")


unban_cmd = on_command("解禁", aliases={"unban"}, priority=10, block=True)


@unban_cmd.handle()
async def handle_unban(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    if target is None:
        await unban_cmd.finish("用法：/解禁 @某人")
    await _require_bot_admin(bot, event.group_id, unban_cmd)
    try:
        await bot.call_api(
            "set_group_ban", group_id=event.group_id, user_id=target, duration=0
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 解禁失败: {exc!r}")
        await unban_cmd.finish("解禁失败了，对方可能本来就没被禁言")
    await unban_cmd.finish("已解禁")


mute_all_cmd = on_command("全体禁言", priority=10, block=True)


@mute_all_cmd.handle()
async def handle_mute_all(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    await _require_bot_admin(bot, event.group_id, mute_all_cmd)
    text = args_text(args)
    enable = text not in ("关", "关闭", "off", "解除", "停")
    try:
        await bot.call_api(
            "set_group_whole_ban", group_id=event.group_id, enable=enable
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 全体禁言失败: {exc!r}")
        await mute_all_cmd.finish("操作失败了，稍后再试试～")
    await mute_all_cmd.finish("已开启全体禁言" if enable else "已解除全体禁言")


recall_cmd = on_command("撤回", aliases={"delete", "recall"}, priority=10, block=True)


@recall_cmd.handle()
async def handle_recall(bot: Bot, event: GroupMessageEvent) -> None:
    target_msg_id = reply_message_id(event)
    if target_msg_id is None:
        await recall_cmd.finish("用法：回复要撤回的那条消息，发 /撤回")
    await _require_bot_admin(bot, event.group_id, recall_cmd)
    try:
        await bot.call_api("delete_msg", message_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 撤回失败: {exc!r}")
        await recall_cmd.finish("撤回失败了，只能撤回我的或普通成员的消息")
    await recall_cmd.finish("已撤回")


essence_cmd = on_command("设精华", aliases={"加精华", "set_essence"}, priority=10, block=True)
unessence_cmd = on_command("取消精华", aliases={"删精华"}, priority=10, block=True)


@essence_cmd.handle()
async def handle_set_essence(bot: Bot, event: GroupMessageEvent) -> None:
    target_msg_id = reply_message_id(event)
    if target_msg_id is None:
        await essence_cmd.finish("用法：回复要加精的那条消息，发 /设精华")
    await _require_bot_admin(bot, event.group_id, essence_cmd)
    try:
        await bot.call_api("set_essence_msg", message_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 设精华失败: {exc!r}")
        await essence_cmd.finish("设精华失败了，可能已经是精华或服务端拒绝")
    await essence_cmd.finish("已设为精华消息")


@unessence_cmd.handle()
async def handle_unset_essence(bot: Bot, event: GroupMessageEvent) -> None:
    target_msg_id = reply_message_id(event)
    if target_msg_id is None:
        await unessence_cmd.finish("用法：回复要移出精华的那条消息，发 /取消精华")
    await _require_bot_admin(bot, event.group_id, unessence_cmd)
    try:
        await bot.call_api("delete_essence_msg", message_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 取消精华失败: {exc!r}")
        await unessence_cmd.finish("取消失败了，可能它本来就不是精华")
    await unessence_cmd.finish("已从精华中移出")


title_cmd = on_command("头衔", aliases={"专属头衔"}, priority=10, block=True)


@title_cmd.handle()
async def handle_title(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    title = _extract_arg_text(args).strip()
    if target is None:
        await title_cmd.finish("用法：/头衔 @某人 头衔文字（留空文字=清除头衔）")
    await _require_bot_admin(bot, event.group_id, title_cmd)
    try:
        await bot.call_api(
            "set_group_special_title",
            group_id=event.group_id,
            user_id=target,
            special_title=title,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 设置头衔失败: {exc!r}")
        await title_cmd.finish("设置头衔失败了，只有群主能授头衔哦")
    await title_cmd.finish(f"头衔已设为「{title}」" if title else "头衔已清除")


card_cmd = on_command("名片", aliases={"群名片"}, priority=10, block=True)


@card_cmd.handle()
async def handle_card(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    card = _extract_arg_text(args).strip()
    if target is None:
        await card_cmd.finish("用法：/名片 @某人 新名片（留空文字=清空名片）")
    await _require_bot_admin(bot, event.group_id, card_cmd)
    try:
        await bot.call_api(
            "set_group_card", group_id=event.group_id, user_id=target, card=card
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 设置名片失败: {exc!r}")
        await card_cmd.finish("设置名片失败了，稍后再试试～")
    await card_cmd.finish(f"群名片已设为「{card}」" if card else "群名片已清空")


admin_cmd = on_command("管理员", aliases={"设管理员"}, priority=10, block=True)


@admin_cmd.handle()
async def handle_admin(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    target = target_user_id(event, args, event.self_id)
    if target is None:
        await admin_cmd.finish("用法：/管理员 @某人 给 或 /管理员 @某人 收")
    await _require_bot_admin(bot, event.group_id, admin_cmd)
    text = args_text(args)
    enable = text.split()[-1] not in ("收", "取消", "收回", "remove") if text else True
    try:
        await bot.call_api(
            "set_group_admin",
            group_id=event.group_id,
            user_id=target,
            enable=enable,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 设置管理员失败: {exc!r}")
        await admin_cmd.finish("设置失败了，只有群主能授予管理员身份")
    await admin_cmd.finish("已授予管理员" if enable else "已收回管理员")
