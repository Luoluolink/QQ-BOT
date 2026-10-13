"""合并转发：发送合并转发消息 + 解析别人发的合并转发。

- /转发 内容1||内容2||…：把几段话打包成一条合并转发消息（自定义节点）
- /解析：回复一条合并转发消息，把里面的楼层展开成纯文本

解析为什么要做成命令而不是自动做：收到合并转发就自动展开会
翻隐私（不是所有人都想让机器人读自己转发的聊天记录），交给用户触发更得体。
"""

from __future__ import annotations

from typing import List

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .tools import args_text, fmt_time, reply_message_id, truncate

__plugin_meta__ = PluginMetadata(
    name="合并转发（发送 / 解析）",
    description="把多段话打包成合并转发消息发送；也能展开别人发的合并转发。",
    usage=(
        "/转发 内容1||内容2||内容3 —— 打包成一条合并转发发送\n"
        "/解析 —— 回复一条合并转发消息，展开成纯文本"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)


send_fwd_cmd = on_command("转发", aliases={"合并转发"}, priority=10, block=True)


@send_fwd_cmd.handle()
async def handle_send_forward(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    raw = args_text(args)
    if not raw:
        await send_fwd_cmd.finish(
            "用法：/转发 内容1||内容2||内容3\n"
            "用 || 分隔的每一段会成为合并转发里的一层"
        )
    parts = [p.strip() for p in raw.split("||") if p.strip()]
    if not parts:
        await send_fwd_cmd.finish("没解析出任何内容，注意每段之间要用 || 隔开")

    self_uin = str(event.self_id)
    nodes = []
    for part in parts[:20]:  # 单条最多 20 层，太多容易被风控
        nodes.append(
            {
                "type": "node",
                "data": {
                    "user_id": self_uin,
                    "nickname": "吉普莉尔",
                    "content": [{"type": "text", "data": {"text": part}}],
                },
            }
        )
    try:
        await bot.call_api(
            "send_group_forward_msg", group_id=event.group_id, messages=nodes
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 发送合并转发失败: {exc!r}")
        await send_fwd_cmd.finish("合并转发发送失败了，稍后再试试～")


parse_fwd_cmd = on_command("解析", aliases={"展开", "展开转发"}, priority=10, block=True)


@parse_fwd_cmd.handle()
async def handle_parse_forward(
    bot: Bot, event: GroupMessageEvent
) -> None:
    target_msg_id = reply_message_id(event)
    if target_msg_id is None:
        await parse_fwd_cmd.finish("用法：回复一条合并转发消息，发 /解析 即可展开内容")
    try:
        resp = await bot.call_api("get_forward_msg", message_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 解析合并转发失败: {exc!r}")
        await parse_fwd_cmd.finish("展开失败了，这条消息可能不是合并转发，或已经过期")

    messages = (resp or {}).get("messages") or []
    if not messages:
        await parse_fwd_cmd.finish("这条合并转发里没有可展开的内容")

    lines: List[str] = []
    for m in messages[:15]:
        sender = (m.get("sender") or {})
        name = sender.get("card") or sender.get("nickname") or "未知"
        ts = m.get("time") or 0
        text = _render_nodes(m.get("message") or [], m.get("content"))
        if not text:
            continue
        lines.append(f"{fmt_time(ts)} {name}：{truncate(text, 200)}")

    total = len(messages)
    head = f"共 {total} 层：" if total > len(lines) else "内容如下："
    if not lines:
        await parse_fwd_cmd.finish("这条合并转发里没有可展示的文字内容（可能全是图片/视频）")
    tail = f"\n……还有 {total - len(lines)} 层没有展示" if total > len(lines) else ""
    await parse_fwd_cmd.finish(head + "\n" + "\n".join(lines) + tail)


def _render_nodes(message: list, content=None) -> str:
    """把转发楼层里的消息段拼成文本；支持新旧两种字段布局。"""
    segments: List[dict] = []
    if isinstance(message, list) and message:
        segments = message
    elif isinstance(content, list):
        segments = content

    parts: List[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            t = (data.get("text") or "").strip()
            if t:
                parts.append(t)
        elif seg_type == "at":
            parts.append(f"@{data.get('nickname') or data.get('qq') or ''}".strip())
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type in ("record", "video", "file"):
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()
