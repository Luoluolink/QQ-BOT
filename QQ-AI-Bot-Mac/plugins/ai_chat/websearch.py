"""联网搜索：额度账本 + DeepSeek Search API 客户端。

两条路线并存，由 .env 的 AI_WEB_SEARCH_MODE 选择：

server = 服务端工具（MiMo 私有 web_search，挂 tools 由服务端执行）。
    计数不靠我们自己估，而是直接读官方响应里的
    ``usage.web_search_usage.tool_usage`` —— 那是服务端算出来的真实计费次数，
    比「我们发了几次请求」精确得多（模型判断不需要联网时就是 0）。

client = 客户端搜索（DeepSeek Search API，2026-10-17 开放）。
    POST /v1/tools/search 独立端点：任何模型可用（包括 flash）、
    结构化结果（标题/来源/日期/权威度/摘要/链接）、¥0.75/千次成功调用
    （2026-11-01 前免费）。搜到的内容由 format_results 格式化成
    「联网检索结果」system 消息注入——Chat Completions 的历史机制与
    风控重试逻辑零改动，结果还可见可控。
    为什么不用 Responses 内置 web_search：实测它对 deepseek-flash 直接忽略
    （官方文档原话 "Ignored with deepseek-flash"），只有 v4-pro 支持。

额度按自然日计算，并落盘到 data/web_search_usage.json。
为什么要落盘：机器人重启很频繁（改配置、换模型都要重启），
只存内存的话每次重启都会把当天已用的次数清零，额度形同虚设。
"""

from __future__ import annotations

import json
from datetime import date
import base64
import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx2
from nonebot import get_plugin_config, logger

from .ai_client import chat_kwargs, make_client
from .config import Config

cfg = get_plugin_config(Config)

# 项目根目录下的 data/（已在 .gitignore 里忽略）
_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "web_search_usage.json"
)

# DeepSeek Search API：同一个 key，拼在对话 base 之后即可
_SEARCH_PATH = "/tools/search"
_SEARCH_TIMEOUT = 20.0

_state: dict[str, Any] | None = None


def _today() -> str:
    return date.today().isoformat()


def _load() -> dict[str, Any]:
    """读当天账本；跨天自动清零。"""
    global _state
    if _state is None:
        data: dict[str, Any] = {}
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            # 文件不存在或内容损坏都不算错误，从零开始记即可
            data = {}
        _state = data

    if _state.get("day") != _today():
        _state = {"day": _today(), "used": 0, "rounds": 0}
        _save()
    return _state


