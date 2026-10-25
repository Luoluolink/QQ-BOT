"""意图路由：@机器人说人话 → 自动理解该执行哪个功能。

背景：/命令 形式对不熟悉机器人的群友有门槛。这一层让「@机器人 + 自然语言」
也能触发管理功能，比如：
    @bot 绑定这个文档到群里 https://docs.qq.com/doc/xxx
    @bot 帮我新建一篇叫「群链接收藏」的文档
    @bot 帮我找一下那张雪山的图
    @bot 记住张三的生日是1月1日
    @bot 之前发过的 github 链接都看看

设计（两道闸门，控制成本）：
    1. 正则预筛 looks_actionable()：消息里没有任何候选关键词就直接跳过，
       纯闲聊零额外开销（绝大多数 @ 都是聊天，不会多花钱）；
    2. 命中后用一次小参数模型调用做意图分类，要求只输出 JSON：
       {"action": "bind_doc", "args": {...}} 或 {"action": "chat"}。
       action 命中注册表 → 执行并直接回复；chat / 解析失败 → 返回 False，
       消息继续走正常聊天流程（宁可不省这一次调用，也不能吞掉消息）。

/命令 全部保留，两条路并存。权限规则与 /命令 一致：标注管理员的动作
群主/群管理员/SUPERUSERS 才能触发，普通群友触发会收到提示。
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message

from .ai_client import make_client
from . import album_index, docsync, essence, linkarchive, memory, resource_index
from .config import Config

cfg = get_plugin_config(Config)

# 路由用的 OpenAI 客户端（懒加载；测试可注入假客户端）
_client: Any = None

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_url(text: str) -> str:
    """从消息文本里抠第一条链接（末尾标点容易黏在 URL 上，顺手剥掉）。"""
    m = _URL_RE.search(text or "")
    if not m:
        return ""
    url = m.group(0).rstrip("。，,！!？?）)】》\"'")
    return url


def _sender_name(event: GroupMessageEvent) -> str:
    sender = getattr(event, "sender", None)
    return (
        getattr(sender, "card", "")
        or getattr(sender, "nickname", "")
        or "?"
    )

# ================================================================ 动作注册表

# 给路由模型看的功能说明。exec 是执行器签名：
#   async def exec(bot, event, args: dict, is_admin: bool) -> str | None
# 返回字符串 = 直接回复给群里；返回 None = 执行失败已打日志（回复兜底文案）；
# 抛 PermissionError = 没权限（统一回复）。
ACTIONS: dict[str, dict[str, Any]] = {}


def _action(name: str, desc: str, args_doc: str, admin: bool = False):
    def deco(fn: Callable[..., Awaitable[str | None]]) -> None:
        ACTIONS[name] = {
            "desc": desc,
            "args": args_doc,
            "admin": admin,
            "exec": fn,
        }
        return fn  # 保留原函数：测试和 try_handle 都可能直接引用

    return deco


# ================================================================ 执行器


@_action(
    "bind_doc",
    "把一篇腾讯在线文档绑定到本群，之后群里发的链接会自动按格式收录进文档",
    "url（docs.qq.com 的文档链接）",
    admin=True,
)
async def _exec_bind_doc(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    url = str(args.get("url") or "").strip()
    if not url:
        return "要绑定的文档链接呢？发我完整的 docs.qq.com 文档链接就行"
    try:
        info = await docsync.bind_doc(event.group_id, url)
    except docsync.DocsNotConfigured:
        return "腾讯文档 API 还没配置好（.env 需要 AI_DOCS_CLIENT_ID / AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID）"
    except Exception as exc:  # noqa: BLE001
        return f"绑定失败：{exc}"
    return f"已绑定 ✅ {info['url']}\n以后群里发的链接我会自动按文档现有格式收录进去"


@_action(
    "create_doc",
    "新建一篇腾讯在线文档并绑定到本群",
    "title（文档标题，可不填默认「群链接收藏」）",
    admin=True,
)
async def _exec_create_doc(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    title = str(args.get("title") or "").strip() or "群链接收藏"
    try:
        info = await docsync.create_doc(event.group_id, title)
    except docsync.DocsNotConfigured:
        return "腾讯文档 API 还没配置好（.env 需要 AI_DOCS_CLIENT_ID / AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID）"
    except Exception as exc:  # noqa: BLE001
        return f"创建失败：{exc}"
    return (
        f"已创建并绑定 ✅《{title}》\n{info['url']}\n"
        "记得把我的授权账号加为文档协作者，之后群里发的链接就会自动收录进去"
    )


@_action("doc_status", "查看本群绑定的在线文档和腾讯文档 API 配置状态", "无参数")
async def _exec_doc_status(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    return docsync._binding_text(event.group_id)


@_action(
    "read_doc",
    "读本群绑定的在线文档或表格的内容：看最近收录了什么，或按关键词在文档里找某条内容",
    "keyword（可选，要找的关键词；不填=看最近的）",
)
async def _exec_read_doc(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    kw = str(args.get("keyword") or "").strip()
    return await docsync.read_bound_text(event.group_id, kw)


@_action(
    "sync_to_doc",
    "把群里发过的某个链接手动收录进绑定的在线文档（过滤规则挡掉的链接用这个手动补收）",
    "keyword（链接本身或搜索关键词，可不填=最近一条）",
    admin=True,
)
async def _exec_sync_to_doc(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    gid = event.group_id
    target = str(args.get("keyword") or "").strip()
    entry: dict[str, Any] | None = None
    if target.startswith("http"):
        url = linkarchive.normalize_url(target)
        for e in reversed(linkarchive._load().get(str(gid), [])):
            if e["url"] == url:
                entry = e
                break
        if entry is None:
            title = await linkarchive.fetch_title(url) or url
            entry = {"url": url, "title": title, "by": "", "time": ""}
    else:
        hits = (
            linkarchive.search(gid, target, limit=1)
            if target
            else linkarchive.recent(gid, limit=1)
        )
        entry = hits[0] if hits else None
    if entry is None:
        return "没找到要收录的链接——告诉我链接地址，或者说个关键词让我在收藏库里找"
    status = await docsync.append_entry(gid, entry["title"], entry["url"], entry.get("by") or "手动收录")
    if status == "ok" or status.startswith("ok:"):
        line = status[3:] if status.startswith("ok:") else ""
        return f"已收录进在线文档 ✅《{entry['title'][:40]}》" + (f"（写在工作表{line}，在表格最底部）" if line else "")
    if status == "dup":
        return f"《{entry['title'][:40]}》已经在文档里了，不用重复收录"
    if status == "未绑定":
        return "本群还没绑定在线文档：先对我说「绑定文档」或「创建文档」"
    if status == "未配置":
        return "腾讯文档 API 还没配置好（.env 需要 AI_DOCS 三项凭证）"
    return f"收录失败：{status}"


@_action(
    "save_link",
    "把一条链接立即收录进本群绑定的在线文档（所有群成员都可以用，不受过滤规则限制）",
    "url（要收录的链接；参数没带时会从原消息里找）",
)
async def _exec_save_link(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    url = str(args.get("url") or "").strip()
    if not url:
        url = _extract_url(event.get_plaintext())
    if not url:
        return "要收录的链接呢？@我并贴上完整链接就行，比如「@我 https://...」"
    title = str(args.get("title") or "").strip()
    if not title:
        title = await linkarchive.fetch_title(url) or url
    status = await docsync.append_entry(event.group_id, title, url, _sender_name(event))
    short = title[:40]
    if status == "ok" or status.startswith("ok:"):
        line = status[3:] if status.startswith("ok:") else ""
        return f"已收录进在线文档 ✅《{short}》" + (f"（写在工作表{line}）" if line else "")
    if status == "dup":
        return f"《{short}》已经在文档里了，不用重复收录"
    if status == "未绑定":
        return "本群还没绑定在线文档——请群主或管理员先对我说「绑定文档 + 链接」或「创建文档」"
    if status == "未配置":
        return "腾讯文档 API 还没配置好（.env 需要 AI_DOCS 三项凭证）"
    return f"收录失败：{status}"


@_action(
    "set_doc_filter",
    "设置链接自动收录进在线文档的过滤规则：模式（all=全部/whitelist=白名单/manual=手动）或增删白名单域名",
    "mode（all/whitelist/manual，可选）、add（要收录的域名，逗号分隔，可选）、remove（不再收录的域名，可选）",
    admin=True,
)
async def _exec_set_doc_filter(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    gid = event.group_id
    mode_raw = str(args.get("mode") or "").strip()
    mode = {"all": "all", "全部": "all", "全部同步": "all",
            "白名单": "whitelist", "whitelist": "whitelist",
            "手动": "manual", "manual": "manual"}.get(mode_raw, mode_raw or None)
    add = [
        d.strip()
        for d in re.split(r"[,，\s]+", str(args.get("add") or ""))
        if d.strip()
    ]
    remove = [
        d.strip()
        for d in re.split(r"[,，\s]+", str(args.get("remove") or ""))
        if d.strip()
    ]
    if not mode and not add and not remove:
        return docsync.filter_text(gid)
    try:
        docsync.set_filter(gid, mode=mode, add=add or None, remove=remove or None)
    except ValueError as exc:
        return str(exc)
    return "已更新 ✅\n" + docsync.filter_text(gid)


@_action(
    "find_image",
    "按内容关键词在群相册索引里找图（找的是用机器人存进相册的图）",
    "keyword（图片内容关键词）",
)
async def _exec_find_image(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    kw = str(args.get("keyword") or "").strip()
    if not kw:
        return "想找什么内容的图？告诉我关键词，比如「雪山」「猫」"
    hits = album_index.search(event.group_id, kw, limit=5)
    if not hits:
        return f"相册索引里没有和「{kw}」相关的图（我只索引了用我存进相册的图）"
    lines = [f"找到 {len(hits)} 张相关图："]
    for i, e in enumerate(hits, 1):
        desc = e.get("desc") or "（无描述）"
        lines.append(f"{i}. {desc}｜相册「{e.get('album_name') or '?'}」｜{e.get('time', '')} 存")
    text = "\n".join(lines)
    try:
        await bot.send(event, Message(text))
        first = hits[0].get("url") or ""
        if first:
            try:
                from nonebot.adapters.onebot.v11 import MessageSegment

                await bot.send(event, MessageSegment.image(first))
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[ai_chat] 意图找图补发图片失败（URL 可能过期）: {exc!r}")
        return None  # 已经发过了
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 意图找图发送失败: {exc!r}")
        return text


@_action(
    "search_links",
    "翻本群链接收藏库：按关键词搜，或看最近收藏",
    "keyword（关键词，可不填=看最近的）",
)
async def _exec_search_links(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    kw = str(args.get("keyword") or "").strip()
    if kw:
        hits = linkarchive.search(event.group_id, kw, limit=5)
        if not hits:
            return f"收藏里没有和「{kw}」相关的链接"
        lines = [f"找到 {len(hits)} 条相关链接："]
    else:
        hits = linkarchive.recent(event.group_id, limit=10)
        if not hits:
            return "收藏库还是空的——群里发个链接我会自动收进来"
        lines = [f"最近 {len(hits)} 条收藏："]
    for i, e in enumerate(hits, 1):
        lines.append(
            f"{i}. {e.get('title', '')[:40]}\n   {e['url']}\n   {e.get('by', '')} · {e.get('time', '')}"
        )
    return "\n".join(lines)


@_action(
    "fetch_resource",
    "综合调取资源：在群文件、群相册、群精华消息、链接收藏、本群绑定的在线文档里"
    "按关键词找东西，群文件会换成下载链接直接发出来",
    "keyword（要找的关键词）",
)
async def _exec_fetch_resource(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    kw = str(args.get("keyword") or "").strip()
    if not kw:
        kw = _extract_url(event.get_plaintext()) or ""
    if not kw:
        return "要找什么？给我个关键词，比如「虚拟机」「团建照片」"
    gid = event.group_id
    sections: list[str] = []

    # ---- 1) 群文件 + 群相册：找到就尽量换成可下载的链接 ----
    try:
        index = await resource_index.get_index(bot, gid, ttl=cfg.ai_file_index_ttl)
        if not index.error:
            hits = resource_index.search(index, kw, limit=5)
            # 决定性命中：关键词完整出现在文件名里（如「图吧工具箱」⊂「图吧工具箱202601.7z」）
            # 就只出精确匹配，不再把模糊沾边的工具一股脑念出来
            core = resource_index._normalize(kw)
            exact = [
                e for e in hits
                if core and core in resource_index._normalize(e.name)
            ]
            for e in (exact or hits[:3]):
                url = e.url
                if not url and e.file_id:
                    try:
                        data = await bot.call_api(
                            "get_group_file_url", group_id=gid, file_id=e.file_id
                        )
                        url = str(data.get("url") or "")
                    except Exception as exc:  # noqa: BLE001
                        logger.info(f"[ai_chat] 取群文件下载链接失败: {exc!r}")
                bits = [f"{e.name}（{e.where}"]
                if e.uploader:
                    bits.append(f"{e.uploader}上传")
                if e.size:
                    bits.append(resource_index.fmt_size(e.size))
                line = "，".join(bits) + "）"
                if url:
                    line += f"\n   下载: {url}\n   （链接有时效，失效了让我再调一次）"
                else:
                    line += "\n   （下载链接没取到，可以在群文件里按名字直接找）"
                sections.append("群文件找到：" + line)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调取-群文件检索失败: {exc!r}")

    # ---- 2) 群精华消息 ----
    try:
        eidx = await essence.get_index(
            bot, gid, ttl=cfg.ai_essence_index_ttl
        )
        if eidx.error:
            logger.info(f"[ai_chat] 调取-精华索引不可用: {eidx.error}")
        else:
            emsgs = essence.search(eidx, kw, limit=3)
            for m in emsgs:
                snippet = (m.text or "").strip()[:80] or "（非文字内容）"
                sections.append(f"群精华（{m.who} {m.when} 设）：{snippet}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调取-精华检索失败: {exc!r}")

    # ---- 3) 链接收藏库 ----
    try:
        for e in linkarchive.search(gid, kw, limit=3):
            sections.append(f"链接收藏：{e.get('title', '')[:40]}\n   {e['url']}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调取-链接收藏检索失败: {exc!r}")

    # ---- 4) 本群绑定的在线文档 ----
    try:
        doc_text = await docsync.read_bound_text(gid, kw, limit=5)
        if doc_text and "还没有收录" not in doc_text and "没有和" not in doc_text:
            # read_bound_text 末尾自带完整文档链接，直接引用
            sections.append("在线文档里找到：\n" + doc_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 调取-在线文档读取失败: {exc!r}")

    if not sections:
        return (
            f"群文件、群相册、群精华、链接收藏、在线文档里都没找到和「{kw}」相关的东西。"
            "如果应该是文件，可能是群文件里没有，或者还没人发过～"
        )
    return f"关于「{kw}」找到这些：\n\n" + "\n\n".join(sections)


@_action(
    "remember",
    "让机器人长期记住一条事实（之后相关问题会自动想起来）",
    "fact（要记住的内容，120 字以内）",
)
async def _exec_remember(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    if not cfg.ai_memory_enabled:
        return "长期记忆功能当前是关闭的"
    fact = str(args.get("fact") or "").strip()
    if not fact:
        return "要记什么呀？直接说「记住 XXX」就行"
    if len(fact) > 120:
        return "太长了，一条记忆控制在 120 字以内，拆成几条来记吧"
    by = getattr(event.sender, "card", "") or getattr(event.sender, "nickname", "") or "?"
    result = memory.add(event.group_id, fact, by=by, max_entries=cfg.ai_memory_max_per_group)
    if result == "dup":
        return "这条我已经记着了，不用重复教～"
    return f"记住了（第 {memory.count(event.group_id)} 条）。问相关问题的时候我会想起来的"


@_action("memory_list", "查看机器人记住了哪些事", "无参数")
async def _exec_memory_list(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    return memory.list_text(event.group_id)


@_action(
    "forget",
    "删除记忆（keyword=删包含该词的记忆，全部=清空）",
    "keyword",
    admin=True,
)
async def _exec_forget(bot: Bot, event: GroupMessageEvent, args: dict, is_admin: bool) -> str | None:
    kw = str(args.get("keyword") or "").strip()
    if not kw:
        return "要忘掉什么？说个关键词，或者说「忘掉全部」清空"
    if kw in ("全部", "清空", "所有"):
        removed = memory.clear(event.group_id)
        return f"已清空全部 {removed} 条记忆"
    removed = memory.remove(event.group_id, kw)
    if removed:
        return f"已删掉 {removed} 条包含「{kw}」的记忆"
    return f"没有找到包含「{kw}」的记忆"


# ================================================================ 外挂动作
# 人设相关在 __init__ 里（避免循环导入），__init__ 启动时把自己的执行器
# 塞进这张表：intent.EXTRA_ACTIONS["set_persona"] = fn ...

EXTRA_ACTIONS: dict[str, Callable[..., Awaitable[str | None]]] = {}
# key -> (desc, args_doc, admin)
EXTRA_SPECS: dict[str, tuple[str, str, bool]] = {}


# ================================================================ 预筛


_GATE_KEYWORDS = (
    # 文档
    "绑定", "创建文档", "建文档", "新建文档", "在线文档", "收集到文档", "存到文档",
    "收进文档", "收录", "进文档", "文档过滤", "白名单", "只收",
    # 读文档
    "读文档", "看看文档", "文档里", "文档内容", "文档有", "文档都", "查文档", "搜文档",
    "念念文档", "文档里有什么", "最近收录", "读一下绑定", "打开文档",
    # 相册
    "找图", "找到那张", "哪张图", "那张", "的图", "图片在哪", "照片在哪", "相册里",
    # 链接
    "收藏的链接", "链接收藏", "发过的链接", "之前的链接", "翻链接", "链接库",
    # 收录链接（所有人可用）
    "存文档", "存一下", "存进文档", "收藏一下", "收藏这个", "帮我收",
    # 调取资源
    "调取", "调出来", "提取", "发给我", "发我", "下载", "群文件", "精华",
    "找文件", "找资源", "帮我找", "帮我看看有没有",
    # 索取/投递类说法（「图吧工具箱发一下」这种没有资源名词的句子也要进路由）
    "发一下", "发个", "发来", "来一份", "来个", "给一份", "给个", "分享",
    "求", "想要", "有吗", "有没有", "在哪", "哪里有",
    # 常见资源名词（裸名词@也可能是在找东西，交给路由判断）
    "工具箱", "软件", "安装包", "补丁", "驱动", "电子书", "网盘", "资源",
    "压缩包", "安装教程", "虚拟机", "模拟器",
    # 记忆
    "记住", "记一下", "忘记", "忘了", "记忆",
    # 人设
    "人设", "性格",
)


def looks_actionable(text: str) -> bool:
    """正则预筛：不含任何候选关键词的消息直接走聊天，不进路由（省钱省时延）。

    带链接的 @ 消息永远进路由——「@我 + 链接」是所有人可用的收录入口，
    哪怕一个关键词都没说（路由模型自己判断是想收录还是随口聊链接）。
    """
    t = (text or "").lower()
    if _URL_RE.search(t):
        return True
    return any(k in t for k in _GATE_KEYWORDS)


# ================================================================ 路由模型调用


def _router_prompt() -> str:
    lines = [
        "你是QQ群机器人的意图路由器。判断用户消息是想执行功能还是普通聊天。",
        "可用功能：",
    ]
    for name, spec in ACTIONS.items():
        admin = "（仅管理员）" if spec["admin"] else ""
        lines.append(f"- {name}: {spec['desc']}。参数: {spec['args']}{admin}")
    for name, (desc, args_doc, admin) in EXTRA_SPECS.items():
        admin = "（仅管理员）" if admin else ""
        lines.append(f"- {name}: {desc}。参数: {args_doc}{admin}")
    lines.append("- chat: 普通聊天/提问/请求内容创作，不属于以上任何功能")
    lines += [
        "判定原则：",
        "- 消息带明确动作意图（绑定/创建/找图/翻链接/记住/忘记/换人设/查状态）才选对应功能；",
        "- 消息里有 http 链接：用户想让保存/收录进文档 → save_link；"
        "只是分享链接求点评或提问 → chat；",
        "- 用户要找/调取群里的东西（文件、图片、视频、资料、精华、文档内容）→ fetch_resource；",
        "- 「XX发一下」「XX发我」「求XX」这类索取句式，且 XX 是具体的东西"
        "（文件/图/照片/视频/表格/链接/软件等资源名词）→ fetch_resource（keyword=XX）；",
        "- 征求意见、观点、情感类的问句——有没有必要…、要不要…、该不该…、"
        "你觉得…怎么样、有没有可能…——即使含有「有没有/发一下/求」这些词也是聊天 → chat。"
        "例：「你说有没有必要读研」→ chat；「有没有团建的照片」→ fetch_resource；",
        "- 问问题、闲聊、要知识、要创作 → chat；",
        "- 是普通提问但恰好含有功能关键词（比如「文档是什么意思」）→ chat；",
        "- 拿不准 → chat。",
        "只输出一行 JSON，不要输出任何别的文字：",
        '{"action":"功能名","args":{"参数名":"值"}}',
    ]
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """从模型回复里抠 JSON（容忍代码围栏和前后废话）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _classify(text: str) -> dict:
    """调路由模型。返回 {"action": ..., "args": ...}，解析失败按 chat 处理。"""
    global _client
    if _client is None:
        _client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    client = _client
    from .ai_client import chat_kwargs

    # 200 在 reasoning 模型下可能被吃到只剩半个 JSON，给 400 保底
    resp = await client.chat.completions.create(
        **chat_kwargs(
            [
                {"role": "system", "content": _router_prompt()},
                {"role": "user", "content": text[:500]},
            ],
            cfg,
            max_tokens=400,
            temperature=0.1,
        )
    )
    reply = (resp.choices[0].message.content or "").strip()
    data = _extract_json(reply) or {}
    action = str(data.get("action") or "chat").strip()
    args = data.get("args") if isinstance(data.get("args"), dict) else {}
    return {"action": action, "args": args}


