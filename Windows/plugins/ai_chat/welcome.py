"""新成员入群欢迎。

两个设计取舍：

1. **用模型生成，而不是写死几句话。**
   写死的欢迎语第二天就腻了，而成员进出是长期高频事件。
   代价是每次多花约 ¥0.0002 和 1~2 秒，可以接受；
   模型挂了则自动退回模板，不会不吭声。

2. **必须限流。**
   有人一次性拉一群人进群时，机器人如果挨个欢迎，就是短时间内连发多条消息 ——
   这是 QQ 风控最敏感的行为之一。所以加了「同群欢迎冷却」和「每日欢迎上限」两道闸。

注意：入群欢迎是**主动发消息**，和「只在被 @ 时响应」的保守策略不同，
客观上会增加被风控盯上的概率。介意的话把 AI_WELCOME_ENABLED 关掉。
"""

from __future__ import annotations

import random
from datetime import date

# 同群两次欢迎的最小间隔（秒）。这期间又有人入群就跳过，
# 避免「一次拉一群人」时机器人连发一串消息。
DEFAULT_COOLDOWN = 15.0

# 每个群每天最多欢迎几次
DEFAULT_DAILY_LIMIT = 20

# 模型生成失败时的兜底欢迎语
TEMPLATES = (
    "哦呀，新的求知者吗。欢迎来到我的图书馆，{name}。",
    "欢迎，{name}。这座图书馆又多了一册尚未读过的书呢。",
    "{name}，你带来了什么新知识吗？我可是很期待的。",
    "又一位访客。进来吧，{name}——这里的每一页都值得品尝。",
)


def pick_template(name: str) -> str:
    return random.choice(TEMPLATES).format(name=name)


def build_messages(
    date_line: str, system_prompt: str, name: str, qq: int
) -> list[dict]:
    """拼出生成欢迎语用的对话。

    单独写一段 user 指令而不是把要求塞进人设里，是为了不动人设本身 ——
    人设是长期资产，欢迎只是其中一种用法。
    """
    return [
        {
            "role": "system",
            "content": f"{date_line}\n\n{system_prompt}",
        },
        {
            "role": "user",
            "content": (
                f"群里刚有一位新成员加入，群名片是「{name}」（QQ {qq}）。\n"
                "请用你的口吻欢迎他，只回一句话，不超过 40 字。\n"
                "新成员对你来说是一份新的知识，可以体现这份期待。\n"
                "不要用「欢迎加入本群」这种客套话，也不要输出 Markdown 或列表符号。"
            ),
        },
    ]


# ---------------------------------------------------------------- 限流

_last_welcome: dict[int, float] = {}
_daily_count: dict[int, int] = {}
_day: str = ""


def _roll_day() -> None:
    global _day
    today = date.today().isoformat()
    if today != _day:
        _day = today
        _daily_count.clear()
        _last_welcome.clear()


def check(group_id: int, now: float, cooldown: float, daily_limit: int) -> str:
    """能不能欢迎这个群的新人。返回空串表示可以，否则返回不能的原因。"""
    _roll_day()
    if daily_limit > 0 and _daily_count.get(group_id, 0) >= daily_limit:
        return f"本群今日欢迎次数已达上限 {daily_limit}"
    last = _last_welcome.get(group_id, 0.0)
    if cooldown > 0 and now - last < cooldown:
        return f"距上次欢迎不足 {cooldown:g} 秒"
    return ""


def mark(group_id: int, now: float) -> None:
    """记一次欢迎。"""
    _roll_day()
    _last_welcome[group_id] = now
    _daily_count[group_id] = _daily_count.get(group_id, 0) + 1


def stats(group_id: int) -> dict:
    _roll_day()
    return {
        "day": _day,
        "count": _daily_count.get(group_id, 0),
        "last": _last_welcome.get(group_id, 0.0),
    }