def _save() -> None:
    if _state is None:
        return
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(_state, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        # 写不进去也不能影响聊天，最坏结果是重启后额度被重置
        pass


def _field(obj: Any, name: str) -> Any:
    """从 dict 或 SDK 对象里取字段。

    OpenAI SDK 返回的是 pydantic 模型，厂商自定义的字段（web_search_usage）
    会落在 model_extra 里；dict 和对象两种形态都试一遍最省心。
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if not isinstance(extra, dict):
            extra = getattr(obj, "__pydantic_extra__", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return value


def extract_usage(resp: Any) -> tuple[int, int]:
    """从模型响应里取出联网用量，返回 (计费次数, 是否触发联网)。

    模型判断这个问题不需要联网时，这里会返回 0 —— 不花钱，也就不会扣额度。
    """
    usage = _field(resp, "usage")
    ws = _field(usage, "web_search_usage")
    try:
        count = int(_field(ws, "tool_usage") or 0)
    except (TypeError, ValueError):
        count = 0
    return max(0, count), (1 if count > 0 else 0)


def record(resp: Any) -> int:
    """把一次模型调用的联网用量记进当天账本，返回本次计费次数。"""
    count, rounds = extract_usage(resp)
    if count == 0 and rounds == 0:
        return 0
    state = _load()
    state["used"] = int(state.get("used") or 0) + count
    state["rounds"] = int(state.get("rounds") or 0) + rounds
    _save()
    return count


def used() -> int:
    """今日已用的计费次数。"""
    return int(_load().get("used") or 0)


def rounds() -> int:
    """今日真正触发了联网的回答条数。"""
    return int(_load().get("rounds") or 0)


def remaining(limit: int) -> int:
    return max(0, limit - used())


def exhausted(limit: int) -> bool:
    """额度是否已用尽。limit <= 0 视为完全不允许联网。"""
    return used() >= limit


def reset() -> None:
    global _state
    _state = {"day": _today(), "used": 0, "rounds": 0}
    _save()


def snapshot(limit: int) -> dict[str, Any]:
    return {
        "day": _load().get("day") or _today(),
        "used": used(),
        "rounds": rounds(),
        "limit": limit,
        "remaining": remaining(limit),
    }


# 出现这些词，才认为「这条消息可能需要实时信息」，此时才挂联网参数。
#
# 为什么要卡这一刀：挂上 tools 之后，服务端每次都要先跑一轮「要不要联网」的
# 判断，实测每条消息因此多等约 1.2 秒 —— 哪怕最后根本没联网。
# 日常闲聊没必要付这个代价，所以只在这类消息上才开启联网能力。
#
# 想调松/调紧直接改这个元组即可；想彻底不卡，把 .env 里的
# AI_WEB_SEARCH_TRIGGER 改成 always。
WEB_SEARCH_KEYWORDS = (
    # 时效性词汇：答案会随时间变化
    "今天", "今日", "现在", "目前", "当前", "最新", "最近", "近期", "实时",
    "刚刚", "昨天", "明天", "本周", "这周", "上周", "本月", "这个月", "今年",
    "此刻", "如今", "截至",
    # 依赖外部数据的题材
    "天气", "气温", "下雨", "台风", "降温", "新闻", "头条", "股价", "股票",
    "指数", "大盘", "涨跌", "汇率", "油价", "票价", "航班", "车次", "比分",
    "赛程", "赛况", "开奖", "发布会", "价格", "多少钱", "售价", "行情",
    "政策", "新规", "法规", "上线", "发售", "销量", "票房", "排名",
    # 显式联网意图（也是关键词没命中时的兜底说法）
    "搜一下", "搜搜", "搜索", "查一下", "查查", "百度", "谷歌",
    "上网查", "联网", "网上查", "查得到",
)


def looks_like_web_query(text: str) -> bool:
    """这条消息值不值得挂联网参数。"""
    if not text:
        return False
    return any(word in text for word in WEB_SEARCH_KEYWORDS)


def record_round() -> int:
    """客户端搜索模式按次记账：一次成功检索 = 1 次。

    与 MiMo 模式的 record(resp)（读服务端 usage）并存：
    换回 MiMo 走 record，DeepSeek 客户端搜索走这里。
    """
    state = _load()
    state["rounds"] = int(state.get("rounds") or 0) + 1
    state["used"] = int(state.get("used") or 0) + 1
    _save()
    return 1


async def search_web(
    query: str, count: int = 5, api_key: str = "", api_base: str = ""
) -> list[dict]:
    """联网检索，返回结果列表；任何失败都返回空列表。

    两条路，按当前对话模型自动选：
    - api_base 是 DeepSeek → 官方 Search API（按次计费，成功才记额度）
    - 其他模型（MiMo/GLM…）→ 必应抓取，无需 key——
      旧实现把搜索端点绑在 DeepSeek 上，换模型后请求 404、联网失效。
    聊天功能永远比联网重要，不能因为搜不到就不说话。
    """
    q = (query or "").strip()[:128]
    if not q:
        return []
    if "deepseek" in (api_base or "").lower() and api_key:
        return await _deepseek_search(q, count, api_key, api_base)
    return await _bing_search(q, count, api_key, api_base)


async def _deepseek_search(q: str, count: int, api_key: str, api_base: str) -> list[dict]:
    url = (api_base or "https://api.deepseek.com/v1").rstrip("/") + _SEARCH_PATH
    try:
        async with httpx2.AsyncClient(timeout=_SEARCH_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": q, "count": count, "need_content": False},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[websearch] 联网检索请求失败: {exc!r}")
        return []
    if resp.status_code != 200:
        logger.warning(
            f"[websearch] 联网检索 HTTP {resp.status_code}: {resp.text[:120]}"
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        logger.warning("[websearch] 联网检索返回的不是 JSON")
        return []
    results = data.get("results") or []
    if not results:
        return []
    record_round()
    return results


def format_results(results: list[dict], query: str) -> str:
    """把检索结果格式化成注入给模型的「联网检索结果」上下文。

    纪律与人设铁律里的群资源检索一致：列表里没有的就直说没有，
    宁可答不上来也不许编。链接保留——群友点得到，模型也更有依据。
    """
    lines = [
        f"【联网检索结果】（刚为用户检索了「{query[:40]}」，共 {len(results)} 条。"
        "涉及这些内容的问题必须依据下面的资料回答，资料里没有的信息就直说"
        "不知道，不要编造；回复时可以附上相关链接）"
    ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "无标题"
        site = (r.get("site_name") or "").strip()
        date = (r.get("date") or "")[:10]
        snippet = (r.get("snippet") or r.get("summary") or "").strip()
        url = (r.get("url") or "").strip()
        head = f"{i}. {title}"
        meta = " · ".join(p for p in (site, date) if p)
        if meta:
            head += f"（{meta}）"
        lines.append(head)
        if snippet:
            lines.append(f"   {snippet[:300]}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


_BING_URL = "https://cn.bing.com/search"
_BING_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _bing_query(text: str) -> str:
    """把自然语言搜索请求裁成搜索关键词。

    必应对整句话的命中率很差（「帮我搜一下今天的科技新闻」会搜出一堆
    不相干的站），DeepSeek API 能理解整句但必应不能——把命令动词和
    客套 filler 剥掉，只留核心词。
    """
    q = text.strip()
    q = re.sub(
        r"^(帮我|麻烦|请|先)?(上网|联网|用必应|用百度)?"
        r"(帮我)?(搜一下|搜索一下|搜搜|搜索|查一下|查查|查查资料|查|找一下|找找|看看)",
        "", q,
    )
    q = re.sub(r"(要)?最新的$|(要)?最新版的$", "", q)
    q = re.sub(r"(今天|今日)的", "", q)
    q = q.strip("，。！？、 ,.!？?\n")
    return q or text.strip()


async def _search_keywords(query: str, api_key: str, api_base: str) -> str:
    """让对话模型把搜索请求浓缩成 2~4 个关键词。

    必应对整句和时间状语（「今天的科技新闻」）命中率极差——
    会搜出一堆日历天气站。让模型先浓缩成关键词，命中率立刻正常。
    模型不可用时退回正则裁剪。
    """
    try:
        client = make_client(api_key, api_base, cfg.ai_proxy)
        resp = await client.chat.completions.create(
            **chat_kwargs(
                [{"role": "user", "content":
                  "把用户的搜索请求改写成适合搜索引擎的关键词（2~4 个词，空格分隔）。\n"
                  "规则：去掉「帮我/搜一下/查一下」这类请求用语；"
                  "去掉「今天的/最新的」这类时间修饰——搜索引擎会自己排新内容；"
                  "只保留核心主题词。\n"
                  "示例：「帮我搜一下今天的科技新闻，要最新的」→ 科技新闻\n"
                  "只输出关键词，不要任何解释或标点。\n"
                  "用户的搜索请求：" + query}],
                cfg,
                max_tokens=600,
                temperature=0.1,
            )
        )
        kw = (resp.choices[0].message.content or "").strip()
        kw = re.sub(r"^[“\"'「『\s]+|[”\"'」』。\s]+$", "", kw)
        kw = re.sub(r"(今天|今日)的", "", kw)   # 时间修饰在必应只会搜出日历天气站
        return " ".join(kw.split()) or query
    except Exception:  # noqa: BLE001
        return _bing_query(query)


async def _bing_search(
    query: str, count: int, api_key: str = "", api_base: str = ""
) -> list[dict]:
    """抓必应（国内直连、无需 key）。解析失败/被风控返回空列表。"""
    query = await _search_keywords(query, api_key, api_base)
    if not query:
        return []
    try:
        async with httpx2.AsyncClient(
            timeout=_SEARCH_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(
                _BING_URL,
                params={"q": query, "count": str(max(count, 10))},
                headers={
                    "User-Agent": _BING_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[websearch] 必应检索请求失败: {exc!r}")
        return []
    if resp.status_code != 200:
        logger.warning(f"[websearch] 必应检索 HTTP {resp.status_code}")
        return []
    page = resp.text
    results: list[dict] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        m = re.search(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        # 必应的跳转链接：/ck/a?u=a1<base64url> —— 解出真实地址
        ck = re.search(r"[?&]u=a1([^&]+)", href)
        if ck:
            b64 = ck.group(1)
            b64 += "=" * (-len(b64) % 4)
            try:
                href = base64.urlsafe_b64decode(b64).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        unq = unquote(href)
        if unq.startswith("http"):
            href = unq
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = html.unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip() if sm else ""
        if title and href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= count:
            break
    if results:
        record_round()   # 计入每日额度，让 daily limit 对抓取同样生效
    return results
