"""群精华消息：抓取 + 本地检索。

思路和 resource_index 一致：模型翻不了群精华，能翻的是协议端接口。
所以先抓下来、在本地做关键词匹配，再把命中的几条交给模型组织语言。

用到的接口：
    get_essence_msg_list    （NapCat「群组接口」，无分页参数，一次给全）

关于 content 字段的类型，这里做了防御式处理：
    NapCat 的 OpenAPI 规范里写的是 array<string>，
    但官方示例里给的是普通 string（"content": "精华内容"）。
    实际返回值随版本不同而不同，还可能是消息段数组，
    所以三种形态都得能接住 —— 接不住就会整个功能静默失效。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

# 复用语料归一化与最长子串匹配，避免和群文件检索各写一套
from .resource_index import _longest_hit, _normalize

# 索引缓存时长（秒）
CACHE_TTL = 600

# 单条精华消息最多交给模型多少字，防止一条超长聊天记录把上下文挤爆
MAX_TEXT_CHARS = 200

# 出现这些词就认为用户在问精华消息
ESSENCE_KEYWORDS = (
    "精华", "加精", "群精华", "精华消息", "精华内容",
    "置顶消息", "置顶的", "精选", "被加精",
)


def looks_like_essence_query(text: str) -> bool:
    return any(word in text for word in ESSENCE_KEYWORDS)


# ---------------------------------------------------------------- 内容归一化

# CQ 码：把 [CQ:image,file=xxx,url=yyy] 这类标记换成人类可读的占位符
_CQ_RE = re.compile(r"\[CQ:([A-Za-z_]+)((?:,[^\]]*)?)\]")

_CQ_LABEL = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "emoji": "[表情]",
    "video": "[视频]",
    "record": "[语音]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "markdown": "[卡片]",
    "reply": "",
    "at": "@",
}

# CQ 码里的转义序列
_CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def _strip_cq(text: str) -> str:
    """把 CQ 码还原成人能读的样子，并压掉多余空白。"""
    out = _CQ_RE.sub(lambda m: _CQ_LABEL.get(m.group(1).lower(), ""), text)
    for src, dst in _CQ_UNESCAPE:
        out = out.replace(src, dst)
    return re.sub(r"\s+", " ", out).strip()


def _segment_to_text(item: object) -> str:
    """把单个消息段（或已经是字符串的片段）转成文本。"""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return ""
    kind = str(item.get("type") or "").lower()
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    if kind == "text":
        return str(data.get("text", ""))
    if kind == "at":
        return f"@{data.get('qq', '')}"
    if kind == "file":
        return f"[文件 {data.get('name') or data.get('file') or ''}]"
    return _CQ_LABEL.get(kind, "")


def content_to_text(content: object) -> str:
    """把精华消息的 content 字段归一化成纯文本。

    兼容三种形态：
        "精华内容"                          —— 普通字符串（可能是 CQ 码）
        ["段1", "段2"]                      —— 字符串数组
        [{"type": "text", "data": {...}}]   —— 消息段数组
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return _strip_cq(content)
    if isinstance(content, (list, tuple)):
        return _strip_cq("".join(_segment_to_text(item) for item in content))
    return _strip_cq(str(content))


def fmt_time(ts: int) -> str:
    """把 unix 时间戳转成「2026-03-09」。取不到就返回空串。"""
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------- 数据结构


@dataclass
class EssenceMsg:
    message_id: int = 0
    sender_id: int = 0
    sender_nick: str = ""
    operator_nick: str = ""
    operator_time: int = 0
    text: str = ""

    @property
    def when(self) -> str:
        return fmt_time(self.operator_time)

    @property
    def who(self) -> str:
        return self.sender_nick or (str(self.sender_id) if self.sender_id else "某位群友")


@dataclass
class EssenceIndex:
    group_id: int
    messages: list[EssenceMsg] = field(default_factory=list)
    built_at: float = 0.0
    error: str = ""
    # 接口本身可用、只是这个群一条精华都没有 —— 要和「接口报错」区分开，
    # 否则用户问起精华时会得到一句含糊的「查不到」。
    supported: bool = True

    def __len__(self) -> int:
        return len(self.messages)


_cache: dict[int, EssenceIndex] = {}


# ---------------------------------------------------------------- 抓取


def _parse_one(item: object) -> EssenceMsg | None:
    if not isinstance(item, dict):
        return None

    def num(*keys) -> int:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    def text_of(*keys) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    msg = EssenceMsg(
        message_id=num("message_id", "msg_id", "id"),
        sender_id=num("sender_id", "user_id"),
        sender_nick=text_of("sender_nick", "sender_name", "nickname"),
        operator_nick=text_of("operator_nick", "operator_name"),
        # 规范里叫 operator_time，但历史版本出现过 operation_time / operatorTime
        operator_time=num("operator_time", "operation_time", "operatorTime"),
        text=content_to_text(item.get("content")),
    )
    # 既没正文又没发送者，基本是脏数据
    if not msg.text and not msg.sender_id:
        return None
    return msg


