"""网页爬虫：B 站 / GitHub 搜索 + 网页正文抓取。

给机器人三条新能力：
1. 「去 b 站找几个自然风光的视频」→ 搜 B 站，把视频标题和链接交给模型；
2. 「去 github 上找几个 xx 项目」→ 搜 GitHub 仓库，返回链接；
3. 用户直接丢一个链接 → 把网页正文抓下来，交给模型总结。

全部用标准库 urllib 在线程池里跑（和 filetext.py 同一套约束：
venv 里没有 httpx/aiohttp），ProxyHandler({}) 显式禁代理直连。

抓回来的内容以「参考资料」文本块的形式注入对话上下文，
由模型自己组织成回复 —— 不直接发消息，保持和群资源检索同一套模式。

B 站搜索说明：
    现在的 search 接口要求 wbi 签名 + buvid3 cookie，缺了会返回 -412/-403。
    这里实现了完整的 wbi 签名（从 nav 接口拿 img_key/sub_key，按官方
    mixin 表重排后 md5），并用 CookieJar 先访问一次主站引导出 cookie。
    全程无需登录。签名算法失效时搜索会静默返回空，不影响其他功能。
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_mod
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

from nonebot import logger

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"https?://[^\s<>\"'）)\]，。；！？]+")
BILI_RE = re.compile(r"(b\s*站|bilibili|哔哩哔哩|哔站)", re.I)
GITHUB_RE = re.compile(r"github|开源(社区|平台|仓库)|gayhub", re.I)
YT_RE = re.compile(r"youtube|youtu\.be|油管", re.I)
SEARCH_INTENT_RE = re.compile(
    r"找|搜|搜索|找找|找下|找一下|看看|看下|推荐|来几|来点|有没有|查查|查下|扒"
)

# wbi 签名用的官方混淆表（公开算法，B 站前端 js 里能拿到）
_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
)

# 共享的 cookie 容器：先访问主站引导出 buvid3，之后所有 B 站请求共用
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(_jar),
)


# ---------------------------------------------------------------- 基础请求


def _get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 15.0,
    proxy: str = "",
) -> tuple[int, bytes, str]:
    """线程里跑的 GET：返回 (状态码, 响应体, 最终 URL)。

    默认用共享 opener（带 CookieJar、禁代理），跟随重定向后
    final_url 用来解析 b23.tv 之类的短链。
    传 proxy（如 http://127.0.0.1:7890）时改走本机代理——YouTube 这类
    直连不了的站点靠它；此时用独立 opener，不污染 B 站的共享 Cookie。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = _opener
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read()
        # 有些站点（如 python.org）不协商、直接回 gzip；urllib 不会自动解压
        if (resp.headers.get("Content-Encoding") or "").lower() in ("gzip", "x-gzip"):
            import gzip as _gzip
            try:
                body = _gzip.decompress(body)
            except OSError:
                pass
        return resp.status, body, resp.geturl()


async def _aget(
    url: str, *, headers: dict | None = None, timeout: float = 15.0
) -> tuple[int, bytes, str]:
    return await asyncio.to_thread(_get, url, headers=headers, timeout=timeout)


def _decode(body: bytes, content_type: str = "") -> str:
    """按 Content-Type / utf-8 / gbk 的顺序尝试解码，全失败就忽略错误。"""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "gbk"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------- 网页正文


_TAG_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_BR_RE = re.compile(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>", re.I)
_ANYTAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def fetch_text_sync(url: str, max_chars: int, timeout: float) -> str:
    """下载网页并抽正文（同步版，供线程池调用）。"""
    status, body, final_url = _get(url, timeout=timeout)
    if status >= 400:
        return ""
    text = _decode(body)
    text = _TAG_RE.sub(" ", text)
    text = _BR_RE.sub("\n", text)
    text = _ANYTAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:max_chars]


async def fetch_text(url: str, max_chars: int, timeout: float) -> str:
    return await asyncio.to_thread(fetch_text_sync, url, max_chars, timeout)


def _page_title_sync(url: str, timeout: float, proxy: str = "") -> str:
    """顺手拿 <title>，抓取失败不致命。YouTube 等站点需传 proxy。"""
    try:
        _, body, _ = _get(url, timeout=timeout, proxy=proxy)
        text = _decode(body)
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        return html_mod.unescape(m.group(1)).strip() if m else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- B 站


