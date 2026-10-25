"""AI 群聊助手：以吉普莉尔的人设聊天，检索群资源（群文件 + 群相册 + 群精华），并支持识图。

设计要点：
- 分群独立上下文，群与群之间互不串台
- 上下文按轮数自动截断，避免输入 token 无限膨胀
- 群资源检索走「本地索引 + 关键词匹配」，模型只负责组织语言，杜绝编造文件名
- 群精华消息同理：先抓下来本地匹配，再说清是哪位群友说的
- 人设（吉普莉尔）只影响语气，事实准确性由系统提示词里的「铁律」兜底
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
import base64
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

from .ai_client import chat_kwargs, make_client
from . import (
    account_face,
    admin,
    album,
    album_index,
    ambient,
    crawler,
    digest,
    docsync,
    essence,
    filetext,
    forward,
    history,
    imagegen,
    intent,
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
    name="AI 群聊助手（吉普莉尔）",
    description=(
        "在 QQ 群里 @ 机器人即可聊天，人设为《游戏人生》的吉普莉尔；"
        "支持检索群文件、群相册、群精华与群历史消息（回答「群里有没有 XX」），"
        "支持识别群友发来的图片（需模型本身是多模态的），"
        "支持识别被 @ 或被提到的群成员，每个人的发言在记忆里单独归档、互不串台，"
        "可选联网搜索获取实时信息（带每日额度限制），"
        "支持网页爬虫（发链接总结正文、搜B站、搜GitHub）、"
        "点歌分享（网易云音乐卡片）、存图进群相册，"
        "支持语音转文字、图片 OCR、合并转发收发、戳一戳、表情回应、"
        "群签到、群荣誉查询、QQ AI 语音，私聊也能对话；"
        "管理命令（踢人/禁言/撤回/设精华/头衔等）已就绪，授予机器人管理员后即可生效；"
        "新成员入群时自动欢迎。"
    ),
    usage=(
        "@机器人 你的问题\n"
        "@机器人 + 图片 —— 识图（需模型支持多模态）\n"
        "@机器人 存相册 + 图片 —— 把图存进群相册\n"
        "@机器人 来首晴天 / 点歌 歌名 —— 分享音乐卡片到群里\n"
        "直接发一个链接 —— 机器人抓网页正文帮你总结\n"
        "@机器人 去b站找xx视频 / 去github找xx项目 —— 真实搜索后给链接\n"
        "@机器人 @某群友 问关于他的问题 —— 会知道说的是谁\n"
        "/files 关键词 —— 直接检索群资源，不走模型，秒回\n"
        "/files list —— 列出所有目录与相册\n"
        "/files refresh —— 重建索引（限群主与管理员）\n"
        "/doc 关键词 —— 在群文件的正文内容里搜索（机器人会读文件）\n"
        "/essence —— 列出本群精华消息\n"
        "/essence 关键词 —— 在精华消息里检索\n"
        "/essence refresh —— 重建精华索引（限群主与管理员）\n"
        "/history 关键词 —— 检索最近的群聊记录\n"
        "/history refresh —— 重建历史消息索引\n"
        "/成员 关键词 —— 按名字或 QQ 号查群成员\n"
        "/成员 refresh —— 重建成员名单（限群主与管理员）\n"
        "/转文字 —— 回复语音消息，识别语音内容\n"
        "/ocr —— 回复图片，提取图中的文字\n"
        "/戳 @某人 —— 戳一戳（被戳会回戳）\n"
        "/回应 👍 —— 回复某条消息贴表情回应\n"
        "/签到 —— 群签到打卡\n"
        "/荣誉 —— 查看龙王等群荣誉榜\n"
        "/语音 文本 —— 用 QQ AI 语音说话；/语音列表 查看音色\n"
        "/转发 内容1||内容2 —— 发合并转发；/解析 —— 展开合并转发\n"
        "/踢 /禁言 /解禁 /全体禁言 /撤回 /设精华 /取消精华 /头衔 /名片 /管理员 —— 管理命令（需机器人管理员身份）\n"
        "/search —— 查看今日联网额度用量\n"
        "/search reset —— 重置今日联网额度（限群主与管理员）\n"
        "/欢迎 —— 查看入群欢迎的开关与用量\n"
        "/reset —— 清空本群上下文（限群主与管理员）\n"
        "私聊直接发消息也能聊"
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


def _assistant_iron_rules() -> str:
    """从默认人设里抽出「铁律」段落（防止编造文件名等约束）。

    管理员换人设只换性格，铁律必须保留——群助手功能大于角色扮演，
    换成人设后机器人照样不能编造文件名、不能混淆发言人。
    抽取方式是按「【铁律」分段，跟着默认人设走，改默认提示词不用改这里。
    """
    if "【铁律" not in cfg.ai_system_prompt:
        return ""
    return "\n\n【铁律 —— 优先级高于上面所有性格设定】" + cfg.ai_system_prompt.split(
        "【铁律", 1
    )[1]


def _persona_for(group_id: int) -> str:
    """本群实际生效的人设：管理员设置过覆盖就用「覆盖 + 铁律」，否则用默认。"""
    try:
        data = json.loads(_PERSONA_PATH.read_text(encoding="utf-8"))
        override = ((data.get("groups") or {}).get(str(group_id)) or "").strip()
    except (OSError, ValueError):
        override = ""
    if not override:
        return cfg.ai_system_prompt
    return override + _assistant_iron_rules()


def _persona_source(group_id: int) -> tuple[str, int]:
    """人设来源描述（给 /人设 看的）：(来源, 自定义文本长度)。"""
    try:
        data = json.loads(_PERSONA_PATH.read_text(encoding="utf-8"))
        override = ((data.get("groups") or {}).get(str(group_id)) or "").strip()
    except (OSError, ValueError):
        override = ""
    return ("本群自定义" if override else "默认（吉普莉尔）"), len(override)


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
    # 实测教训：问「虚拟机」时触发词全没命中，检索层整个被跳过，
    # 模型顺着聊天历史编了个不存在的安装包。补上常见的资源类名词。
    "安装包", "补丁", "驱动", "电子书", "种子", "网盘", "资源",
)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """懒加载 OpenAI 客户端，避免没配 Key 时启动就崩。"""
    global _client
    if _client is None:
        if not cfg.ai_api_key:
            raise RuntimeError("未配置 AI_API_KEY，请检查 .env 文件")
        _client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    return _client


def _roll_day_if_needed() -> None:
    """跨天时清零用量统计（群聊和私聊一起清）。"""
    global _usage_day
    today = date.today().isoformat()
    if today != _usage_day:
        _usage.clear()
        _private_usage.clear()
        _usage_day = today


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


async def _build_ambient_context(bot: Bot, group_id: int, text: str) -> str:
    """最近群聊背景：让机器人接得上 @ 它之前群里在聊的话题。

    没 @ 机器人的群聊由 ambient 模块在后台记进环形缓冲，这里取最近几条
    注入。和 digest 滚动摘要是互补关系：摘要粗而久（跨小时的话题），
    这里是逐字原文（刚才那几句的细节）。没有记录就返回空串。
    """
    if not cfg.ai_ambient_enabled:
        return ""
    return ambient.describe(group_id, cfg.ai_ambient_context_count)


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
        _build_ambient_context,
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
    kwargs: dict = chat_kwargs(
        messages,
        cfg,
        max_tokens=cfg.ai_max_tokens,
        temperature=cfg.ai_temperature,
    )

    # 额度闸门：用尽后不再挂 tools。不挂 tools 就不会产生联网费用，
    # 对话本身照常进行，只是回答基于模型已有知识。
    # 注意 client 模式（DeepSeek Search API）不在这里挂任何 tools——
    # 检索在调用模型前就完成了，结果已作为「联网检索结果」注入上下文，
    # Chat Completions 端点也不认厂商私有的 web_search 工具。
    search_attached = False
    if (
        cfg.ai_web_search_enabled
        and allow_search
        and cfg.ai_web_search_mode == "server"
    ):
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
            "改成 false 就不会再报这个告警。"
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


# 模型幻觉：冒充系统输出「审核通知」之类的罐头文本
# （实测 mimo 对「Step-5-Preview 是谁家的模型」这类问题吐出过
#   「【系统提示：本条消息为系统自动内容审核通知，非群成员发言，无需归档…】」）。
# 特征组合要具体：单有「系统提示」会误杀正常讲电脑操作的长回复。
_FAKE_NOTICE_RE = re.compile(r"【?\s*系统提示[^】]{0,80}】?", re.IGNORECASE)
_FAKE_NOTICE_HINTS = ("审核", "归档", "非群成员", "自动内容")


def _looks_fake_notice(reply: str) -> bool:
    """识别模型冒充系统发出的假「内容审核通知」。"""
    return len(reply) < 200 and bool(_FAKE_NOTICE_RE.search(reply)) and any(
        h in reply for h in _FAKE_NOTICE_HINTS
    )


async def _complete_with_reject_retry(
    bot: Bot,
    messages: list[dict],
    text: str,
    images: list[dict],
    allow_search: bool,
    drop_contents: list[str] | None = None,
):
    """调用模型并对风控拒绝做两段重试。

    第一步：原样重试一次 —— 风控大多是概率性拦截，同样的请求再发
    一次常就过了。第二步：仍被拦且调用方给了 drop_contents（检索资料段）
    就去掉它们再试 —— 资料里的视频标题/网页摘录是最常见的触发源，
    去掉后至少能正常对话。全部被拦返回 None，由调用方决定兜底话术。
    """
    resp = await _complete_with_images(
        bot, messages, text, images, allow_search=allow_search
    )
    reply = (resp.choices[0].message.content or "").strip()
    if not _looks_rejected(reply):
        if not _looks_fake_notice(reply):
            return resp
        # 假系统通知：带着纠偏提示重试一次，重试结果不再二次校验
        logger.warning(f"[ai_chat] 回复疑似模型幻觉的系统通知，纠偏重试: {reply[:80]!r}")
        fixup = messages + [
            {
                "role": "user",
                "content": "（刚才你的回复输出了一段不存在的系统审核通知，这是错误的。"
                "请直接正常回答上一个问题，绝不要输出任何「系统提示/内容审核通知」样式的文字。）",
            }
        ]
        resp = await _complete_with_images(
            bot, fixup, text, images, allow_search=allow_search
        )
        reply = (resp.choices[0].message.content or "").strip()
        if reply and not _looks_rejected(reply):
            return resp
        logger.warning("[ai_chat] 纠偏重试仍异常，改用兜底话术")
        return None

    logger.warning(f"[ai_chat] 回复疑似风控文案，原样重试一次: {reply[:80]!r}")
    resp = await _complete_with_images(
        bot, messages, text, images, allow_search=allow_search
    )
    reply = (resp.choices[0].message.content or "").strip()
    if not _looks_rejected(reply):
        return resp

    if drop_contents:
        logger.warning("[ai_chat] 仍被拦截，去掉检索资料后再试一次")
        retry_messages = [
            m
            for m in messages
            if not (
                m.get("role") == "system" and m.get("content") in drop_contents
            )
        ]
        resp = await _complete_with_images(
            bot, retry_messages, text, images, allow_search=allow_search
        )
        reply = (resp.choices[0].message.content or "").strip()
        if not _looks_rejected(reply):
            return resp

    logger.warning("[ai_chat] 风控重试全部失败，改用兜底话术")
    return None


# 表情包防刷屏：{会话ID: 上次发表情的时间戳}。群聊传群号，私聊传负用户ID。
# 没有这层冷却的话，「好的」「晚安」这类高频词会每次回复都带图。
_last_sticker: Dict[int, float] = {}


async def _maybe_sticker(bot: Bot, text: str, chat_id: int) -> MessageSegment | None:
    """决定这次要不要附带表情包（chat_id 用于冷却，群聊传群号、私聊传负用户ID）。

    表情来源分三种，优先级从高到低：
    1. 明确索要「收藏表情」—— 账号收藏（fetch_custom_face 拉的 URL）
    2. 明确索要「表情包」—— 默认也优先账号收藏，可用
       AI_ACCOUNT_FACE_ON_REQUEST 关掉；账号那边没图才回本地 stickers/
    3. 语义匹配 —— 只走本地：语义表按文件名匹配，账号收藏没有语义可言
    4. 随机附带 —— 账号收藏和本地按 AI_ACCOUNT_FACE_MIX 概率混发

    返回 None 表示这次不发。返回值直接就是可发送的消息段
    （本地图走 base64，收藏表情走 URL），调用方拼进 segments 即可。
    """
    if not cfg.ai_sticker_enabled:
        return None
    directory = stickers.resolve_dir(cfg.ai_sticker_dir)
    now = time.time()
    seg: MessageSegment | None = None

    if stickers.is_sticker_request(text):
        # 明确索要：10 秒短冷却——连着要三次也只当一次，但不能不该给
        if now - _last_sticker.get(chat_id, 0.0) < 10.0:
            return None
        want_account = cfg.ai_account_face_enabled and (
            account_face.is_face_request(text) or cfg.ai_account_face_on_request
        )
        if want_account:
            seg = await _account_face_segment(bot)
        if seg is None:
            path = stickers.pick_sticker(directory)
            seg = _sticker_segment(path) if path else None
    else:
        # 语义匹配 / 随机附带：60 秒冷却，防止「好的」这种词把表情刷屏
        if now - _last_sticker.get(chat_id, 0.0) < 60.0:
            return None
        path = stickers.match_sticker(text, directory)
        if path is not None:
            seg = _sticker_segment(path)
        elif (
            cfg.ai_sticker_probability > 0
            and random.random() < cfg.ai_sticker_probability
        ):
            # 随机附带：账号收藏和本地按 mix 概率混发
            if cfg.ai_account_face_enabled and random.random() < cfg.ai_account_face_mix:
                seg = await _account_face_segment(bot)
            if seg is None:
                path = stickers.pick_sticker(directory)
                seg = _sticker_segment(path) if path else None
    if seg is not None:
        _last_sticker[chat_id] = now
    return seg


async def _account_face_segment(bot: Bot) -> MessageSegment | None:
    """拉一次账号收藏表情并随机挑一个，失败返回 None（调用方退回本地）。"""
    if not cfg.ai_account_face_enabled:
        return None
    url = await account_face.random_face(bot, count=cfg.ai_account_face_count)
    if url is None:
        return None
    return account_face.face_segment(url)


def _sticker_segment(path: Path) -> MessageSegment | None:
    """把本地表情图转成消息段。

    必须走 base64 而不是文件路径：NapCat 的 QQ 跑在 macOS 沙盒容器里，
    直接传 file:// URI 会被沙盒拦下（retcode 1200 EPERM，
    日志报 "operation not permitted"）。base64 把图片内容塞进消息体，
    不依赖 NapCat 读本地文件。代价是每条消息多 100~250KB 流量，可接受。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning(f"[ai_chat] 表情文件读取失败，本次跳过: {exc!r}")
        return None
    if not raw:
        return None
    return MessageSegment.image("base64://" + base64.b64encode(raw).decode())


