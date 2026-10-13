"""媒体识别：语音转文字 + 图片 OCR。

两个功能都借用 QQ 自己的服务端能力（走 NapCat 的 fetch_ptt_text / ocr_image），
不额外花大模型的钱：
- 语音转文字：直接回复一条语音发 /转文字，QQ 会返回识别文本
- 图片 OCR：回复一张图片发 /ocr，QQ 的 OCR 返回图里的文字

为什么不做「收到语音自动转文字」：语音没法 @ 机器人，自动转文字会
把群里所有人的语音都拉去识别，既浪费额度又容易触发风控，改成命令更稳妥。
"""

from __future__ import annotations

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .tools import args_text, reply_message_id, truncate

__plugin_meta__ = PluginMetadata(
    name="媒体识别（语音转文字 / OCR）",
    description="回复语音消息发 /转文字 识别语音；回复图片发 /ocr 提取图中文字。用 QQ 官方能力，免费。",
    usage=(
        "/转文字 —— 回复一条语音消息使用\n"
        "/ocr —— 回复一张图片使用（也可直接带图：/ocr [图片]）"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)


asr_cmd = on_command("转文字", aliases={"语音转文字", "听打"}, priority=10, block=True)


@asr_cmd.handle()
async def handle_asr(bot: Bot, event: GroupMessageEvent) -> None:
    target_msg_id = reply_message_id(event)
    if target_msg_id is None:
        await asr_cmd.finish(
            "用法：先回复（引用）一条语音消息，再发 /转文字\n"
            "（QQ 没法在语音上直接 @ 我，所以只能这样触发～）"
        )
    try:
        resp = await bot.call_api("fetch_ptt_text", message_id=target_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 语音转文字失败: {exc!r}")
        await asr_cmd.finish("语音转文字失败了，可能是这条语音太久远或服务端抽风，稍后再试试")
    text = ((resp or {}).get("text") or "").strip()
    if not text:
        await asr_cmd.finish("没能从这条语音里识别出文字（可能是静音或纯哼唱～）")
    await asr_cmd.finish(f"语音内容：{text}")


ocr_cmd = on_command("ocr", aliases={"识别", "取字"}, priority=10, block=True)


def _collect_image_files(event: GroupMessageEvent, args: Message) -> list[str]:
    """收集可用作 OCR 的图片：优先命令里现带的图，其次被引用消息里的图。

    每个图片段按优先级取 url → file → file_id，都是 NapCat 认的写法。
    """
    files: list[str] = []

    def _push(seg_data: dict) -> None:
        for key in ("url", "file", "file_id"):
            value = (seg_data or {}).get(key)
            if value:
                files.append(str(value))
                return

    for seg in event.get_message():
        if seg.type == "image":
            _push(seg.data)
    if not files:
        reply = getattr(event, "reply", None)
        if reply is not None:
            for seg in getattr(reply, "message", []) or []:
                if seg.get("type") == "image":
                    _push(seg.get("data"))
    return files


@ocr_cmd.handle()
async def handle_ocr(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    images = _collect_image_files(event, args)
    if not images:
        await ocr_cmd.finish(
            "用法：回复一张图片发 /ocr，或者直接发 /ocr 并附上图片"
        )

    all_texts: list[str] = []
    for image in images[:2]:  # 最多认两张，够用且不容易超时
        try:
            resp = await bot.call_api("ocr_image", image=image)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] OCR 失败: {exc!r}")
            continue
        texts = _parse_ocr_texts(resp)
        if texts:
            all_texts.append("\n".join(texts))

    if not all_texts:
        await ocr_cmd.finish("没能从图片里认出文字（可能没有文字，或图太糊了）")
    joined = "\n".join(all_texts)
    await ocr_cmd.finish(f"图里的文字：\n{truncate(joined, 1200)}")


def _parse_ocr_texts(resp: dict) -> list[str]:
    """防御式解析 OCR 返回。

    NapCat/onebot 标准返回 {"language": ..., "texts": [{"text": ...}]}；
    也有实现返回 word_array。都兜住，解析不出就返回空列表。
    """
    if not isinstance(resp, dict):
        return []
    for key in ("texts", "word_array", "words"):
        raw = resp.get(key)
        if not isinstance(raw, list):
            continue
        out: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                t = (item.get("text") or item.get("words") or "").strip()
            elif isinstance(item, str):
                t = item.strip()
            else:
                continue
            if t:
                out.append(t)
        if out:
            return out
    return []