def _wbi_sign(params: dict) -> dict:
    """给查询参数加上 wts + w_rid 签名。

    算法（公开）：
    1. 从 nav 接口拿 wbi_img 的 img_key / sub_key（取 URL 文件名）；
    2. img_key + sub_key 按混淆表重排，取前 32 位得 mixin_key；
    3. 参数按 key 排序 urlencode，加上 wts（当前秒级时间戳）；
    4. md5(查询串 + mixin_key) 得 w_rid。
    """
    _, body, _ = _get("https://api.bilibili.com/x/web-interface/nav")
    data = json.loads(_decode(body)).get("data") or {}
    wbi = data.get("wbi_img") or {}
    img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        raise ValueError("拿不到 wbi key")
    mixin = "".join((img_key + sub_key)[i] for i in _MIXIN_TAB)[:32]

    params = {**params, "wts": int(time.time())}
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def search_bilibili_sync(keyword: str, limit: int, timeout: float) -> list[dict]:
    """搜 B 站视频，返回 [{title, url, author, play, duration, desc}]。"""
    # 先摸一次主站引导 cookie（buvid3），不带 cookie 的搜索会被 -412
    try:
        _get("https://www.bilibili.com/", timeout=timeout)
    except Exception:  # noqa: BLE001
        pass

    params = _wbi_sign(
        {
            "keyword": keyword,
            "search_type": "video",
            "page": 1,
            "page_size": max(limit, 5),
        }
    )
    _, body, _ = _get(
        "https://api.bilibili.com/x/web-interface/search/type?" +
        urllib.parse.urlencode(params),
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=timeout,
    )
    data = json.loads(_decode(body))
    if data.get("code") != 0:
        raise ValueError(f"B站搜索接口返回 {data.get('code')}: {data.get('message')}")

    hits: list[dict] = []
    for item in (data.get("data") or {}).get("result") or []:
        if item.get("type") != "video":
            continue
        bvid = item.get("bvid") or ""
        if not bvid:
            continue
        hits.append(
            {
                "title": re.sub(r"</?em[^>]*>", "", item.get("title") or ""),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "author": item.get("author") or "",
                "play": item.get("play") or 0,
                "duration": item.get("duration") or "",
                "desc": (item.get("description") or "")[:120],
            }
        )
        if len(hits) >= limit:
            break
    return hits


async def search_bilibili(keyword: str, limit: int, timeout: float) -> list[dict]:
    return await asyncio.to_thread(
        search_bilibili_sync, keyword, limit, timeout
    )


def _bili_video_info_sync(bvid: str, timeout: float) -> dict:
    """单视频信息（公开接口，无需签名/cookie）。"""
    _, body, _ = _get(
        "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid,
        headers={"Referer": "https://www.bilibili.com/"},
        timeout=timeout,
    )
    data = json.loads(_decode(body))
    if data.get("code") != 0:
        return {}
    d = data.get("data") or {}
    return {
        "title": d.get("title") or "",
        "url": f"https://www.bilibili.com/video/{bvid}",
        "author": (d.get("owner") or {}).get("name") or "",
        "play": (d.get("stat") or {}).get("view") or 0,
        "desc": (d.get("desc") or "")[:500],
        "pic": d.get("pic") or "",
    }