def _demote_image_content(history) -> None:
    """把历史里还带着图片 content 数组的消息降级成纯文本占位。

    历史里只保留最近一轮的图片原内容：QQ 图床 URL 会过期，且每轮都把
    图片重发一遍的话 token 和延迟都会炸。在新消息入历史前调用。
    """
    for msg in history:
        if isinstance(msg.get("content"), list):
            msg["content"] = vision.content_to_text(msg["content"])


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


# ---------------------------------------------------------------- 思考过程清洗
# 偶发故障（实测 MiMo v2.6-flash 挂联网工具时）：模型把内心独白写进正文，
# 形态是开头的「【搜索结果分析】…所以答案是…」过程叙述，加上结尾的
# 「我需要用人设的语气回答…」元话语。铁律第 12 条只能降低概率，拦不住
# 偶发，这里按空行分段兜底：从【…分析…】标题起剥掉过程段，直到元话语
# 段收尾为止，之后的内容才是真正的回复，原样保留。
_THOUGHT_TITLE = re.compile(
    r"^【[^】]{0,20}(?:分析|思考|搜索|检索|推理|内心|心路|结论|整理|理解)[^】]*】"
)
_META_TALK = re.compile(
    r"我需要.{0,50}(?:语气|人设|回答|回复|解释)"
    r"|我应当.{0,50}(?:语气|人设|回答|回复|解释)"
    r"|用人设的语气"
    r"|用.{0,8}的语气(?:来)?回答"
    r"|让我.{0,20}(?:回答|回复|组织一下语言)"
    r"|先想一下"
)


