"""群成员识别：把「@某人」和「说到某人的名字」翻译成模型能懂的信息。

解决两个问题：

1. **消息里的 @ 在模型看来是隐形的。**
   ``event.get_plaintext()`` 只取 text 消息段，``at`` 段被整个丢掉。
   所以「@机器人 @小a 他最近怎么样」到模型眼里只剩「他最近怎么样」，
   完全不知道说的是谁。

2. **直接打名字时，模型不知道那是群成员。**
   「小a怎么样」里的「小a」在模型眼里只是两个普通汉字，
   它只能瞎猜，或者反问「小a是谁」。

做法：

- @ 段 → 查群名片/昵称，还原成「@小a」再交给模型
- 群成员名单 → 本地建索引（带 TTL 缓存），在消息里做名字匹配，
  **只有命中的成员**才把资料拼进提示词

为什么只拼命中的：一个群几百人，全塞进去 token 直接爆，
而且模型看到一长串无关名单反而更容易张冠李戴。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import Bot, Message
from nonebot.log import logger

from .tools import speaker_tag

# 成员名单缓存时长（秒）。群成员变动不频繁，可以设长一点。
# 代价是新人入群后最多半小时内认不出来，属可接受范围。
CACHE_TTL = 1800

# 参与文本匹配的名字长度范围。
# 下限 2：单字昵称（如「A」「鱼」）误伤率太高，一句话里到处都是，
#         宁可认不出来也不要乱认人。
# 上限 16：超长昵称基本是整句话，不可能是称呼。
MIN_NAME_LEN = 2
MAX_NAME_LEN = 16


@dataclass
class Member:
    qq: int
    nickname: str = ""
    card: str = ""
    role: str = ""
    level: str = ""
    title: str = ""
    join_time: int = 0

    @property
    def names(self) -> list[str]:
        """可以用来称呼这个人的所有名字，群名片优先。"""
        out: list[str] = []
        for name in (self.card, self.nickname):
            name = (name or "").strip()
            if name and name not in out:
                out.append(name)
        return out

    @property
    def display(self) -> str:
        return self.names[0] if self.names else str(self.qq)

    @property
    def role_cn(self) -> str:
        return {"owner": "群主", "admin": "管理员"}.get(self.role, "群成员")

    @property
    def matchable(self) -> list[str]:
        """适合拿去做文本匹配的名字。"""
        return [n for n in self.names if MIN_NAME_LEN <= len(n) <= MAX_NAME_LEN]


@dataclass
class MemberIndex:
    group_id: int
    members: list[Member] = field(default_factory=list)
    built_at: float = 0.0
    error: str = ""

    def by_qq(self, qq: int) -> Member | None:
        for member in self.members:
            if member.qq == qq:
                return member
        return None


_cache: dict[int, MemberIndex] = {}


def _pick(data: dict, *keys, default=""):
    """按顺序取第一个存在且非空的键。协议端不同版本字段名可能不一样。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _to_member(raw: dict) -> Member | None:
    if not isinstance(raw, dict):
        return None
    try:
        qq = int(_pick(raw, "user_id", "uin", "qq", default=0))
    except (TypeError, ValueError):
        return None
    if qq <= 0:
        return None
    try:
        join_time = int(_pick(raw, "join_time", default=0) or 0)
    except (TypeError, ValueError):
        join_time = 0
    return Member(
        qq=qq,
        nickname=str(_pick(raw, "nickname", "nick", default="")),
        card=str(_pick(raw, "card", "card_name", default="")),
        role=str(_pick(raw, "role", default="")),
        level=str(_pick(raw, "level", default="")),
        title=str(_pick(raw, "title", default="")),
        join_time=join_time,
    )


