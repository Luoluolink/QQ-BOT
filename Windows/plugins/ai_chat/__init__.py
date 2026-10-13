"""AI 群聊助手：在群里 @ 机器人即可对话，支持检索群资源（群文件 + 群相册 + 群精华）与识图。

设计要点：
- 分群独立上下文，群与群之间互不串台
- 上下文按轮数自动截断，避免输入 token 无限膨胀
- 群资源检索走「本地索引 + 关键词匹配」，模型只负责组织语言，杜绝编造文件名
- 群精华消息同理：先抓下来本地匹配，再说清是哪位群友说的
- 人设只影响语气，事实准确性由系统提示词里的「铁律」兜底
- 图片走多模态 content 数组；只发图不打字也能识别
- 联网搜索是可选能力，账号没开通插件时自动降级为普通对话，不会让机器人挂掉
- 联网搜索带每日额度闸门（默认全局 30 次），按官方返回的真实计费次数扣减，
  额度用尽就自动停止联网，避免群聊这种高频场景把账单跑飞
- 群成员识别：@ 段在 get_plaintext() 里会整个丢掉，所以自己拼文本把它还原成
  「@小a」；正文里直接打的群名片/昵称也靠本地名单匹配出来
- 新成员入群欢迎用模型生成（有模板兜底），但限流更严：它是主动发消息，
  一次拉一群人时连发多条最容易触发风控
- 每群冷却 + 每群每日额度，既防刷屏也防账单失控
- 只在被 @ 时响应，减少被 QQ 风控盯上的概率
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from typing import DefaultDict, Deque, Dict

from nonebot import get_plugin_config, logger, on_command, on_message, on_notice
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me
from openai import AsyncOpenAI

from . import (
    admin,
    album,
    album_index,
    crawler,
    docsync,
    essence,
    filetext,
    forward,
    history,
    interact,
    linkarchive,
    media,
    members,
    memory,
    music,
    proactive,
    resource_index,
    stickers,
    tools,
    vision,
    websearch,
    welcome,
)
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="AI 群聊助手",
    description=(
        "在 QQ 群里 @ 机器人即可聊天，人设与模型都可在启动器的网页里配置；"
        "支持检索群文件、群相册与群精华消息（回答「群里有没有 XX」），"
        "支持识别群友发来的图片（需模型本身是多模态的），"
        "支持识别被 @ 或被提到的群成员，"
        "可选联网搜索获取实时信息（带每日额度限制），"
        "支持点歌分享（网易云音乐卡片），"
        "新成员入群时自动欢迎。"
    ),
    usage=(
        "@机器人 你的问题\n"
        "@机器人 + 图片 —— 识图（需模型支持多模态）\n"
        "@机器人 @某群友 问关于他的问题 —— 会知道说的是谁\n"
        "/files 关键词 —— 直接检索群资源，不走模型，秒回\n"
        "/files list —— 列出所有目录与相册\n"
        "/files refresh —— 重建索引（限群主与管理员）\n"
        "/doc 关键词 —— 在群文件的正文内容里搜索（机器人会读文件）\n"
        "/essence —— 列出本群精华消息\n"
        "/essence 关键词 —— 在精华消息里检索\n"
        "/essence refresh —— 重建精华索引（限群主与管理员）\n"
        "/成员 关键词 —— 按名字或 QQ 号查群成员\n"
        "/成员 refresh —— 重建成员名单（限群主与管理员）\n"
        "/search —— 查看今日联网额度用量\n"
        "/search reset —— 重置今日联网额度（限群主与管理员）\n"
        "@机器人 来首晴天 / 点歌 歌名 —— 分享音乐卡片到群里\n"
        "/history 关键词 —— 检索最近谁说过什么\n"
        "/history recent —— 看最近 10 条消息\n"
        "/转文字 —— 回复语音消息，识别语音内容（QQ 官方能力，免费）\n"
        "/ocr —— 回复图片，提取图中文字\n"
        "/戳 @某人 —— 戳一下（被戳会回戳）\n"
        "/回应 👍 —— 给被回复的消息加表情回应\n"
        "/签到 —— 领取今天的群签到\n"
        "/荣誉 —— 查看群荣誉榜（龙王等）\n"
        "/语音 文本 —— 用群 AI 音色把文本念出来\n"
        "/语音列表 —— 查看可用的 AI 音色\n"
        "/转发 内容1||内容2 —— 打包成合并转发消息\n"
        "/解析 —— 回复合并转发消息，展开楼层\n"
        "/欢迎 —— 查看入群欢迎的开关与用量\n"
        "/reset —— 清空本群上下文（限群主与管理员）\n"
        "私聊直接发消息也能聊（可在配置里关闭）"
    ),
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

cfg = get_plugin_config(Config)

# 每个群一个独立的历史队列
_history: DefaultDict[int, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=cfg.ai_history_rounds * 2)
)
_last_call: Dict[int, float] = {}
_usage: DefaultDict[int, int] = defaultdict(int)
_usage_day: str = date.today().isoformat()

# ---------- 人设覆盖（管理员 /人设 指令的存储层）----------
# 存 data/persona.json：{"groups": {"群号": "自定义人设文本"}}
_PERSONA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "persona.json"
)


def _persona_override(group_id: int) -> str:
    """读本群的人设覆盖；没设置过返回空串。"""
    try:
        data = json.loads(_PERSONA_PATH.read_text(encoding="utf-8"))
        return ((data.get("groups") or {}).get(str(group_id)) or "").strip()
    except (OSError, ValueError):
        return ""


def _persona_source(group_id: int) -> tuple[str, int]:
    """人设来源描述（给 /人设 看的）：(来源, 自定义文本长度)。"""
    override = _persona_override(group_id)
    return ("本群自定义" if override else "默认（.env / 网页配置）"), len(override)


def _set_persona(group_id: int, text: str) -> None:
    """写入/清除本群人设覆盖；text 为空串表示清除。"""
    try:
        data = json.loads(_PERSONA_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    groups = data.setdefault("groups", {})
    if text:
        groups[str(group_id)] = text
    else:
        groups.pop(str(group_id), None)
    _PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PERSONA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _effective_system_prompt(group_id: int) -> str:
    """本群实际生效的系统提示词。

    没有覆盖时直接用 _system_prompt()；有覆盖时替换「人设段」——
    日期和机器人名字保留，IRON_RULES 由代码重新无条件附加。
    群助手功能大于角色扮演：换什么人设都不能破坏防幻觉底线。
    """
    override = _persona_override(group_id)
    if not override:
        return _system_prompt()
    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    parts = [
        f"今天的日期是 {today.isoformat()}（星期{weekday}）。涉及时间的问题一律以此为准。"
    ]
    name = (cfg.ai_bot_name or "").strip()
    if name:
        parts.append(f"你的名字是「{name}」。")
    parts.append(override)
    parts.append(IRON_RULES)
    return "\n\n".join(parts)


def _is_bot_admin(bot: Bot, event: GroupMessageEvent) -> bool:
    """群主 / 群管理员 / 超级用户（.env 里 SUPERUSERS）都有机器人控制权。"""
    if event.sender.role in ("owner", "admin"):
        return True
    try:
        return str(event.user_id) in {str(s) for s in bot.config.superusers}
    except Exception:  # noqa: BLE001
        return False

# 出现这些词就认为用户在问群资源的事
RESOURCE_KEYWORDS = (
    "文件", "文档", "资料", "附件", "表格", "文件夹", "群文件", "目录",
    "图片", "照片", "相片", "截图", "相册", "图集", "壁纸", "头像",
    "视频", "录像", "影片", "短片", "录屏", "录播",
    "音频", "录音", "语音",
    "压缩包", "课件", "ppt", "讲义",
    "有没有", "有没有人", "找一下", "找找", "搜一下", "搜搜",
    "在哪", "在哪个", "哪个文件夹", "共享",
)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """懒加载 OpenAI 客户端，避免没配 Key 时启动就崩。"""
    global _client
    if _client is None:
        if not cfg.ai_api_key:
            raise RuntimeError("未配置 AI_API_KEY，请检查 .env 文件")
        _client = AsyncOpenAI(api_key=cfg.ai_api_key, base_url=cfg.ai_api_base)
    return _client


def _roll_day_if_needed() -> None:
    """跨天时清零用量统计（群聊和私聊一起清）。"""
    global _usage_day
    today = date.today().isoformat()
    if today != _usage_day:
        _usage.clear()
        _private_usage.clear()
        _usage_day = today


# ---------------------------------------------------------------- 铁律
#
# 这段**不属于人设**，而是代码强制的约束：无论人设怎么写、从哪来
# （启动器网页 / .env / config.py），它都会被拼在 system 提示词的最后。
#
# 为什么要这样拆：人设是用户随便改的东西，而这几条是功能可靠性的底线。
# 早先把它们写在默认人设正文里，结果只要用户在网页上填了自己的性格，
# 整段约束就被一起覆盖掉了 —— 机器人开始在群里报不存在的文件名。
# 放在代码里就没有这个缺口了。
IRON_RULES = (
    "【铁律 —— 优先级高于上面所有性格设定】\n"
    "1. 当系统给出「群资源检索结果」时，你必须严格依据那份列表回答，明确告诉对方"
    "东西在哪个文件夹或哪个相册。列表里没有的，一律回答没有。"
    "绝对禁止编造不存在的文件名、相册名或链接。\n"
    "2. 当系统给出「群精华消息检索结果」时，同样只能依据那份列表回答，"
    "并说清是哪位群友说的。列表里没有的就说没有，不要凭印象补充。\n"
    "3. 当对方发来图片时，只描述你真正看到的内容。看不清、看不出就说看不出来，"
    "不要猜测图里有什么，也不要假装看到了文字或细节。\n"
    "4. 当系统给出「群成员识别结果」时，说明对方说的就是群里那些人。"
    "涉及他们的群身份、昵称、头衔时只能依据那份资料，不要编造。"
    "至于他们说过什么、做过什么、和谁关系好，资料里没有的就说不知道。\n"
    "5. 人设只影响语气，不影响事实。哪怕语气再随意，说出来的文件名、位置、"
    "谁说过什么，都必须准确。\n"
    "6. 不使用 Markdown 语法，不输出列表符号、标题或代码块，因为 QQ 不渲染这些格式。\n"
    "7. 遇到违法、有害或明显违规的请求，直接拒绝，不要用人设去包装。\n"
    "8. 发言人归档：每条用户消息开头的【】里是这条消息发言人（群成员）的名字，"
    "历史记录里的用户消息也带同样的标记。每个人的话只能记在 TA 自己名下——"
    "当有人问「我喜欢什么」「我说过什么」「我之前提过吗」这类问题，"
    "只能翻历史里【这个人自己】名下的发言来回答；TA 没说过的就直说不知道，"
    "绝不能把别人说过的话当成 TA 说的，也不能把 TA 说过的话安到别人头上。"
    "对方提到「他/她」时，优先结合最近几条发言的发言人判断指的是谁，拿不准就问。"
    "注意：【】标记只是给你看的备注格式，不是对话格式——"
    "你自己的回复里绝对不要出现【】开头的名字前缀（包括你自己的名字），直接说话就行。\n"
    "9. 当系统给出「群文件内容检索结果」时，那是从群文件正文里摘出来的原文，"
    "只能依据摘录回答并说清出自哪个文件；摘录里没有的信息就直说文件里没写到，"
    "绝不能脑补文件内容，也不能把别的文件的内容安到这个文件头上。"
)


def _system_prompt() -> str:
    """拼出系统提示词：日期 + 机器人名字 + 人设 + 铁律。

    日期是必须喂的 —— 不喂的话模型不知道「今天」是哪天，问天气会算错日期，
    联网时尤其明显。

    名字单独拼进来（而不是写死在 config.py 的人设正文里），是为了让启动器网页里
    填的「机器人名字」能生效，同时不干扰用户自己改人设。

    铁律固定拼在最后，且由本函数无条件附加 —— 见上面 IRON_RULES 的说明。
    """
    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    parts = [
        f"今天的日期是 {today.isoformat()}（星期{weekday}）。涉及时间的问题一律以此为准。"
    ]
    name = (cfg.ai_bot_name or "").strip()
    if name:
        parts.append(f"你的名字是「{name}」。")
    persona = (cfg.ai_system_prompt or "").strip()
    if persona:
        parts.append(persona)
    parts.append(IRON_RULES)
    return "\n\n".join(parts)


def _looks_like_resource_query(text: str) -> bool:
    return any(word in text for word in RESOURCE_KEYWORDS)


async def _build_member_context(
    bot: Bot, group_id: int, text: str, at_qqs: list[int], self_id: int
) -> str:
    """识别这条消息在说哪些群成员，拼成给模型看的资料。没有就返回空串。

    两种命中来源：消息里 @ 到的（at_qqs），以及正文里直接打了名字的。
    """
    if not cfg.ai_member_enabled:
        return ""

    index = await members.get_index(bot, group_id, ttl=cfg.ai_member_index_ttl)
    if index.error:
        # 名单拿不到就给不了资料。@ 到的名字已经由 render_text 还原进正文了，
        # 所以不会彻底失去线索，只是少了群身份/头衔这些细节。
        logger.warning(f"[ai_chat] 群成员名单不可用: {index.error}")
        return ""
    if not index.members:
        return ""

    hits = members.find_mentioned(
        index,
        text,
        extra_qqs=at_qqs,
        exclude_qqs=[self_id],
        limit=cfg.ai_member_max_results,
    )
    if not hits:
        return ""
    return members.describe(hits)


async def _build_filetext_context(bot: Bot, group_id: int, text: str) -> str:
    """群文件内容检索（RAG）：把文件正文里命中的段落摘出来给模型看。

    任何一步失败都返回空串，绝不让它拖垮资源检索主流程。
    """
    if not cfg.ai_filetext_enabled:
        return ""

    index = await resource_index.get_index(bot, group_id, ttl=cfg.ai_file_index_ttl)
    if index.error or not index.entries:
        return ""

    candidates = [
        e
        for e in index.entries
        if e.source == "群文件"
        and e.file_id
        and filetext.is_readable(e.name, e.size, cfg.ai_filetext_max_file_size)
    ]
    if not candidates:
        return ""

    cidx = await filetext.get_index(
        bot,
        group_id,
        candidates,
        max_files=cfg.ai_filetext_max_files,
        max_file_size=cfg.ai_filetext_max_file_size,
        chunk_size=cfg.ai_filetext_chunk_size,
        timeout=cfg.ai_filetext_timeout,
        ttl=cfg.ai_filetext_ttl,
    )
    hits = filetext.search(cidx, text, limit=cfg.ai_filetext_max_results)
    if not hits:
        return ""
    note = ""
    if cidx.building:
        note = "\n（注意：文件内容索引正在后台构建，以上可能不是全部内容）"
    return (
        "【群文件内容检索结果】以下原文摘自群文件的正文内容，你必须严格依据它们回答：\n"
        + filetext.describe(hits)
        + "\n回答要求：只引用上面摘录里真实出现的内容，并说清出自哪个文件。"
        "摘录里没有的就直说文件里没写到，绝不编造。"
        + note
    )


async def _build_resource_context(bot: Bot, group_id: int, text: str) -> str:
    """检索群资源，拼成一段给模型看的参考资料。没有相关内容就返回空串。"""
    if not cfg.ai_file_search_enabled:
        return ""

    index = await resource_index.get_index(
        bot, group_id, ttl=cfg.ai_file_index_ttl
    )
    if index.error:
        logger.warning(f"[ai_chat] 群资源索引不可用: {index.error}")
        return ""
    if not index.entries:
        return ""

    hits = resource_index.search(index, text, limit=cfg.ai_file_max_results)
    parts: list[str] = []

    if hits:
        parts.append("【群资源检索结果】以下是系统从群文件与群相册里实际检索到的内容，你必须严格依据它回答：")
        parts.append(resource_index.describe(hits))
        parts.append(
            f"该群共有群文件 {index.count_of('群文件')} 个、群相册资源 {index.count_of('群相册')} 个，"
            "涉及的目录与相册有：" + "、".join(index.locations[:20])
        )
        parts.append(
            "回答要求：只使用上面列出的资源，绝不编造不存在的文件名、相册名或链接。"
            "要明确告诉用户东西在哪个文件夹或哪个相册。"
            "用自然口语转述，不要照抄「类型」「位置」这类字段名，也不要输出方括号标记。"
        )
    elif not _looks_like_resource_query(text):
        # 文件名没命中、也不像资源类问题 → 没什么可给的
        return ""

    # 内容级检索（RAG）：文件名没命中但正文里有答案的问题靠它兜住
    try:
        block = await _build_filetext_context(bot, group_id, text)
        if block:
            parts.append(block)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 群文件内容检索失败: {exc!r}")

    if not parts:
        # 关键：没有匹配时，绝对不要把目录/相册清单塞给模型。
        # 实测它会顺着清单「推理」出一个不存在的文件名 —— 问量子力学课件时
        # 凭空多报了一个「考勤表」。宁可少给信息，也不能给幻觉留把柄。
        return (
            "【群资源检索结果】系统已经检索过该群的群文件与群相册（含文件正文内容），"
            "没有找到任何与你问的东西相关的资源。\n"
            "回答要求：只回答没有，不要列举群里还有什么别的东西，"
            "不要猜测，更不要编造任何文件名或相册名。"
        )
    return "\n".join(parts)


async def _build_essence_context(bot: Bot, group_id: int, text: str) -> str:
    """检索群精华消息，拼成给模型看的参考资料。没有相关内容就返回空串。

    和群资源一样的原则：没命中就明说没有，绝不把整份清单丢给模型让它自己挑，
    否则它会挑出一个不相关的精华内容硬凑答案。
    """
    if not cfg.ai_essence_enabled:
        return ""

    index = await essence.get_index(
        bot, group_id, ttl=cfg.ai_essence_index_ttl
    )
    if index.error:
        logger.warning(f"[ai_chat] 群精华索引不可用: {index.error}")
        return ""

    # 接口通、但群里一条精华都没有
    if not index.messages:
        if not essence.looks_like_essence_query(text):
            return ""
        return (
            "【群精华消息检索结果】该群目前没有任何精华消息。\n"
            "回答要求：直接告诉对方这个群还没设过精华消息。"
        )

    hits = essence.search(index, text, limit=cfg.ai_essence_max_results)
    if not hits:
        if not essence.looks_like_essence_query(text):
            return ""
        return (
            f"【群精华消息检索结果】系统已经检索过该群的 {len(index)} 条精华消息，"
            "没有找到与对方问的内容相关的。\n"
            "回答要求：只回答没有，不要列举群里还有什么别的精华，"
            "不要猜测，更不要编造谁说过什么。"
        )

    return "\n".join(
        [
            "【群精华消息检索结果】以下是系统从本群精华消息里实际检索到的内容，你必须严格依据它回答：",
            essence.describe(hits),
            "回答要求：只使用上面列出的内容，说清是哪位群友说的。"
            "绝不编造上面没有出现的发言、人名或时间。"
            "用自然口语转述，不要输出方括号标记。",
        ]
    )


async def _build_crawler_context(bot: Bot, group_id: int, text: str) -> str:
    """网页爬虫：用户丢链接就抓正文总结；「去b站/github找xx」就先搜再给链接。

    挂进 _build_context 的检索循环，和其他资料段一样失败自动降级。
    """
    if not cfg.ai_crawler_enabled:
        return ""
    return await crawler.build_context(
        text,
        max_results=cfg.ai_crawler_max_results,
        page_chars=cfg.ai_crawler_page_max_chars,
        timeout=cfg.ai_crawler_timeout,
        youtube_proxy=cfg.ai_crawler_youtube_proxy,
    )


async def _build_albumidx_context(bot: Bot, group_id: int, text: str) -> str:
    """相册内容索引：用户在找某张图时给出相册位置。纯 JSON 查询，开销可忽略。"""
    if not cfg.ai_album_index_enabled:
        return ""
    return album_index.describe(group_id, text)


async def _build_linkarchive_context(bot: Bot, group_id: int, text: str) -> str:
    """链接收藏库：用户在翻以前发过的链接时给出相关收藏。纯 JSON 查询。"""
    if not cfg.ai_linkarchive_enabled:
        return ""
    return linkarchive.describe(group_id, text)


async def _build_context(
    bot: Bot, group_id: int, text: str, at_qqs: list[int], self_id: int
) -> str:
    """汇总所有检索类参考资料。任何一段取不到都不影响其他段。"""
    # 纯图片消息没有可检索的文字，别白白去拉一遍索引
    if not text.strip():
        return ""

    blocks: list[str] = []

    # 群成员识别放最前面：它不依赖关键词，而且能把「@小a」落到具体的人，
    # 后面几段用到的正文也因此更容易被模型理解。
    try:
        block = await _build_member_context(bot, group_id, text, at_qqs, self_id)
        if block:
            blocks.append(block)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 群成员识别失败: {exc!r}")

    for builder in (
        _build_resource_context,
        _build_essence_context,
        _build_crawler_context,
        _build_albumidx_context,
        _build_linkarchive_context,
    ):
        try:
            block = await builder(bot, group_id, text)
        except Exception as exc:  # noqa: BLE001
            # 检索失败不该让整条对话挂掉，降级成普通聊天
            logger.warning(f"[ai_chat] {builder.__name__} 失败: {exc!r}")
            continue
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _web_search_unavailable(exc: Exception) -> bool:
    """判断异常是不是「这个模型不接受联网工具」导致的。

    三种情况都要能识别出来，否则机器人会直接报「模型那边好像出问题了」：

    1. MiMo 没开通联网服务插件 → 400，文案带 webSearchEnabled is false
    2. 换成本地模型（Ollama / llama.cpp / vLLM）→ 它们不认识 web_search
       这种厂商私有的服务端工具类型，各家报错文案完全不一样
    3. 换成别的云端模型 → 同理

    所以除了厂商专属文案，还兜了一层「错误信息里提到 tool 就算」。
    代价是偶尔会多做一次摘掉 tools 的重试（只发生在失败路径上）；
    换来的是换模型时不会莫名其妙挂掉。摘掉 tools 后还失败，那才是真失败。
    """
    text = str(exc)
    if "webSearchEnabled" in text or "web search tool" in text:
        return True
    low = text.lower()
    return any(
        marker in low
        for marker in (
            "tool_choice",
            "tool choice",
            "tools",
            "tool ",
            "invalid tool",
            "unknown tool",
            "unsupported tool",
            "function calling",
            "functions",
        )
    )


# 记录「今天已经提醒过额度用尽」，避免群里每条消息都刷一条日志
_search_budget_warned_day: str = ""


def _warn_search_budget_exhausted() -> None:
    global _search_budget_warned_day
    today = date.today().isoformat()
    if _search_budget_warned_day == today:
        return
    _search_budget_warned_day = today
    logger.warning(
        f"[ai_chat] 今日联网额度已用尽（{cfg.ai_web_search_daily_limit} 次），"
        "后续对话不再联网，仅基于模型自身知识回答。次日零点自动恢复；"
        "想调整就改 .env 里的 AI_WEB_SEARCH_DAILY_LIMIT。"
    )


async def _create_completion(messages: list[dict], allow_search: bool = False):
    """统一的模型调用入口，负责透传思考控制与联网搜索。

    allow_search 由调用方决定（关键词命中，或配置成 always）。
    为什么要由调用方传而不是这里自己判断：挂上联网参数会让服务端多做一轮
    「要不要联网」的判断，每条消息因此慢约 1.2 秒，所以不该无脑挂。
    """
    kwargs: dict = {
        "model": cfg.ai_model,
        "messages": messages,
        "temperature": cfg.ai_temperature,
        "max_tokens": cfg.ai_max_tokens,
    }

    # 思考模式控制：通过 extra_body 透传，兼容各家 OpenAI 兼容接口
    extra_body: dict = {}
    if cfg.ai_disable_thinking:
        # 关掉思考。实测 GLM-4.5-air 与 MiMo v2.6-flash 都认这个字段，
        # 关掉后群聊延迟从 4-9s 降到 1-2s，且不占用 max_tokens。
        extra_body["thinking"] = {"type": "disabled"}
    if cfg.ai_reasoning_effort:
        # 仅对「无法关闭思考、只能调档」的模型有意义（如 GLM-5.3 系列）
        extra_body["reasoning_effort"] = cfg.ai_reasoning_effort
    if extra_body:
        kwargs["extra_body"] = extra_body

    # 额度闸门：用尽后不再挂 tools。不挂 tools 就不会产生联网费用，
    # 对话本身照常进行，只是回答基于模型已有知识。
    search_attached = False
    if cfg.ai_web_search_enabled and allow_search:
        if websearch.exhausted(cfg.ai_web_search_daily_limit):
            _warn_search_budget_exhausted()
        else:
            kwargs["tools"] = [
                {
                    "type": "web_search",
                    "max_keyword": cfg.ai_web_search_max_keyword,
                    "force_search": cfg.ai_web_search_force,
                    "limit": 1,
                }
            ]
            kwargs["tool_choice"] = "auto"
            search_attached = True

    try:
        resp = await get_client().chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if not (search_attached and _web_search_unavailable(exc)):
            raise
        logger.warning(
            "[ai_chat] 联网搜索不可用，已自动降级为普通对话（本次不扣额度）。"
            "常见原因：① 账号还没开通「联网服务插件」——"
            "去 MiMo 控制台 → 插件管理 开通后即可生效；"
            "② 换成了本地模型（Ollama / llama.cpp），它不认识厂商私有的 "
            "web_search 工具——这种情况把 .env 里的 AI_WEB_SEARCH_ENABLED "
            "改成 false 就不会再报这个告警了。"
        )
        kwargs.pop("tools", None)
        kwargs.pop("tool_choice", None)
        resp = await get_client().chat.completions.create(**kwargs)
        search_attached = False

    # 扣额度。计数用的是官方返回的真实计费次数（web_search_usage.tool_usage），
    # 模型判断这个问题不需要联网时是 0 —— 这时既不扣额度也不打日志，免得刷屏。
    if search_attached:
        cost = websearch.record(resp)
        if cost:
            logger.info(
                f"[ai_chat] 本轮联网 {cost} 次，今日累计 "
                f"{websearch.used()}/{cfg.ai_web_search_daily_limit}"
            )
    return resp


async def _complete_with_images(
    bot: Bot,
    messages: list[dict],
    text: str,
    images: list[dict],
    allow_search: bool = False,
):
    """带图调用模型；用 url 传图失败时自动退回 base64 再试一次。

    为什么要这层兜底：QQ 图片的 CDN 地址带 rkey，会过期；
    也有些模型厂商的网络拉不到腾讯的图床。这时整条请求会直接失败，
    而 base64 是自己把图读进来发过去的，不受对方网络影响。
    """
    try:
        return await _create_completion(messages, allow_search=allow_search)
    except Exception as exc:  # noqa: BLE001
        if not images or cfg.ai_vision_image_mode != "url":
            raise
        logger.warning(f"[ai_chat] 用图片 URL 调用失败，改用 base64 重试一次: {exc!r}")

        content = await vision.build_content(
            bot, text, images, cfg.ai_vision_default_prompt, mode="base64"
        )
        if not isinstance(content, list):
            # 一张图都没转成 base64。这时绝不能拿纯文本去重试 ——
            # 模型看不到图却仍会回答「图里是……」，等于凭空编造。
            logger.warning("[ai_chat] base64 回退也没取到图，放弃重试")
            raise

        retry = messages[:-1] + [{"role": "user", "content": content}]
        try:
            return await _create_completion(retry, allow_search=allow_search)
        except Exception as retry_exc:  # noqa: BLE001
            logger.warning(
                f"[ai_chat] base64 回退也失败了: {retry_exc!r}（原始错误：{exc!r}）"
            )
            raise retry_exc


# ---------------------------------------------------------------- 风控拦截
# 有些接口被内容风控命中时不报异常，而是正常返回、正文直接是一句拒绝
# 文案（实测：「The request was rejected because it was considered high
# risk」）。原样发到群里既是英文报错、又会让「搜索结果出不来」，这里
# 识别出来后按「原样重试 → 去掉检索资料重试」的顺序自救。
_RISK_REJECT_PAT = re.compile(
    r"high\s*risk"
    r"|was\s+rejected"
    r"|content\s+(?:policy|filter|moderation)"
    r"|(?:内容)?风控"
    r"|敏感内容"
    r"|违规内容",
    re.IGNORECASE,
)


def _looks_rejected(reply: str) -> bool:
    """识别「风控拒绝文案」。限长是为了不误杀正常引用这些词的长回复。"""
    return 0 < len(reply) < 120 and bool(_RISK_REJECT_PAT.search(reply))


async def _complete_with_reject_retry(
    bot: Bot,
    messages: list[dict],
    text: str,
    images: list[dict],
    allow_search: bool,
    drop_content: str = "",
):
    """调用模型并对风控拒绝做两段重试。

    第一步：原样重试一次 —— 风控大多是概率性拦截，同样的请求再发
    一次常就过了。第二步：仍被拦且调用方给了 drop_content（爬虫检索
    资料段）就去掉它再试 —— 资料里的视频标题/简介是最常见的触发源，
    去掉后至少能正常对话。全部被拦返回 None，由调用方决定兜底话术。
    """
    resp = await _complete_with_images(
        bot, messages, text, images, allow_search=allow_search
    )
    reply = (resp.choices[0].message.content or "").strip()
    if not _looks_rejected(reply):
        return resp

    logger.warning(f"[ai_chat] 回复疑似风控文案，原样重试一次: {reply[:80]!r}")
    resp = await _complete_with_images(
        bot, messages, text, images, allow_search=allow_search
    )
    reply = (resp.choices[0].message.content or "").strip()
    if not _looks_rejected(reply):
        return resp

    if drop_content:
        logger.warning("[ai_chat] 仍被拦截，去掉检索资料后再试一次")
        retry_messages = [
            m
            for m in messages
            if not (m.get("role") == "system" and m.get("content") == drop_content)
        ]
        resp = await _complete_with_images(
            bot, retry_messages, text, images, allow_search=allow_search
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not _looks_rejected(reply):
            return resp

    logger.warning("[ai_chat] 风控重试全部失败，改用兜底话术")
    return None


def _maybe_sticker(text: str) -> Path | None:
    """决定这次要不要附带表情包。"""
    if not cfg.ai_sticker_enabled:
        return None
    directory = stickers.resolve_dir(cfg.ai_sticker_dir)
    if stickers.is_sticker_request(text):
        return stickers.pick_sticker(directory)
    if cfg.ai_sticker_probability > 0 and random.random() < cfg.ai_sticker_probability:
        return stickers.pick_sticker(directory)
    return None


def _speaker_label(event: GroupMessageEvent) -> str:
    """取发言人的称呼：群名片优先，其次昵称，实在没有就报 QQ 号。

    为什么要带发言人：聊天历史是整个群共用一份的。不给每句话标注是谁说的，
    模型就分不清「我喜欢小猫」是小a说的还是小b说的，问「我喜欢什么」时
    就会把别人说过的话安到提问的人头上（张冠李戴）。
    """
    sender = event.sender
    name = (
        (getattr(sender, "card", "") or getattr(sender, "nickname", "") or "")
    ).strip()
    if not name:
        name = f"QQ{event.user_id}"
    return name


def _chat_rule(bot: Bot, event: Event) -> bool:
    """@ 机器人在任意位置都算（适配器只认开头/结尾，图片打头时 @ 在中间会漏）。"""
    return tools.at_me(event, bot.self_id)


chat = on_message(rule=Rule(_chat_rule), priority=50, block=False)


@chat.handle()
async def handle_chat(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = event.group_id
    now = time.time()

    if cfg.ai_group_whitelist and group_id not in cfg.ai_group_whitelist:
        return

    # get_plaintext() 只取 text 消息段，图片和 @ 都会被丢掉 —— 所以都得单独抽。
    # @ 的还原尤其重要：「@机器人 @小a 他怎么样」如果直接走 get_plaintext()，
    # 到模型眼里只剩「他怎么样」，它根本不知道在说谁。
    message = event.get_message()
    at_qqs = members.extract_ats(message, event.self_id)
    at_names = (
        await members.resolve_ats(bot, group_id, at_qqs) if at_qqs else {}
    )
    text = members.render_text(message, at_names, event.self_id)

    images = (
        await vision.collect_images(bot, event, limit=cfg.ai_vision_max_images)
        if cfg.ai_vision_enabled
        else []
    )

    # 纯图片消息以前会在这里被 `if not text` 直接丢掉，
    # 连冷却和额度都不走，表现就是「发图没反应」。现在只要带了图就继续。
    # 注意 text 里已经去掉了对机器人自己的 @，所以「只 @ 机器人不说话」仍会被忽略。
    if not text and not images:
        return

    # 冷却：同一群短时间内只回一次
    if now - _last_call.get(group_id, 0.0) < cfg.ai_cooldown:
        return

    _roll_day_if_needed()

    if _usage[group_id] >= cfg.ai_daily_limit:
        _last_call[group_id] = now
        await chat.finish("今天的对话额度已经用完啦，明天再来找我～")

    _last_call[group_id] = now
    _usage[group_id] += 1

    # 日期、机器人名字、人设一起拼成 system 提示词（见 _system_prompt）；
    # 管理员设过人设覆盖的群会替换人设段（见 _effective_system_prompt）
    messages: list[dict] = [
        {"role": "system", "content": _effective_system_prompt(group_id)}
    ]

    context = await _build_context(bot, group_id, text, at_qqs, event.self_id)
    if context:
        messages.append({"role": "system", "content": context})

    # 长期记忆注入：/记住 存下的事实，按与本条消息的相关性挑出来给模型。
    # 放在检索结果之后、历史之前——记忆是「明确交代过的事」，优先级应高于闲聊历史。
    if cfg.ai_memory_enabled:
        mem_ctx = memory.describe(group_id, text, cfg.ai_memory_max_results)
        if mem_ctx:
            messages.append({"role": "system", "content": mem_ctx})

    messages.extend(_history[group_id])

    # 发言人标记只影响「给模型看的内容」，检索（context）仍用原始 text，
    # 不然名字前缀会参与关键词匹配，反而引入噪音。
    speaker = _speaker_label(event)
    speaker_text = f"【{speaker}】{text}"

    user_content = await vision.build_content(
        bot,
        speaker_text,
        images,
        cfg.ai_vision_default_prompt,
        mode=cfg.ai_vision_image_mode,
    )
    messages.append({"role": "user", "content": user_content})

    # 只有消息命中时效性关键词（或配置成 always）才允许联网。
    # 原因见 config.py 里 ai_web_search_trigger 的注释：挂上联网参数
    # 会让服务端多做一轮判断，每条消息白慢约 1.2 秒。
    allow_search = (
        cfg.ai_web_search_trigger == "always"
        or websearch.looks_like_web_query(text)
    )

    try:
        resp = await _complete_with_reject_retry(
            bot,
            messages,
            text,
            images,
            allow_search=allow_search,
            drop_content=context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调用模型失败: {exc!r}")
        await chat.finish("模型那边好像出问题了，稍后再试试～")

    if resp is None:
        # 风控重试全失败：不把英文拒绝文案丢群里，也不写进历史污染上下文
        await chat.finish("这条触发了上游内容风控，我答不了，换个问法试试～")

    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        logger.warning(
            "[ai_chat] 模型返回空正文，通常是思考过程把 max_tokens 占满了。"
            f"当前 max_tokens={cfg.ai_max_tokens}, "
            f"disable_thinking={cfg.ai_disable_thinking}, "
            f"reasoning_effort={cfg.ai_reasoning_effort!r}, usage={resp.usage}"
        )
        await chat.finish("模型光顾着思考没写出正文，把 AI_MAX_TOKENS 调大一点试试")

    # 历史里只留文字：把带图片的 content 数组存进历史的话，
    # 之后每一轮都会把图片重新发一遍，token 和延迟都会炸。
    # 每条历史都带【发言人】标记，模型才能把「谁说过什么」记在正确的人名下。
    _history[group_id].append(
        {
            "role": "user",
            "content": f"【{speaker}】{vision.history_text(text, images)}",
        }
    )
    _history[group_id].append({"role": "assistant", "content": reply})

    segments: list = []
    if cfg.ai_reply_quote:
        segments.append(MessageSegment.reply(event.message_id))
    # 链接转卡片：回复里的 B 站/网页链接自动变成分享卡片，失败退回纯文本
    segments.extend(await crawler.linkify(reply, proxy=cfg.ai_crawler_youtube_proxy))

    sticker = _maybe_sticker(text)
    if sticker is not None:
        # 用 file:// URI 而不是裸路径：CQ 码里反斜杠是转义符，Windows 路径会解析出错
        segments.append(MessageSegment.image(sticker.as_uri()))

    try:
        # 先直接 send：卡片段（share）个别 NapCat 版本可能不支持，
        # 失败就撤掉卡片退回纯文本重发，不能让整条回复消失
        await bot.send(event, Message(segments))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 带卡片发送失败，退回纯文本: {exc!r}")

    fallback: list = []
    if cfg.ai_reply_quote:
        fallback.append(MessageSegment.reply(event.message_id))
    fallback.append(MessageSegment.text(reply))
    await chat.finish(Message(fallback))


files_cmd = on_command("files", aliases={"文件", "资源"}, priority=10, block=True)


@files_cmd.handle()
async def handle_files(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    query = args.extract_plain_text().strip()

    if query in ("refresh", "刷新", "重建"):
        if event.sender.role not in ("owner", "admin"):
            await files_cmd.finish("只有群主或管理员可以重建索引")
        index = await resource_index.get_index(bot, event.group_id, force=True)
        if index.error:
            await files_cmd.finish(f"重建失败：{index.error}")
        await files_cmd.finish(
            f"索引已重建：群文件 {index.count_of('群文件')} 个，"
            f"群相册资源 {index.count_of('群相册')} 个，"
            f"共 {len(index.locations)} 个目录或相册"
        )

    index = await resource_index.get_index(bot, event.group_id)
    if index.error:
        await files_cmd.finish(f"读取群资源失败：{index.error}")

    if query in ("list", "列表", "目录"):
        if not index.entries:
            await files_cmd.finish("这个群还没有群文件或群相册资源")
        lines = [
            f"群文件 {index.count_of('群文件')} 个，群相册资源 {index.count_of('群相册')} 个。"
        ]
        for location in index.locations:
            count = sum(1 for entry in index.entries if entry.location == location)
            lines.append(f"　{location}（{count} 个）")
        await files_cmd.finish("\n".join(lines))

    if not query:
        await files_cmd.finish(
            "用法：\n"
            "/files 关键词 —— 检索群文件与群相册\n"
            "/files list —— 列出所有目录与相册\n"
            "/files refresh —— 重建索引（管理员）"
        )

    hits = resource_index.search(index, query, limit=cfg.ai_file_max_results)
    if not hits:
        await files_cmd.finish(f"没有找到和「{query}」相关的资源")

    lines = [f"和「{query}」相关的资源，共 {len(hits)} 个："]
    for entry in hits:
        size = f"　（{resource_index.fmt_size(entry.size)}）" if entry.size else ""
        lines.append(f"　[{entry.kind}] {entry.name}　→　{entry.where}{size}")
    await files_cmd.finish("\n".join(lines))


doc_cmd = on_command("doc", aliases={"文档", "读文件"}, priority=10, block=True)


@doc_cmd.handle()
async def handle_doc(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """直接搜文件正文，不走模型，秒回。

    和 /files 的分工：/files 搜「文件叫什么」，/doc 搜「文件里写了什么」。
    """
    if not cfg.ai_filetext_enabled:
        await doc_cmd.finish(
            "群文件内容检索当前是关闭的（.env 里 AI_FILETEXT_ENABLED=false）"
        )

    query = args.extract_plain_text().strip()
    if not query:
        await doc_cmd.finish(
            "用法：\n"
            "/doc 关键词 —— 在群文件的正文内容里搜索\n"
            "/doc refresh —— 重建内容索引（管理员，会重新下载文件）"
        )

    if query in ("refresh", "刷新", "重建"):
        if event.sender.role not in ("owner", "admin"):
            await doc_cmd.finish("只有群主或管理员可以重建内容索引")
        rindex = await resource_index.get_index(
            bot, event.group_id, ttl=cfg.ai_file_index_ttl
        )
        candidates = [
            e
            for e in rindex.entries
            if e.source == "群文件"
            and e.file_id
            and filetext.is_readable(e.name, e.size, cfg.ai_filetext_max_file_size)
        ]
        if not candidates:
            await doc_cmd.finish("群里没有可以读取正文的文档（支持 txt/md/docx/pdf 等）")
        cidx = await filetext.get_index(
            bot,
            event.group_id,
            candidates,
            max_files=cfg.ai_filetext_max_files,
            max_file_size=cfg.ai_filetext_max_file_size,
            chunk_size=cfg.ai_filetext_chunk_size,
            timeout=cfg.ai_filetext_timeout,
            ttl=cfg.ai_filetext_ttl,
            force=True,
        )
        await doc_cmd.finish(
            "内容索引开始重建（在后台下载和抽取，不影响机器人使用）。\n"
            f"待处理文档 {len(candidates)} 个，已经就绪 {len(cidx.chunks)} 段。"
        )

    rindex = await resource_index.get_index(
        bot, event.group_id, ttl=cfg.ai_file_index_ttl
    )
    if rindex.error:
        await doc_cmd.finish(f"读取群文件失败：{rindex.error}")
    candidates = [
        e
        for e in rindex.entries
        if e.source == "群文件"
        and e.file_id
        and filetext.is_readable(e.name, e.size, cfg.ai_filetext_max_file_size)
    ]
    if not candidates:
        await doc_cmd.finish("群里没有可以读取正文的文档（支持 txt/md/docx/pdf 等）")

    cidx = await filetext.get_index(
        bot,
        event.group_id,
        candidates,
        max_files=cfg.ai_filetext_max_files,
        max_file_size=cfg.ai_filetext_max_file_size,
        chunk_size=cfg.ai_filetext_chunk_size,
        timeout=cfg.ai_filetext_timeout,
        ttl=cfg.ai_filetext_ttl,
    )
    if not cidx.chunks:
        if cidx.building:
            await doc_cmd.finish(
                "文件内容索引正在后台构建（第一次要下载文件，大概一两分钟），"
                "稍后再试一次就有结果了。"
            )
        await doc_cmd.finish(
            f"内容索引是空的（{cidx.files_failed} 个文件抽不出文本或下载失败）。"
            "试试 /doc refresh 重建"
        )

    hits = filetext.search(cidx, query, limit=cfg.ai_filetext_max_results)
    if not hits:
        await doc_cmd.finish(f"群文件正文里没有找到和「{query}」相关的内容")
    if cidx.building:
        await doc_cmd.finish(
            f"索引还在后台构建，先给你目前已有的结果（共 {len(hits)} 条）：\n"
            + "\n".join(
                f"　{idx}. 「{snippet}」\n　　—— {chunk.file_name}（{chunk.location}）"
                for idx, (chunk, _, snippet) in enumerate(hits, 1)
            )
        )
    await doc_cmd.finish(
        f"群文件正文里和「{query}」相关的内容有 {len(hits)} 处：\n"
        + "\n".join(
            f"　{idx}. 「{snippet}」\n　　—— {chunk.file_name}（{chunk.location}）"
            for idx, (chunk, _, snippet) in enumerate(hits, 1)
        )
    )


essence_cmd = on_command("essence", aliases={"精华"}, priority=10, block=True)


@essence_cmd.handle()
async def handle_essence(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    query = args.extract_plain_text().strip()

    if query in ("refresh", "刷新", "重建"):
        if event.sender.role not in ("owner", "admin"):
            await essence_cmd.finish("只有群主或管理员可以重建精华索引")
        index = await essence.get_index(bot, event.group_id, force=True)
        if index.error:
            await essence_cmd.finish(f"重建失败：{index.error}")
        await essence_cmd.finish(f"精华索引已重建：共 {len(index)} 条精华消息")

    index = await essence.get_index(bot, event.group_id)
    if index.error:
        await essence_cmd.finish(f"读取群精华失败：{index.error}")
    if not index.messages:
        await essence_cmd.finish("这个群还没有精华消息")

    if not query:
        await essence_cmd.finish(
            f"本群共有 {len(index)} 条精华消息：\n" + essence.render_all(index.messages)
        )

    hits = essence.search(index, query, limit=cfg.ai_essence_max_results)
    if not hits:
        await essence_cmd.finish(f"精华消息里没有和「{query}」相关的内容")
    await essence_cmd.finish(
        f"精华消息里和「{query}」相关的有 {len(hits)} 条：\n" + essence.render_all(hits)
    )


history_cmd = on_command(
    "history", aliases={"历史", "聊天记录"}, priority=10, block=True
)


@history_cmd.handle()
async def handle_history(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    if not cfg.ai_history_enabled:
        await history_cmd.finish(
            "群历史检索当前是关闭的（.env 里 AI_HISTORY_ENABLED=false）"
        )

    query = args.extract_plain_text().strip()

    if query in ("refresh", "刷新", "重建"):
        if event.sender.role not in ("owner", "admin"):
            await history_cmd.finish("只有群主或管理员可以重建历史索引")
        index = await history.get_index(
            bot,
            event.group_id,
            force=True,
            max_fetch=cfg.ai_history_max_fetch,
        )
        if index.error:
            await history_cmd.finish(f"重建失败：{index.error}")
        await history_cmd.finish(f"历史消息索引已重建：共收录 {len(index.entries)} 条")
    elif query.startswith("recent") or query.startswith("最近"):
        index = await history.get_index(
            bot, event.group_id, ttl=cfg.ai_history_index_ttl,
            max_fetch=cfg.ai_history_max_fetch,
        )
        if index.error:
            await history_cmd.finish(f"读取群历史失败：{index.error}")
        recent = index.entries[-10:]
        await history_cmd.finish(
            "最近的消息：\n" + history.render_hits(recent)
        )

    index = await history.get_index(
        bot, event.group_id, ttl=cfg.ai_history_index_ttl,
        max_fetch=cfg.ai_history_max_fetch,
    )
    if index.error:
        await history_cmd.finish(f"读取群历史失败：{index.error}")

    if not query:
        await history_cmd.finish(
            f"已收录最近 {len(index.entries)} 条群消息。用法：\n"
            "/history 关键词 —— 检索谁说过什么\n"
            "/history recent —— 看最近 10 条\n"
            "/history refresh —— 重建索引（管理员）"
        )

    hits = history.search(index, query, limit=cfg.ai_history_max_results)
    if not hits:
        await history_cmd.finish(
            f"最近的群消息里没有找到「{query}」（索引范围是最近 "
            f"{len(index.entries)} 条，更早的就翻不到了）"
        )
    await history_cmd.finish(
        f"和「{query}」相关的消息有 {len(hits)} 条：\n" + history.render_hits(hits)
    )


member_cmd = on_command(
    "member", aliases={"成员", "群成员", "members"}, priority=10, block=True
)


@member_cmd.handle()
async def handle_member(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    query = args.extract_plain_text().strip()

    if query in ("refresh", "刷新", "重建"):
        if event.sender.role not in ("owner", "admin"):
            await member_cmd.finish("只有群主或管理员可以重建成员名单")
        index = await members.get_index(bot, event.group_id, force=True)
        if index.error:
            await member_cmd.finish(f"重建失败：{index.error}")
        await member_cmd.finish(f"成员名单已重建：共 {len(index.members)} 人")

    if not cfg.ai_member_enabled:
        await member_cmd.finish(
            "群成员识别当前是关闭的（.env 里 AI_MEMBER_ENABLED=false）"
        )

    index = await members.get_index(
        bot, event.group_id, ttl=cfg.ai_member_index_ttl
    )
    if index.error:
        await member_cmd.finish(f"读取群成员失败：{index.error}")

    hits = members.search(index, query)
    if not hits:
        await member_cmd.finish(f"没有找到和「{query}」相关的群成员")
    await member_cmd.finish(
        f"本群共 {len(index.members)} 人，匹配到 {len(hits)} 个：\n"
        + members.render_all(hits)
    )


search_cmd = on_command("search", aliases={"联网", "联网查询"}, priority=10, block=True)


@search_cmd.handle()
async def handle_search(
    event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    query = args.extract_plain_text().strip()

    if query in ("reset", "重置", "清零"):
        if event.sender.role not in ("owner", "admin"):
            await search_cmd.finish("只有群主或管理员可以重置联网额度")
        websearch.reset()
        await search_cmd.finish("今日联网额度已重置")

    if not cfg.ai_web_search_enabled:
        await search_cmd.finish(
            "联网搜索当前是关闭的（.env 里 AI_WEB_SEARCH_ENABLED=false）"
        )

    info = websearch.snapshot(cfg.ai_web_search_daily_limit)
    force = "强制联网" if cfg.ai_web_search_force else "由模型自行判断是否需要联网"
    trigger = (
        "每条消息都可联网"
        if cfg.ai_web_search_trigger == "always"
        else "命中时效性关键词才联网"
    )
    await search_cmd.finish(
        f"联网搜索额度（{info['day']}）\n"
        f"　已用 {info['used']} / {info['limit']} 次\n"
        f"　剩余 {info['remaining']} 次\n"
        f"　实际触发联网的回答 {info['rounds']} 条\n"
        f"　触发方式：{trigger}\n"
        f"　每轮最多 {cfg.ai_web_search_max_keyword} 个关键词（{force}）\n"
        f"　注：计费单位是插件调用次数，一轮搜索若用 2 个关键词就计 2 次"
    )


reset = on_command("reset", priority=10, block=True)


@reset.handle()
async def handle_reset(bot: Bot, event: GroupMessageEvent) -> None:
    if not _is_bot_admin(bot, event):
        await reset.finish("只有群主或管理员可以清空上下文")
    _history.pop(event.group_id, None)
    await reset.finish("本群上下文已清空")


# ---------------------------------------------------------------- 群助手指令（帮助 / 状态 / 长期记忆 / 人设）

help_cmd = on_command("help", aliases={"帮助", "功能", "菜单"}, priority=10, block=True)

HELP_TEXT = (
    "我是本群的 AI 助手，常用功能：\n"
    "· @我 + 任意问题 —— 对话（带图片也能看图说话）\n"
    "· @我 + 图片 + 说「存相册」—— 收进群相册\n"
    "· @我 + 来首 XXX —— 点歌分享\n"
    "· /文件 XXX —— 查群文件在哪；/文档 XXX —— 查文件内容写了什么\n"
    "· /精华 XXX —— 翻群精华；/历史 XXX —— 查刚才谁说过什么\n"
    "· /成员 XXX —— 查群成员资料\n"
    "· /记住 XXX —— 让我长期记住一件事；/记忆 —— 看我记住了什么\n"
    "· /找图 XXX —— 按内容找群相册里的图；存图时我会自动记住图里有什么\n"
    "· /链接 [关键词] —— 翻本群收藏过的链接（群里发链接我自动收）\n"
    "· /绑定文档 —— 看在线文档绑定状态；/绑定文档 链接 / /创建文档 标题（管理员）\n"
    "· /状态 —— 看今天的对话/联网/主动发言额度\n"
    "· /人设 —— 看当前人设（管理员可换可恢复）\n"
    "· /reset —— 管理员清空本群对话上下文\n"
    "· /帮助 —— 显示本清单\n"
    "另有管理类命令（/踢 /禁言 /撤回 /设精华 等），需要群主给我管理员身份，"
    "发 /踢 可看用法。"
)


@help_cmd.handle()
async def handle_help() -> None:
    await help_cmd.finish(HELP_TEXT)


status_cmd = on_command("status", aliases={"状态", "额度"}, priority=10, block=True)


@status_cmd.handle()
async def handle_status(event: GroupMessageEvent) -> None:
    _roll_day_if_needed()
    group_id = event.group_id
    search = websearch.snapshot(cfg.ai_web_search_daily_limit)
    pro = proactive.stats(group_id)
    rounds = len(_history.get(group_id, ()))
    persona_src, persona_len = _persona_source(group_id)
    cooldown_left = max(
        0, int(cfg.ai_proactive_cooldown - (time.time() - pro["last"]))
    )
    lines = [
        f"本群状态（{date.today().isoformat()}）",
        f"　对话额度：今天已用 {_usage.get(group_id, 0)} / {cfg.ai_daily_limit} 次",
        f"　上下文：{rounds} 条（约 {rounds // 2} 轮，上限 {cfg.ai_history_rounds} 轮）",
        f"　联网额度：已用 {search['used']} / {search['limit']} 次（剩 {search['remaining']}）",
        f"　长期记忆：{memory.count(group_id)} 条（/记忆 查看）",
        f"　主动发言：今天已用 {pro['used']} / {cfg.ai_proactive_daily_limit} 次",
        f"　　下次可触发：{'冷却中，剩约 %d 分钟' % (cooldown_left // 60) if cooldown_left > 0 else '随时'}",
        f"　人设：{persona_src}"
        + (f"（{persona_len} 字）" if persona_len else ""),
    ]
    await status_cmd.finish("\n".join(lines))


remember_cmd = on_command("remember", aliases={"记住", "记一下"}, priority=10, block=True)


@remember_cmd.handle()
async def handle_remember(event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    if not cfg.ai_memory_enabled:
        await remember_cmd.finish("长期记忆功能当前是关闭的")
    text = args.extract_plain_text().strip()
    if not text:
        await remember_cmd.finish("用法：/记住 XXX（比如 /记住 张三的生日是 1 月 1 日）")
    if len(text) > 120:
        await remember_cmd.finish("太长了，一条记忆控制在 120 字以内，拆成几条来记吧")
    result = memory.add(
        event.group_id,
        text,
        by=_speaker_label(event),
        max_entries=cfg.ai_memory_max_per_group,
    )
    if result == "dup":
        await remember_cmd.finish("这条我已经记着了，不用重复教～")
    await remember_cmd.finish(
        f"记住了（第 {memory.count(event.group_id)} 条）。问相关问题的时候我会想起来的"
    )


memory_cmd = on_command("memory", aliases={"记忆"}, priority=10, block=True)


@memory_cmd.handle()
async def handle_memory(event: GroupMessageEvent) -> None:
    await memory_cmd.finish(memory.list_text(event.group_id))


forget_cmd = on_command("forget", aliases={"忘记", "忘了"}, priority=10, block=True)


@forget_cmd.handle()
async def handle_forget(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    text = args.extract_plain_text().strip()
    if not text:
        await forget_cmd.finish("用法：/忘记 关键词（删掉包含这个词的记忆）")
    if not _is_bot_admin(bot, event):
        await forget_cmd.finish("只有群主或管理员可以删除记忆，防止有人乱删别人记的东西")
    if text in ("全部", "清空", "所有"):
        removed = memory.clear(event.group_id)
        await forget_cmd.finish(f"已清空全部 {removed} 条记忆")
    removed = memory.remove(event.group_id, text)
    if removed:
        await forget_cmd.finish(f"已删掉 {removed} 条包含「{text}」的记忆")
    await forget_cmd.finish(f"没有找到包含「{text}」的记忆，/记忆 可以看现存列表")


persona_cmd = on_command("persona", aliases={"人设"}, priority=10, block=True)


@persona_cmd.handle()
async def handle_persona(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    text = args.extract_plain_text().strip()

    if not text:
        src, length = _persona_source(event.group_id)
        current = _effective_system_prompt(event.group_id)
        preview = current[:100] + ("…" if len(current) > 100 else "")
        await persona_cmd.finish(
            f"当前人设：{src}" + (f"（{length} 字）" if length else "") + f"\n开头是：{preview}\n"
            "管理员可发 /人设 新人设文本 换人设；/人设 恢复 回默认。"
            "注意：换人设只换性格，资源检索铁律会自动保留。"
        )

    if not cfg.ai_persona_override_enabled:
        await persona_cmd.finish("人设覆盖功能当前是关闭的（AI_PERSONA_OVERRIDE_ENABLED=false）")
    if not _is_bot_admin(bot, event):
        await persona_cmd.finish("只有群主或管理员可以换人设")

    if text in ("恢复", "重置", "默认", "还原"):
        _set_persona(event.group_id, "")
        await persona_cmd.finish("已恢复默认人设")

    if len(text) < cfg.ai_persona_min_len:
        await persona_cmd.finish(
            f"新人设太短了，至少 {cfg.ai_persona_min_len} 字——"
            "把性格、说话方式描述清楚，效果才稳定"
        )
    if len(text) > 2000:
        await persona_cmd.finish("人设最长 2000 字，精简一点反而效果更好")

    _set_persona(event.group_id, text)
    await persona_cmd.finish(
        f"本群人设已更新（{len(text)} 字），立刻生效。\n"
        "资源检索铁律已自动附加，我不会因此开始编造文件名～"
    )


# ---------------------------------------------------------------- 新成员入群欢迎

notice = on_notice(priority=50, block=False)


@notice.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    """有人入群时，按当前人设欢迎一句。

    和聊天最大的区别：这是**主动发消息**。聊天只在被 @ 时才说话，
    这里却是机器人自己开口 —— 而「短时间连发多条消息」正是 QQ 风控最敏感的行为。
    所以限流比聊天更严：同群冷却 + 每群每日上限，一次拉一群人时只会欢迎第一个。
    真觉得有风险就把 .env 里的 AI_WELCOME_ENABLED 改成 false。
    """
    if not cfg.ai_welcome_enabled:
        return

    group_id = event.group_id
    user_id = int(event.user_id)

    if cfg.ai_group_whitelist and group_id not in cfg.ai_group_whitelist:
        return

    # 机器人自己进群时不自我欢迎
    if user_id == int(event.self_id):
        return

    now = time.time()
    reason = welcome.check(
        group_id, now, cfg.ai_welcome_cooldown, cfg.ai_welcome_daily_limit
    )
    if reason:
        logger.info(f"[ai_chat] 跳过入群欢迎（{reason}），群 {group_id}")
        return

    # 名单变了，让缓存作废，下次对话时会自动重建（新人也就能被识别到）
    members.invalidate(group_id)
    name = await members.fetch_member_name(bot, group_id, user_id)

    reply = ""
    try:
        resp = await _create_completion(
            welcome.build_messages(_system_prompt(), name, user_id),
            allow_search=False,
        )
        reply = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        # 模型挂了也得欢迎，退回模板。绝不因为生成失败就沉默。
        logger.warning(f"[ai_chat] 生成入群欢迎语失败，改用模板: {exc!r}")

    if not reply:
        reply = welcome.pick_template(name)
    else:
        # 只取第一行：模型偶尔会写成小作文或带列表符号，群里刷屏很难看
        reply = reply.splitlines()[0].strip() or welcome.pick_template(name)

    # 先记额度再发，避免发送失败后反复重试把额度漏光
    welcome.mark(group_id, now)

    try:
        await bot.send_group_msg(
            group_id=group_id,
            message=MessageSegment.at(user_id) + MessageSegment.text(f" {reply}"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 发送入群欢迎失败: {exc!r}")


welcome_cmd = on_command("welcome", aliases={"欢迎"}, priority=10, block=True)


@welcome_cmd.handle()
async def handle_welcome(event: GroupMessageEvent) -> None:
    info = welcome.stats(event.group_id)
    await welcome_cmd.finish(
        f"入群欢迎（{info['day']}）\n"
        f"　状态：{'开启' if cfg.ai_welcome_enabled else '关闭'}"
        f"（AI_WELCOME_ENABLED）\n"
        f"　本群今日已欢迎 {info['count']} / {cfg.ai_welcome_daily_limit} 次\n"
        f"　同群冷却：{cfg.ai_welcome_cooldown:g} 秒"
    )


# ---------------------------------------------------------------- 私聊对话

# 私聊状态与群聊分开记：{user_id: ...}
_private_history: DefaultDict[int, Deque[dict]] = defaultdict(
    lambda: deque(maxlen=cfg.ai_private_history_rounds * 2)
)
_private_last: Dict[int, float] = {}
_private_usage: DefaultDict[int, int] = defaultdict(int)

def _private_chat_rule(bot: Bot, event: Event) -> bool:
    """@ 机器人在任意位置都算（适配器只认开头/结尾，图片打头时 @ 在中间会漏）。"""
    return tools.at_me(event, bot.self_id)


private_chat = on_message(rule=Rule(_private_chat_rule), priority=50, block=False)


@private_chat.handle()
async def handle_private_chat(bot: Bot, event: PrivateMessageEvent) -> None:
    """私聊也走 AI 对话。

    和群聊的差异：没有群资源/精华/成员检索（那些上下文在私聊里无意义），
    识图和联网照常可用；限流按「每个人」而不是按群。
    """
    if not cfg.ai_private_enabled:
        return

    user_id = int(event.user_id)
    now = time.time()
    if now - _private_last.get(user_id, 0.0) < cfg.ai_private_cooldown:
        return

    _roll_day_if_needed()
    if _private_usage[user_id] >= cfg.ai_private_daily_limit:
        _private_last[user_id] = now
        await private_chat.finish("今天的对话额度已经用完啦，明天再来找我～")

    message = event.get_message()
    text = members.render_text(message, {}, event.self_id)

    images = (
        await vision.collect_images(bot, event, limit=cfg.ai_vision_max_images)
        if cfg.ai_vision_enabled
        else []
    )
    if not text and not images:
        return

    _private_last[user_id] = now
    _private_usage[user_id] += 1

    messages: list[dict] = [{"role": "system", "content": _system_prompt()}]
    messages.extend(_private_history[user_id])

    user_content = await vision.build_content(
        bot,
        text,
        images,
        cfg.ai_vision_default_prompt,
        mode=cfg.ai_vision_image_mode,
    )
    messages.append({"role": "user", "content": user_content})

    allow_search = (
        cfg.ai_web_search_trigger == "always"
        or websearch.looks_like_web_query(text)
    )

    try:
        resp = await _complete_with_reject_retry(
            bot, messages, text, images, allow_search=allow_search
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 私聊调用模型失败: {exc!r}")
        await private_chat.finish("模型那边好像出问题了，稍后再试试～")

    if resp is None:
        await private_chat.finish("这条触发了上游内容风控，我答不了，换个问法试试～")

    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        await private_chat.finish("模型光顾着思考没写出正文，把 AI_MAX_TOKENS 调大一点试试")

    _private_history[user_id].append(
        {"role": "user", "content": vision.history_text(text, images)}
    )
    _private_history[user_id].append({"role": "assistant", "content": reply})

    segments: list = []
    if cfg.ai_reply_quote:
        segments.append(MessageSegment.reply(event.message_id))
    # 链接转卡片：回复里的 B 站/网页链接自动变成分享卡片，失败退回纯文本
    segments.extend(await crawler.linkify(reply, proxy=cfg.ai_crawler_youtube_proxy))

    sticker = _maybe_sticker(text)
    if sticker is not None:
        segments.append(MessageSegment.image(sticker.as_uri()))

    try:
        await bot.send(event, Message(segments))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 带卡片发送失败，退回纯文本: {exc!r}")

    fallback: list = [MessageSegment.text(reply)]
    if cfg.ai_reply_quote:
        fallback.insert(0, MessageSegment.reply(event.message_id))
    await private_chat.finish(Message(fallback))
