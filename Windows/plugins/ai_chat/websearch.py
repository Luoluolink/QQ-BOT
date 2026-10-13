"""联网搜索的额度账本。

为什么单独做一层：
MiMo 的联网能力是「服务插件」，按插件调用次数计费（国内 ¥16 / 1000 次）。
一轮搜索会按 max_keyword 的数值同时发起多个关键词，也就是一次提问可能
消耗好几次调用。群聊属于高频场景，不设闸门很容易跑飞。

关键点：计数不靠我们自己估，而是直接读官方响应里的
``usage.web_search_usage.tool_usage`` —— 那是服务端算出来的真实计费次数，
比「我们发了几次请求」精确得多（模型判断不需要联网时就是 0）。

额度按自然日计算，并落盘到 data/web_search_usage.json。
为什么要落盘：机器人重启很频繁（改配置、换模型都要重启），
只存内存的话每次重启都会把当天已用的次数清零，额度形同虚设。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

# 项目根目录下的 data/（已在 .gitignore 里忽略）
_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "web_search_usage.json"
)

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