async def build_index(bot: Bot, group_id: int) -> MemberIndex:
    """拉取群成员名单。失败不抛异常，把原因写进 index.error。"""
    started = time.perf_counter()
    try:
        raw = await bot.call_api("get_group_member_list", group_id=group_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 读取群成员名单失败: {exc!r}")
        return MemberIndex(group_id=group_id, error=str(exc))

    members: list[Member] = []
    for item in raw or []:
        member = _to_member(item)
        if member is not None:
            members.append(member)

    cost = time.perf_counter() - started
    logger.info(f"[ai_chat] 群 {group_id} 成员名单已加载：{len(members)} 人（{cost:.2f}s）")
    return MemberIndex(group_id=group_id, members=members, built_at=time.time())


async def get_index(
    bot: Bot, group_id: int, force: bool = False, ttl: float | None = None
) -> MemberIndex:
    """取成员索引，优先用缓存。ttl 由调用方从配置传入。"""
    cached = _cache.get(group_id)
    life = CACHE_TTL if ttl is None else ttl
    if not force and cached and time.time() - cached.built_at < life:
        return cached
    index = await build_index(bot, group_id)
    _cache[group_id] = index
    return index


def invalidate(group_id: int) -> None:
    """群成员有变动时让缓存作废，下次用到会自动重建。"""
    _cache.pop(group_id, None)


async def fetch_member_name(
    bot: Bot, group_id: int, qq: int, retries: int = 3, delay: float = 0.6
) -> str:
    """查某个成员的群名片/昵称。

    为什么要重试：入群通知到达时，成员信息在协议端可能还没建好，
    第一次查经常是空的。实在查不到就返回 QQ 号，宁可叫号码也别不吭声。
    """
    for attempt in range(retries):
        try:
            raw = await bot.call_api(
                "get_group_member_info", group_id=group_id, user_id=qq
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_chat] 查询成员 {qq} 失败（第 {attempt + 1} 次）: {exc!r}")
            raw = None
        member = _to_member(raw if isinstance(raw, dict) else {})
        if member is not None and member.names:
            return member.display
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return str(qq)


# ---------------------------------------------------------------- @ 还原


def extract_ats(message: Message, self_id: int) -> list[int]:
    """取出消息里 @ 到的其他人（不含机器人自己、不含 @全体成员）。"""
    out: list[int] = []
    for seg in message:
        if seg.type != "at":
            continue
        raw = str(seg.data.get("qq", ""))
        if raw in ("all", "") or raw == str(self_id):
            continue
        try:
            qq = int(raw)
        except ValueError:
            continue
        if qq not in out:
            out.append(qq)
    return out


async def resolve_ats(bot: Bot, group_id: int, qqs: list[int]) -> dict[int, str]:
    """把 @ 的 QQ 号翻译成名字。优先查索引，查不到再单独问一次接口。"""
    if not qqs:
        return {}
    names: dict[int, str] = {}

    index = await get_index(bot, group_id)
    for qq in qqs:
        member = index.by_qq(qq)
        if member is not None:
            # 带 QQ 锚点的规范名：和聊天历史的【发言人】标记同款格式，
            # 模型才能把「@某人」和「某人说过的话」对上号
            names[qq] = speaker_tag(member.card, member.nickname, qq)

    # 索引里没有的（比如刚入群还没刷新缓存）单独查
    missing = [qq for qq in qqs if qq not in names]
    for qq in missing:
        try:
            raw = await bot.call_api(
                "get_group_member_info", group_id=group_id, user_id=qq
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_chat] 查询成员 {qq} 失败: {exc!r}")
            continue
        member = _to_member(raw if isinstance(raw, dict) else {})
        if member is not None:
            names[qq] = speaker_tag(member.card, member.nickname, qq)
    return names


def render_text(message: Message, at_names: dict[int, str], self_id: int) -> str:
    """把消息拼成模型能读的文本。

    和 ``get_plaintext()`` 的区别：@ 段不再被丢掉，而是还原成「@小a」；
    对机器人自己的 @ 直接去掉（那是触发用的，不算内容）。
    """
    parts: list[str] = []
    for seg in message:
        if seg.type == "text":
            parts.append(str(seg.data.get("text", "")))
        elif seg.type == "at":
            raw = str(seg.data.get("qq", ""))
            if raw == "all":
                parts.append("@全体成员 ")
            elif raw == str(self_id) or raw == "":
                continue
            else:
                try:
                    qq = int(raw)
                except ValueError:
                    continue
                # 名字后面补个空格，避免「@小a他最近怎么样」黏成一团
                parts.append(f"@{at_names.get(qq, raw)} ")
    return "".join(parts).strip()


# ---------------------------------------------------------------- 文本匹配


def _find_spans(text: str, name: str) -> list[tuple[int, int]]:
    """找出 name 在 text 里的所有出现区间。"""
    spans: list[tuple[int, int]] = []
    start = text.find(name)
    while start != -1:
        spans.append((start, start + len(name)))
        start = text.find(name, start + 1)
    return spans


def _overlaps(span: tuple[int, int], others: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < o_end and o_start < end for o_start, o_end in others)


def find_mentioned(
    index: MemberIndex,
    text: str,
    extra_qqs: list[int] | tuple[int, ...] = (),
    exclude_qqs: list[int] | tuple[int, ...] = (),
    limit: int = 5,
) -> list[Member]:
    """找出这条消息里被提到的人。

    两种来源：消息里 @ 到的（extra_qqs），以及正文里直接打了名字的。
    名字长的优先——「小a」和「小a的猫」同时是群成员时，说「小a的猫可爱吗」
    应该只认出「小a的猫」，而不是把两个人都算上。
    """
    if not text and not extra_qqs:
        return []

    exclude = set(exclude_qqs)
    hits: dict[int, int] = {}  # qq -> 命中名字的长度，用来排序

    # @ 到的优先级最高。给一个不可能被正文匹配超越的长度。
    for qq in extra_qqs:
        member = index.by_qq(qq)
        if member is not None and member.qq not in exclude:
            hits[member.qq] = MAX_NAME_LEN

    # 每个成员取自己最长的一个命中名字
    matched: dict[int, str] = {}
    for member in index.members:
        if member.qq in exclude:
            continue
        best = ""
        for name in member.matchable:
            if len(name) > len(best) and name in text:
                best = name
        if best:
            matched[member.qq] = best

    # 长名字先占位。短名字如果只剩「被长名字包住」的出现位置，就丢弃：
    # 「小a的猫可爱吗」里的「小a」其实只是「小a的猫」的一部分，不是单独提到的人。
    # 但「小a和小a的猫」这种两处都真的出现了的情况要保留 —— 所以判的是
    # 「有没有一段没被覆盖的出现位置」，而不是简单地比长度。
    covered: list[tuple[int, int]] = []
    for qq, name in sorted(matched.items(), key=lambda kv: -len(kv[1])):
        spans = _find_spans(text, name)
        if not any(not _overlaps(span, covered) for span in spans):
            continue
        covered.extend(spans)
        hits[qq] = max(hits.get(qq, 0), len(name))

    ordered = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[Member] = []
    for qq, _ in ordered[:limit]:
        member = index.by_qq(qq)
        if member is not None:
            out.append(member)
    return out


def describe(members: list[Member]) -> str:
    """拼成给模型看的群成员资料。"""
    if not members:
        return ""
    lines = [
        "【群成员识别结果】对方这条消息里提到了以下群成员，"
        "系统已从群名单里查到他们的真实资料："
    ]
    for member in members:
        bits = [f"群名片：{member.card}"] if member.card else []
        if member.nickname and member.nickname != member.card:
            bits.append(f"昵称：{member.nickname}")
        bits.append(f"QQ：{member.qq}")
        bits.append(f"群身份：{member.role_cn}")
        if member.level:
            bits.append(f"群等级：{member.level}")
        if member.title:
            bits.append(f"专属头衔：{member.title}")
        lines.append(f"- 「{member.display}」（{'｜'.join(bits)}）")

    lines.append(
        "回答要求：上面是系统查到的真实成员资料，涉及这些人的群身份、昵称、"
        "头衔时只能依据它，不要编造。至于他们说过什么、做过什么、和谁关系好，"
        "上面没有的就说不知道，别自己想象。"
    )
    return "\n".join(lines)


def render_all(members: list[Member], limit: int = 40) -> str:
    """给 /成员 命令用：列出成员。"""
    if not members:
        return "没有找到匹配的群成员"
    lines = []
    for member in members[:limit]:
        bits = [member.role_cn]
        if member.level:
            bits.append(f"等级 {member.level}")
        if member.title:
            bits.append(member.title)
        lines.append(f"　{member.display}（{member.qq}｜{'｜'.join(bits)}）")
    if len(members) > limit:
        lines.append(f"　…… 共 {len(members)} 人，只显示前 {limit} 个")
    return "\n".join(lines)


def search(index: MemberIndex, query: str, limit: int = 20) -> list[Member]:
    """按名字模糊找成员，供 /成员 命令使用。"""
    query = query.strip()
    if not query:
        return index.members[:limit]
    low = query.lower()
    exact: list[Member] = []
    fuzzy: list[Member] = []
    for member in index.members:
        names = [n.lower() for n in member.names]
        if str(member.qq) == query:
            exact.append(member)
        elif any(low == n for n in names):
            exact.append(member)
        elif any(low in n for n in names):
            fuzzy.append(member)
    return (exact + fuzzy)[:limit]