def _strip_inner_monologue(reply: str) -> str:
    """剥掉模型偶尔漏出来的思考过程，只留真正的回复。

    返回空串表示整条都是思考、没有正文——交给上层的空正文分支处理。
    """
    if not reply:
        return reply
    paragraphs = re.split(r"\n\s*\n", reply.strip())
    if _THOUGHT_TITLE.match(paragraphs[0].strip()) is None:
        # 开头不是思考标题，认为没有泄漏，原文返回（正常路径零损耗）
        return reply.strip()
    logger.info(f"[ai_chat] 检测到思考过程泄漏，开始清洗: {paragraphs[0][:60]}")
    kept: list[str] = []
    in_thought = True
    for para in paragraphs:
        body = para.strip()
        if in_thought:
            if _META_TALK.search(body):
                # 元话语段是思考区的收尾；剥掉它，之后的内容就是正文
                in_thought = False
                logger.info(f"[ai_chat] 思考过程收尾于: {body[:60]}")
            continue
        if body:
            kept.append(body)
    return "\n\n".join(kept).strip()


chat = on_message(rule=Rule(_chat_rule), priority=50, block=False)


def _imagegen_rule(bot: Bot, event: Event) -> bool:
    """@ 机器人 + 生图意图，且生图功能可用（开了且有 Key）。

    两种意图：
    - 显式：「画一张/画个/画一个…」触发词
    - 隐式：没说「画」字，但明显在要求看机器人某个样子，
      如「你卖萌是什么样子」「你生气的样子」「你长什么样」
    """
    text = event.get_plaintext()
    return (
        cfg.ai_imagegen_enabled
        and bool(cfg.ai_imagegen_api_key)
        and tools.at_me(event, bot.self_id)
        and (
            imagegen.is_imagegen_request(text)
            or imagegen.is_implicit_imagegen_request(text)
        )
    )