# ================================================================ 对外入口


async def try_handle(bot: Bot, event: GroupMessageEvent, text: str, is_admin: bool) -> bool:
    """@ 消息进来先过这里。执行了动作返回 True（消息到此为止）；
    判定为聊天/解析失败/功能关闭返回 False，调用方继续正常聊天流程。"""
    if not cfg.ai_intent_enabled or not looks_actionable(text):
        return False
    try:
        route = await _classify(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 意图路由调用失败，退回聊天: {exc!r}")
        return False

    action = route["action"]
    if action == "chat":
        return False

    spec = ACTIONS.get(action)
    if spec is not None:
        exec_fn = spec["exec"]
        need_admin = spec["admin"]
    elif action in EXTRA_ACTIONS:
        exec_fn = EXTRA_ACTIONS[action]
        need_admin = EXTRA_SPECS.get(action, ("", "", False))[2]
    else:
        logger.info(f"[ai_chat] 路由返回未知动作 {action!r}，退回聊天")
        return False

    if need_admin and not is_admin:
        await bot.send(event, Message("这个操作只有群主或管理员可以让我做哦"))
        return True

    try:
        reply = await exec_fn(bot, event, route["args"], is_admin)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 意图动作 {action} 执行失败: {exc!r}")
        reply = f"执行的时候出了点问题：{exc}"
    if reply is not None:
        try:
            await bot.send(event, Message(reply))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 意图动作回复发送失败: {exc!r}")
    logger.info(f"[ai_chat] 意图路由命中: {action} args={route['args']}")
    return True
