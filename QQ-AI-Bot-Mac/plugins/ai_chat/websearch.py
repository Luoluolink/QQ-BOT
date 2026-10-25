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
from pathlib import Path
from typing import Any

import httpx2
from nonebot import logger

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
    """联网检索，返回 DeepSeek Search API 的结果列表；任何失败都返回空列表。

    计费与降级：只有 HTTP 200 且带回结果的调用才记额度（官方规则：
    空结果与失败不计费）；网络错误/参数错误只告警不抛出——
    聊天功能永远比联网重要，不能因为搜不到就不说话。
    """
    if not api_key:
        return []
    q = (query or "").strip()[:128]  # 官方上限 128 字符
    if not q:
        return []
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
