"""群情绪状态机：给吉普莉尔一个会呼吸的心情。

每组（每个私聊对象）一个情绪槽：{情绪类别, 强度 0-100, 原因, 更新时间}。

- bump()：事件触发。被夸 → 开心，被怼 → 不耐烦，被戳 → 害羞……
- describe()：给系统提示的情绪段落。冷静时返回空串（默认人设就是平静态），
  有情绪时要求回复语气带出状态，但明确「渐变、别夸张」。
- 衰减：读取时按时间向平静值回落，情绪是会过劲的，不是开关。
- 持久化 data/mood.json，重启不失忆。

设计参考 MaiBot 的情绪系统思路：情绪是连续量不是标签开关，
由具体事件推动、随时间衰减、注入提示词影响表达而非决定内容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 情绪 → 展示名。value 是 0-100 的强度，40 附近算「平静基准线」。
EMOTION_LABELS = {
    "happy": "开心",
    "annoyed": "不耐烦",
    "shy": "害羞",
    "touched": "在意",
    "bored": "无聊",
    "excited": "兴奋",
}
NEUTRAL = 40          # 平静基准线：衰减的目标值
CALM_THRESHOLD = 42   # 强度低于此值视为平静，不再注入提示
DECAY_PER_HOUR = 18.0  # 每小时向基准线衰减的强度

_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "mood.json"
_state: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _state
    if _state is None:
        try:
            raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            _state = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _state = {}
    return _state


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(_state or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # 落盘失败只影响重启后失忆，不抛出


def _decayed(slot: dict, now: float) -> dict:
    """按时间衰减强度。平静化只在「事件过去一阵」后才发生——
    刚触发的小情绪要给后续事件累积的机会，不能立刻清零。"""
    value = slot.get("value", 0)
    emotion = slot.get("emotion", "calm")
    ts = slot.get("ts", now)
    if emotion == "calm" or value <= 0:
        return {"emotion": "calm", "value": 0, "reason": "", "ts": now}
    elapsed_h = max(0.0, (now - ts) / 3600)
    value = value - DECAY_PER_HOUR * elapsed_h
    if value < CALM_THRESHOLD and now - ts > 600:
        emotion, value = "calm", 0  # 事件过去 10 分钟后还弱于阈值 → 平静
    value = max(0.0, value)
    return {"emotion": emotion, "value": round(value, 1),
            "reason": slot.get("reason", ""), "ts": ts}


def bump(group_key: str | int, emotion: str, amount: int, reason: str) -> None:
    """事件触发情绪变化。同类情绪累加，不同情绪则以新事件为主（旧情绪过劲让位）。"""
    if emotion not in EMOTION_LABELS or amount <= 0:
        return
    now = time.time()
    state = _load()
    key = str(group_key)
    slot = _decayed(state.get(key, {"emotion": "calm", "value": 0, "reason": "", "ts": now}), now)
    if slot["emotion"] == emotion:
        value = min(100, slot["value"] + amount)
    else:
        value = min(100, amount)  # 新情绪事件直接以自身强度登场
    state[key] = {"emotion": emotion, "value": value, "reason": reason, "ts": now}
    _save()


def describe(group_key: str | int) -> str:
    """给系统提示的情绪段落。平静/无情绪返回空串（默认人设即平静态）。"""
    now = time.time()
    state = _load()
    key = str(group_key)
    slot = _decayed(state.get(key, {"emotion": "calm", "value": 0, "reason": "", "ts": now}), now)
    state[key] = slot
    _save()
    if slot["emotion"] == "calm":
        return ""
    label = EMOTION_LABELS.get(slot["emotion"], slot["emotion"])
    level = "有点" if slot["value"] < 65 else "相当"
    return (
        f"【当前情绪】你现在{level}{label}（{int(slot['value'])}/100）——{slot['reason']}。"
        "回复的语气要自然带出这个状态：开心就热络些，不耐烦就懒洋洋带刺，害羞就嘴硬，"
        "在意就放软。情绪是渐变的，别夸张演绎，一两处措辞体现就够。"
    )


def react_to_text(group_key: str | int, text: str) -> None:
    """根据消息内容做规则触发（便宜、即时、不用模型）。

    只匹配明显信号；细微情绪交给模型自己从人设里带出来。
    """
    t = text.strip()
    if not t:
        return
    if any(w in t for w in ("厉害", "牛", "太强", "可爱", "好看", "聪明", "大佬", "膜", "好棒", "真棒")):
        bump(group_key, "happy", 12, "被夸了")
    elif any(w in t for w in ("谢谢", "感谢", "thx", "3q", "多谢")):
        bump(group_key, "happy", 8, "被道谢了")
    elif any(w in t for w in ("傻", "笨", "滚", "废物", "闭嘴", "垃圾", "烦人", "蠢", "去死")):
        bump(group_key, "annoyed", 15, "被怼了")
    elif any(w in t for w in ("想你", "陪我", "想你了", "抱抱")):
        bump(group_key, "happy", 8, "被需要着")
    elif any(w in t for w in ("难过", "呜呜", "难受", "哭了", "emo")):
        bump(group_key, "touched", 10, "有人心情不好，得看着点")