def _resolve_bvid_sync(url: str, timeout: float) -> str:
    """从链接（含 b23.tv 短链）里解析出 BV 号。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return m.group(1)
    if "b23.tv" in url or "bili2233" in url:
        # 短链：让它跟一次重定向，从最终地址里拿 BV 号
        try:
            _, _, final = _get(url, timeout=timeout)
            m = re.search(r"(BV[0-9A-Za-z]{10})", final)
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""
    return ""


# ---------------------------------------------------------------- GitHub


def search_github_sync(keyword: str, limit: int, timeout: float) -> list[dict]:
    """搜 GitHub 仓库（匿名接口，10 次/分钟足够用）。"""
    _, body, _ = _get(
        "https://api.github.com/search/repositories?"
        + urllib.parse.urlencode(
            {"q": keyword, "sort": "stars", "order": "desc", "per_page": limit}
        ),
        headers={"Accept": "application/vnd.github+json"},
        timeout=timeout,
    )
    data = json.loads(_decode(body))
    items = data.get("items") or []
    hits: list[dict] = []
    for it in items[:limit]:
        hits.append(
            {
                "title": it.get("full_name") or "",
                "url": it.get("html_url") or "",
                "stars": it.get("stargazers_count") or 0,
                "lang": it.get("language") or "",
                "desc": (it.get("description") or "")[:160],
            }
        )
    return hits


async def search_github(keyword: str, limit: int, timeout: float) -> list[dict]:
    return await asyncio.to_thread(search_github_sync, keyword, limit, timeout)


# ---------------------------------------------------------------- YouTube


# 搜索页把结果内嵌在 ytInitialData 这个 JSON 里，抓出来走一遍树就能拿全
_YT_INITIAL_RE = re.compile(
    r"var ytInitialData\s*=\s*(\{.+?\})\s*;\s*</script>", re.S
)


def _walk_video_renderers(node) -> list[dict]:
    """深度优先找出 ytInitialData 里所有 videoRenderer 节点（保持文档顺序）。"""
    out: list[dict] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "videoRenderer" in cur:
                out.append(cur["videoRenderer"])
                continue  # 视频节点下面不用再翻
            stack.extend(reversed(list(cur.values())))
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
    return out


def _runs_text(obj) -> str:
    """YouTube 的文案字段都是 {runs: [{text: ...}]} 结构，拼接成纯文本。"""
    if not isinstance(obj, dict):
        return ""
    if obj.get("simpleText"):
        return str(obj["simpleText"])
    return "".join(r.get("text", "") for r in obj.get("runs") or [])


def search_youtube_sync(
    keyword: str, limit: int, timeout: float, proxy: str = ""
) -> list[dict]:
    """YouTube 搜索（解析网页内嵌的 ytInitialData，无需 API key）。

    国内网络直连不了 YouTube，必须传 proxy（本机代理端口）。
    """
    from urllib.parse import quote_plus

    url = (
        "https://www.youtube.com/results?hl=zh-CN&gl=CN&search_query="
        + quote_plus(keyword)
    )
    _, body, _ = _get(
        url,
        headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        timeout=timeout,
        proxy=proxy,
    )
    m = _YT_INITIAL_RE.search(_decode(body))
    if not m:
        raise ValueError("搜索页里没有 ytInitialData（可能被要求人机验证）")
    hits: list[dict] = []
    for v in _walk_video_renderers(json.loads(m.group(1))):
        vid = v.get("videoId") or ""
        if not vid:
            continue
        hits.append(
            {
                "title": _runs_text(v.get("title")),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "author": _runs_text(v.get("ownerText")),
                "views": _runs_text(v.get("viewCountText")),
                "duration": _runs_text(v.get("lengthText")),
            }
        )
        if len(hits) >= limit:
            break
    return hits


async def search_youtube(
    keyword: str, limit: int, timeout: float, proxy: str = ""
) -> list[dict]:
    return await asyncio.to_thread(
        search_youtube_sync, keyword, limit, timeout, proxy
    )


# ---------------------------------------------------------------- 意图识别


def _extract_keyword(text: str, platform: re.Pattern) -> str:
    """从「去b站找几个自然风光的视频」里抠出搜索词「自然风光」。

    粗暴但有效：删掉平台词、客套话、搜索动词、宾语词，
    剩下最长的那个词块就当关键词。
    """
    k = platform.sub(" ", text)
    k = re.sub(
        r"(帮我|麻烦|麻烦你|请你|给|为|替|想要|想|要|来|上|去|到|从|在)",
        " ", k,
    )
    k = re.sub(
        r"(找|搜|搜索|找找|找下|找一下|看看|看下|瞅瞅|推荐|推荐下|查查|查下|扒拉|扒|有没有|几|个|些|只|部|条|款|一些|点|下)",
        " ", k,
    )
    k = re.sub(
        r"(的)?(视频|片子|资源|链接|项目|仓库|开源项目|源码|教程|东西|内容|网站|网页)(们)?",
        " ", k,
    )
    tokens = [t.strip("，。！？、,!?~ ") for t in k.split()]
    tokens = [t for t in tokens if len(t) >= 2]
    if not tokens:
        return ""
    return max(tokens, key=len)


def _fmt_play(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "未知"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ---------------------------------------------------------------- 对外入口


async def build_context(
    text: str,
    *,
    max_results: int = 3,
    page_chars: int = 3500,
    timeout: float = 15.0,
    youtube_proxy: str = "",
) -> str:
    """按消息内容决定爬什么，返回注入给模型的资料块（没有就空串）。

    - 消息里带 URL（最多取 2 个）→ 抓正文；
    - 提到 B 站且有搜索意图 → 搜 B 站；
    - 提到 GitHub 且有搜索意图 → 搜 GitHub；
    - 提到 YouTube/油管且有搜索意图 → 走本机代理搜 YouTube。
    """
    blocks: list[str] = []

    # ---- 1. 直接给了链接：抓正文 ----
    urls = URL_RE.findall(text)[:2]
    for url in urls:
        try:
            bvid = await asyncio.to_thread(_resolve_bvid_sync, url, timeout)
            if bvid:
                info = await asyncio.to_thread(
                    _bili_video_info_sync, bvid, timeout
                )
                if info:
                    blocks.append(
                        f"【B站视频信息】\n标题：{info['title']}\n"
                        f"UP主：{info['author']}｜播放量：{_fmt_play(info['play'])}\n"
                        f"简介：{info['desc']}\n链接：{info['url']}"
                    )
                    continue
            page = await fetch_text(url, page_chars, timeout)
            if page:
                blocks.append(
                    f"【网页内容】{url}\n{page}\n"
                    "（以上是该网页的正文摘录，请据此回答）"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 网页抓取失败 {url}: {exc!r}")

    # ---- 2. B 站搜索意图 ----
    if BILI_RE.search(text) and SEARCH_INTENT_RE.search(text):
        kw = _extract_keyword(text, BILI_RE)
        if kw:
            try:
                hits = await search_bilibili(kw, max_results, timeout)
                if hits:
                    lines = [f"【B站搜索】关键词「{kw}」的结果："]
                    for i, h in enumerate(hits, 1):
                        lines.append(
                            f"{i}. {h['title']}｜UP主:{h['author']}"
                            f"｜播放:{_fmt_play(h['play'])}"
                            + (f"｜时长:{h['duration']}" if h["duration"] else "")
                            + f"\n   {h['url']}"
                        )
                    lines.append(
                        "（以上是真实搜索结果，回答时把对应链接发给用户，"
                        "不要编造上面没有的链接）"
                    )
                    blocks.append("\n".join(lines))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_chat] B站搜索失败（{kw}）: {exc!r}")

    # ---- 3. GitHub 搜索意图 ----
    if GITHUB_RE.search(text) and SEARCH_INTENT_RE.search(text):
        kw = _extract_keyword(text, GITHUB_RE)
        if kw:
            try:
                hits = await search_github(kw, max_results, timeout)
                if hits:
                    lines = [f"【GitHub搜索】关键词「{kw}」的结果："]
                    for i, h in enumerate(hits, 1):
                        lines.append(
                            f"{i}. {h['title']}｜★{h['stars']}"
                            + (f"｜{h['lang']}" if h["lang"] else "")
                            + f"\n   {h['desc']}\n   {h['url']}"
                        )
                    lines.append(
                        "（以上是真实搜索结果，回答时把对应链接发给用户，"
                        "不要编造上面没有的链接）"
                    )
                    blocks.append("\n".join(lines))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_chat] GitHub搜索失败（{kw}）: {exc!r}")

    # ---- 4. YouTube 搜索意图（必须走本机代理，直连不通） ----
    if YT_RE.search(text) and SEARCH_INTENT_RE.search(text):
        kw = _extract_keyword(text, YT_RE)
        if kw:
            if not youtube_proxy:
                blocks.append(
                    "【YouTube搜索】未配置代理地址，无法搜索 YouTube。"
                    "（如实告诉用户：需要先配置 ai_crawler_youtube_proxy）"
                )
            else:
                try:
                    hits = await search_youtube(kw, max_results, timeout, youtube_proxy)
                    if hits:
                        lines = [f"【YouTube搜索】关键词「{kw}」的结果："]
                        for i, h in enumerate(hits, 1):
                            lines.append(
                                f"{i}. {h['title']}｜{h['author']}"
                                + (f"｜{h['views']}" if h["views"] else "")
                                + (f"｜时长{h['duration']}" if h["duration"] else "")
                                + f"\n   {h['url']}"
                            )
                        lines.append(
                            "（以上是真实搜索结果，回答时把对应链接发给用户，"
                            "不要编造上面没有的链接）"
                        )
                        blocks.append("\n".join(lines))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[ai_chat] YouTube搜索失败（{kw}）: {exc!r}")
                    blocks.append(
                        "【YouTube搜索】搜索失败（本机代理可能没开或节点不通）。"
                        "如实告诉用户：YouTube 搜索依赖本机代理，请确认代理在运行。"
                    )

    if blocks:
        logger.info(f"[ai_chat] 爬虫命中 {len(blocks)} 个资料块: "
                    + ", ".join(b.split("】")[0].lstrip("【") for b in blocks))
    return "\n\n".join(blocks)


# ---------------------------------------------------------- 回复后处理：链接转分享卡片
#
# 模型经常只在正文里描述搜索结果、不带链接（或带了链接但 QQ 里只是裸文字）。
# 与其指望模型听话，不如在发送前把回复里的链接自动换成 QQ 分享卡片：
#   - B 站视频（含 b23.tv 短链）→ 接口取真实标题/UP主/封面，发正规视频卡；
#   - 其他链接 → 抓网页 <title> 当卡片标题；
#   - 任何一步失败都保留纯文本，绝不因为转卡片弄丢回复。

_URL_PAT = re.compile(r"https?://[^\s<>\"'【】（）()]+", re.IGNORECASE)
_MD_LINK_PAT = re.compile(r"\[([^\]]{1,80})\]\((https?://[^)\s]+)\)")


async def linkify(text: str, timeout: float = 8.0, proxy: str = "") -> list:
    """回复文本里的链接 → MessageSegment 列表（正文 + 分享卡片）。

    没有链接或卡片全部拿不到标题时，原样返回纯文本段，行为不变。
    YouTube 链接取标题必须走代理（proxy 参数）。
    """
    from nonebot.adapters.onebot.v11 import MessageSegment

    rest = text
    shares: list = []

    # 1) markdown 链接 [标题](url)：标题是现成的，直接发卡片
    for title, url in _MD_LINK_PAT.findall(rest)[:3]:
        rest = rest.replace(f"[{title}]({url})", title)
        shares.append(MessageSegment.share(url, title=title.strip()[:60]))

    # 2) 裸链接：B 站用接口取信息，其他抓网页标题
    for u in _URL_PAT.findall(rest)[:3]:
        title, content, image = "", "", ""
        try:
            bvid = await asyncio.to_thread(_resolve_bvid_sync, u, timeout)
            if bvid:
                info = await asyncio.to_thread(_bili_video_info_sync, bvid, timeout)
                if info:
                    title = info.get("title") or ""
                    author = info.get("author") or ""
                    content = f"UP主：{author}" if author else ""
                    image = info.get("pic") or ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[ai_chat] 链接卡片取B站信息失败 {u}: {exc!r}")
        if not title:
            # YouTube 直连不通，必须走代理
            p = proxy if YT_RE.search(u) else ""
            title = await asyncio.to_thread(_page_title_sync, u, timeout, p) or ""
        if title:
            rest = rest.replace(u, "")
            shares.append(
                MessageSegment.share(u, title=title[:60], content=content[:60], image=image)
            )
        # 标题拿不到的链接留在正文里（裸链接 QQ 也能点开）

    if not shares:
        return [MessageSegment.text(text)]

    # 摘走链接后残留的空编号行（"1."、"2."）和多余空行清一清
    rest = re.sub(r"(?m)^\s*\d+[.、：:]\s*$\n?", "", rest)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip(" \n-–—")
    segments: list = []
    if rest:
        segments.append(MessageSegment.text(rest))
    segments.extend(shares)
    return segments