async def _call(bot: Bot, action: str, **params):
    """调用 OneBot 接口。

    和 resource_index 里同样的坑：适配器可能已经把 data 拆出来了，
    也可能没拆，所以两种都要认。
    """
    raw = await bot.call_api(action, **params)
    if isinstance(raw, dict) and isinstance(raw.get("data"), (dict, list)):
        return raw["data"]
    return raw


async def build_index(bot: Bot, group_id: int) -> EssenceIndex:
    index = EssenceIndex(group_id=group_id, built_at=time.time())
    try:
        data = await _call(bot, "get_essence_msg_list", group_id=group_id)
    except Exception as exc:  # noqa: BLE001
        index.error = str(exc)
        index.supported = False
        logger.warning(f"[ai_chat] 读取群精华失败: {exc}")
        return index

    # 有的实现把数组包在 data.essence_list / data.messages 里
    if isinstance(data, dict):
        for key in ("essence_list", "messages", "list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = []

    if not isinstance(data, list):
        index.supported = False
        index.error = f"接口返回了意料之外的结构：{type(data).__name__}"
        return index

    for item in data:
        msg = _parse_one(item)
        if msg is not None:
            index.messages.append(msg)

    # 接口通了但返回空数组，说明这个群没设过精华 —— 不是错误
    logger.info(f"[ai_chat] 群 {group_id} 精华消息 {len(index.messages)} 条")
    return index


async def get_index(
    bot: Bot, group_id: int, force: bool = False, ttl: float | None = None
) -> EssenceIndex:
    """ttl 由调用方从配置传入；不传就用本模块默认值。"""
    cached = _cache.get(group_id)
    life = CACHE_TTL if ttl is None else ttl
    if not force and cached and time.time() - cached.built_at < life:
        return cached
    index = await build_index(bot, group_id)
    _cache[group_id] = index
    return index


# ---------------------------------------------------------------- 检索


def search(index: EssenceIndex, query: str, limit: int = 10) -> list[EssenceMsg]:
    """在精华消息里做模糊检索。

    评分：
        正文命中长度 × 4    —— 问「团建」命中正文里的「团建」最相关
        发送者昵称命中 × 2  —— 「张三说过什么」也能找
        整词命中 +10        —— 空格分词后精确命中
    """
    core = _normalize(query)
    if not core:
        return []

    terms = [t.lower() for t in re.split(r"[\s,，、。；;：:!！?？/\\|]+", query) if len(t) >= 2]

    scored: list[tuple[float, EssenceMsg]] = []
    for msg in index.messages:
        text = msg.text.lower()
        nick = msg.who.lower()
        score = 0.0

        hit = _longest_hit(core, text)
        if hit:
            score += hit * 4

        nick_hit = _longest_hit(core, nick)
        if nick_hit:
            score += nick_hit * 2

        for term in terms:
            if term in text:
                score += 10
            elif term in nick:
                score += 4

        if score > 0:
            scored.append((score, msg))

    scored.sort(key=lambda pair: (-pair[0], -pair[1].operator_time))
    return [msg for _, msg in scored[:limit]]


def describe(messages: list[EssenceMsg]) -> str:
    """整理成给模型看的文本。

    同样刻意写成「读起来像人话」的样子 —— 模型会把方括号标记原样抄进回复里。
    """
    lines = []
    for idx, msg in enumerate(messages, 1):
        body = msg.text or "（非文字内容）"
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS] + "…"
        # 用括号而不是逗号收尾 —— 正文本身常以「。」「，」结尾，
        # 再接一个逗号会变成「200。，2026-06-09被设为精华」这种怪句子。
        when = f"（{msg.when} 被设为精华）" if msg.when else ""
        lines.append(f"{idx}. {msg.who} 说：{body}{when}")
    return "\n".join(lines)


def render_all(messages: list[EssenceMsg], limit: int = 30) -> str:
    """给 /essence 命令直接展示用（不经过模型）。"""
    lines = []
    for idx, msg in enumerate(messages[:limit], 1):
        body = msg.text or "（非文字内容）"
        if len(body) > 60:
            body = body[:60] + "…"
        when = f"　{msg.when}" if msg.when else ""
        lines.append(f"{idx}. {msg.who}：{body}{when}")
    if len(messages) > limit:
        lines.append(f"…… 还有 {len(messages) - limit} 条")
    return "\n".join(lines)