# priority=5 + block=True：比对话匹配器（50）先拦下，
# 否则机器人会先回一句「好我画」再画图，一句话变两句话
imagegen_cmd = on_message(rule=Rule(_imagegen_rule), priority=5, block=True)


@imagegen_cmd.handle()
async def handle_imagegen(bot: Bot, event: GroupMessageEvent) -> None:
    """生图指令：显式「@机器人 画一张X」或隐式「你卖萌是什么样子」。"""
    text = event.get_plaintext().strip()
    # 「画一张小猫」→ 话题「小猫」；没说话题就用整句
    topic = text
    for word in imagegen.IMAGEGEN_TRIGGERS:
        if word in topic:
            topic = topic.split(word, 1)[1]
            break
    topic = topic.strip(" ，。！？~～的啊呀") or text
    # 隐式意图（「你卖萌是什么样子」）没有触发词可切，topic 就是原句，
    # 交给模型理解；且必然是角色模式（画机器人自己）。
    implicit = not imagegen.is_imagegen_request(text)
    await imagegen_cmd.send("好，你看～" if implicit else "等着，我画一张～")
    ok = await imagegen.generate_and_send(
        bot, event, topic, mode="character" if implicit else "auto"
    )
    if not ok:
        await imagegen_cmd.finish("……这张画不出来，换个话题试试")


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

    # 引用还原：用户「回复某条消息再 @机器人」时，被引用的内容必须拼进正文，
    # 否则 render_text 会把 reply 段整个丢掉，「他说得对吗」就断了上下文
    quote = tools.reply_quote_text(event)
    if quote:
        text = f"{quote}\n{text}" if text else quote

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

    # 意图路由：@机器人说人话也能触发功能（/命令仍可用）。
    # 放在聊天额度扣减之前——功能动作不该消耗聊天额度；
    # 预筛不命中的消息在这里零开销直接落回下面的聊天流程，
    # 路由判定为「聊天」时也会落回去，不会吞消息。
    if text and await intent.try_handle(bot, event, text, _is_bot_admin(bot, event)):
        return

    _roll_day_if_needed()

    if _usage[group_id] >= cfg.ai_daily_limit:
        _last_call[group_id] = now
        await chat.finish("今天的对话额度已经用完啦，明天再来找我～")

    _last_call[group_id] = now
    _usage[group_id] += 1

    # 把当天日期喂给模型。不喂的话它根本不知道「今天」是哪天 ——
    # 联网时尤其明显：问「今天天气」，它会把日期算错一天。
    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    messages: list[dict] = [
        {
            "role": "system",
            "content": f"今天的日期是 {today.isoformat()}（星期{weekday}）。"
            "涉及时间的问题一律以此为准。\n\n" + _persona_for(group_id),
        }
    ]

    # 是否允许联网：关键词命中（或配置 always）。提前算——client 模式要靠它
    # 决定要不要在注入上下文前先跑一轮检索。
    allow_search = cfg.ai_web_search_enabled and (
        cfg.ai_web_search_trigger == "always"
        or websearch.looks_like_web_query(text)
    )

    context = await _build_context(bot, group_id, text, at_qqs, event.self_id)
    if context:
        messages.append({"role": "system", "content": context})

    # 联网检索（client 模式）：调 DeepSeek Search API 拿到结果，格式化成
    # 「联网检索结果」注入。放在群资源 context 之后、历史之前——与检索资料
    # 同级的参考资料地位。server 模式（MiMo）不在这里检索，工具由模型侧触发。
    search_ctx = ""
    if allow_search and cfg.ai_web_search_mode == "client":
        if websearch.exhausted(cfg.ai_web_search_daily_limit):
            _warn_search_budget_exhausted()
        else:
            results = await websearch.search_web(
                text,
                count=cfg.ai_web_search_count,
                api_key=cfg.ai_api_key,
                api_base=cfg.ai_api_base,
            )
            if results:
                search_ctx = websearch.format_results(results, text)
                messages.append({"role": "system", "content": search_ctx})
                logger.info(
                    f"[ai_chat] 联网检索 {len(results)} 条，"
                    f"今日累计 {websearch.rounds()} 轮"
                )

    # 长期记忆注入：/记住 存下的事实，按与本条消息的相关性挑出来给模型。
    # 放在检索结果之后、历史之前——记忆是「明确交代过的事」，优先级应高于闲聊历史。
    if cfg.ai_memory_enabled:
        mem_ctx = memory.describe(group_id, text, cfg.ai_memory_max_results)
        if mem_ctx:
            messages.append({"role": "system", "content": mem_ctx})

    # 群聊滚动摘要注入：让机器人知道「群里最近在聊什么」，接得上梗。
    # 放在记忆之后、历史之前——摘要和记忆同级，都是背景知识，
    # 且都让位于本轮真正的检索结果（那些排在更前面）。
    if cfg.ai_digest_enabled:
        digest_ctx = digest.current(group_id)
        if digest_ctx:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "【群聊近况】以下是这个群最近在聊的内容的摘要，"
                        "仅供你理解上下文、接得住话题。它不是本轮问题的答案，"
                        "回答时要以本轮的检索结果和对方的问题为准，"
                        "不要编造摘要里没有细节，也不要主动复述摘要：\n"
                        f"{digest_ctx}"
                    ),
                }
            )

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

    _t0 = time.time()
    try:
        resp = await _complete_with_reject_retry(
            bot,
            messages,
            text,
            images,
            allow_search=allow_search,
            drop_contents=[c for c in (context, search_ctx) if c],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调用模型失败: {exc!r}")
        await chat.finish("模型那边好像出问题了，稍后再试试～")

    if resp is None:
        # 风控重试全失败：不把英文拒绝文案丢群里，也不写进历史污染上下文
        await chat.finish("这条触发了上游内容风控，我答不了，换个问法试试～")

    reply = (resp.choices[0].message.content or "").strip()
    # 兜底清洗：偶发的思考过程泄漏（见 _strip_inner_monologue 注释）
    cleaned = _strip_inner_monologue(reply)
    if cleaned != reply:
        logger.info(f"[ai_chat] 思考过程已清洗: {reply[:80]!r} -> {cleaned[:80]!r}")
    reply = cleaned
    if not reply:
        logger.warning(
            "[ai_chat] 模型返回空正文，或整条都是思考过程被清洗掉了。"
            f"当前 max_tokens={cfg.ai_max_tokens}, "
            f"disable_thinking={cfg.ai_disable_thinking}, "
            f"reasoning_effort={cfg.ai_reasoning_effort!r}, usage={resp.usage}"
        )
        await chat.finish("模型光顾着思考没写出正文，稍后再试试～")

    # 记录回复内容（截断）：排查「机器人到底回了什么」「回复多慢」全靠它
    logger.info(f"[ai_chat] 回复({len(reply)}字, {time.time() - _t0:.1f}s): {reply[:200]}")

    # 历史里只留文字 + 最近一轮的图片原内容：带图的 content 数组直接入历史，
    # 下一轮先把更早的降级成 [图片] 占位（见 _demote_image_content），
    # 这样「这是什么猫→那多少钱一只」的多轮看图追问不会断档。
    # 每条历史都带【发言人】标记，模型才能把「谁说过什么」记在正确的人名下。
    _demote_image_content(_history[group_id])
    _history[group_id].append({"role": "user", "content": user_content})
    _history[group_id].append({"role": "assistant", "content": reply})

    # @ 机器人的这条消息也是群聊内容，记进摘要流水（没 @ 的消息由
    # proactive 那边记，两边合起来就是完整的群聊记录）。
    if cfg.ai_digest_enabled:
        digest.observe(group_id, speaker, text, time.time())
        # 顺手检查是否该压缩了：到期就起后台任务，不拖慢这条回复
        digest.maybe_compress(group_id)

    segments: list = []
    if cfg.ai_reply_quote:
        segments.append(MessageSegment.reply(event.message_id))
    # 链接转卡片：回复里的 B 站/网页链接自动变成分享卡片，失败退回纯文本
    segments.extend(await crawler.linkify(reply, proxy=cfg.ai_crawler_youtube_proxy))

    sticker = await _maybe_sticker(bot, text, group_id)
    if sticker is not None:
        segments.append(sticker)

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
    "· @我 + 说人话 —— 我自己听懂你想干嘛：绑定文档、建文档、找图、\n"
    "  翻收藏链接、记住/忘记事情、换人设……不用背任何命令\n"
    "· @我 + 图片 + 说「存相册」—— 收进群相册\n"
    "· @我 + 说「表情包」—— 发张表情；说「收藏表情」发我账号里收藏的\n"
    "· @我 + 来首 XXX —— 点歌分享\n"
    "· /文件 XXX —— 查群文件在哪；/文档 XXX —— 查文件内容写了什么\n"
    "· /精华 XXX —— 翻群精华；/历史 XXX —— 查刚才谁说过什么\n"
    "· /成员 XXX —— 查群成员资料\n"
    "· /记住 XXX —— 让我长期记住一件事；/记忆 —— 看我记住了什么\n"
    "· /找图 XXX —— 按内容找群相册里的图；存图时我会自动记住图里有什么\n"
    "· /链接 [关键词] —— 翻本群收藏过的链接（群里发链接我自动收）\n"
    "· /绑定文档 —— 看在线文档绑定状态；/绑定文档 链接 / /创建文档 标题（管理员）\n"
    "· /文档过滤 —— 链接自动收录进文档的规则：模式(全部/白名单/手动)和白名单可改（管理员）\n"
    "· /收录 关键词 —— 把收藏库里的链接手动写进在线文档（管理员）\n"
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
        current = _persona_for(event.group_id)
        preview = current[:100] + ("…" if len(current) > 100 else "")
        await persona_cmd.finish(
            f"当前人设：{src}" + (f"（{length} 字）" if length else "") + f"\n开头是：{preview}\n"
            "管理员可发 /人设 新人设文本 换人设；/人设 恢复 回默认。"
            "注意：换人设只换性格，资源检索的铁律会自动保留。"
        )

    if not cfg.ai_persona_override_enabled:
        await persona_cmd.finish("人设覆盖功能当前是关闭的（AI_PERSONA_OVERRIDE_ENABLED=false）")
    if not _is_bot_admin(bot, event):
        await persona_cmd.finish("只有群主或管理员可以换人设")

    if text in ("恢复", "重置", "默认", "还原"):
        _set_persona(event.group_id, "")
        await persona_cmd.finish("已恢复默认人设（吉普莉尔）")

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


# 人设的执行器在 __init__（人设存储函数在这里），注册给意图路由供
# 「@机器人 换个人设：…」这种自然语言说法使用。
async def _intent_set_persona(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "想把我换成什么性格？描述清楚一点，比如「毒舌但心软的猫娘」"
    if not cfg.ai_persona_override_enabled:
        return "人设覆盖功能当前是关闭的（AI_PERSONA_OVERRIDE_ENABLED=false）"
    if text in ("恢复", "重置", "默认", "还原"):
        _set_persona(event.group_id, "")
        return "已恢复默认人设（吉普莉尔）"
    if len(text) < cfg.ai_persona_min_len:
        return (
            f"新人设太短了，至少 {cfg.ai_persona_min_len} 字——"
            "把性格、说话方式描述清楚，效果才稳定"
        )
    if len(text) > 2000:
        return "人设最长 2000 字，精简一点反而效果更好"
    _set_persona(event.group_id, text)
    return (
        f"本群人设已更新（{len(text)} 字），立刻生效。\n"
        "资源检索铁律已自动附加，我不会因此开始编造文件名～"
    )


async def _intent_persona_status(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str:
    src, length = _persona_source(event.group_id)
    current = _persona_for(event.group_id)
    preview = current[:100] + ("…" if len(current) > 100 else "")
    return (
        f"当前人设：{src}" + (f"（{length} 字）" if length else "") + f"\n开头是：{preview}\n"
        "对我说「换个人设：…」即可更换，说「恢复默认人设」还原（管理员）。"
    )


intent.EXTRA_ACTIONS["set_persona"] = _intent_set_persona
intent.EXTRA_SPECS["set_persona"] = (
    "更换机器人的性格人设",
    "text（人设描述，恢复默认时传「恢复」）",
    True,
)
intent.EXTRA_ACTIONS["persona_status"] = _intent_persona_status
intent.EXTRA_SPECS["persona_status"] = ("查看当前人设", "无参数", False)


# ---------------------------------------------------------------- 新成员入群欢迎

notice = on_notice(priority=50, block=False)


@notice.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    """有人入群时，用吉普莉尔的口吻欢迎一句。

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

    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    date_line = f"今天的日期是 {today.isoformat()}（星期{weekday}）。"

    reply = ""
    try:
        resp = await _create_completion(
            welcome.build_messages(date_line, cfg.ai_system_prompt, name, user_id),
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

private_chat = on_message(rule=to_me(), priority=50, block=False)


@private_chat.handle()
async def handle_private_chat(bot: Bot, event: PrivateMessageEvent) -> None:
    """私聊也走吉普莉尔对话。

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
    at_qqs = []  # 私聊里 @ 没有意义
    text = members.render_text(message, {}, event.self_id)
    quote = tools.reply_quote_text(event)
    if quote:
        text = f"{quote}\n{text}" if text else quote

    images = (
        await vision.collect_images(bot, event, limit=cfg.ai_vision_max_images)
        if cfg.ai_vision_enabled
        else []
    )
    if not text and not images:
        return

    _private_last[user_id] = now
    _private_usage[user_id] += 1

    today = date.today()
    weekday = "一二三四五六日"[today.weekday()]
    messages: list[dict] = [
        {
            "role": "system",
            "content": f"今天的日期是 {today.isoformat()}（星期{weekday}）。"
            "涉及时间的问题一律以此为准。\n\n" + cfg.ai_system_prompt,
        }
    ]
    messages.extend(_private_history[user_id])

    # 联网检索（client 模式）：私聊没有群资源 context，但联网照常可用
    allow_search = cfg.ai_web_search_enabled and (
        cfg.ai_web_search_trigger == "always"
        or websearch.looks_like_web_query(text)
    )
    search_ctx = ""
    if allow_search and cfg.ai_web_search_mode == "client":
        if websearch.exhausted(cfg.ai_web_search_daily_limit):
            _warn_search_budget_exhausted()
        else:
            results = await websearch.search_web(
                text,
                count=cfg.ai_web_search_count,
                api_key=cfg.ai_api_key,
                api_base=cfg.ai_api_base,
            )
            if results:
                search_ctx = websearch.format_results(results, text)
                messages.append({"role": "system", "content": search_ctx})
                logger.info(
                    f"[ai_chat] 私聊联网检索 {len(results)} 条，"
                    f"今日累计 {websearch.rounds()} 轮"
                )

    user_content = await vision.build_content(
        bot,
        text,
        images,
        cfg.ai_vision_default_prompt,
        mode=cfg.ai_vision_image_mode,
    )
    messages.append({"role": "user", "content": user_content})

    _t0 = time.time()
    try:
        resp = await _complete_with_reject_retry(
            bot,
            messages,
            text,
            images,
            allow_search=allow_search,
            drop_contents=[c for c in (search_ctx,) if c],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 私聊调用模型失败: {exc!r}")
        await private_chat.finish("模型那边好像出问题了，稍后再试试～")

    if resp is None:
        await private_chat.finish("这条触发了上游内容风控，我答不了，换个问法试试～")

    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        await private_chat.finish("模型光顾着思考没写出正文，把 AI_MAX_TOKENS 调大一点试试")

    _demote_image_content(_private_history[user_id])
    _private_history[user_id].append({"role": "user", "content": user_content})
    _private_history[user_id].append({"role": "assistant", "content": reply})
    logger.info(f"[ai_chat] 私聊回复({len(reply)}字, {time.time() - _t0:.1f}s): {reply[:200]}")

    segments: list = []
    if cfg.ai_reply_quote:
        segments.append(MessageSegment.reply(event.message_id))
    segments.append(MessageSegment.text(reply))

    sticker = await _maybe_sticker(bot, text, -event.user_id)
    if sticker is not None:
        segments.append(sticker)

    await private_chat.finish(Message(segments))
