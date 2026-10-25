"""连续对话窗口：@ 过机器人的人，短时间内不 @ 直接接着说也能得到回复。

状态就一个字典：谁在哪个群正和机器人说话、还剩几句额度。
- activate：机器人成功回复某人后调用，开/续一个窗口
- hit：判断某条消息算不算「接着说」，算就扣一次额度
- active：只读判断（proactive 用它避开和正常回复撞车）

额度用完或超时后回到老规矩：想继续说就再 @ 一次，防止被无限接话。
"""

from __future__ import annotations

import time
from typing import Dict, Tuple

_slots: Dict[Tuple[int, int], Dict[str, float]] = {}


def activate(group_id: int, user_id: int, window: int, turns: int) -> None:
    """机器人回复了这个人：开/续一个对话窗口。"""
    _slots[(group_id, user_id)] = {
        "expires": time.time() + max(30, window),
        "turns": max(1, turns),
    }


def hit(group_id: int, user_id: int) -> bool:
    """这条消息算不算「接着说」。算就扣一次额度，扣完关窗。"""
    slot = _slots.get((group_id, user_id))
    if not slot:
        return False
    if time.time() > slot["expires"] or slot["turns"] <= 0:
        _slots.pop((group_id, user_id), None)
        return False
    slot["turns"] -= 1
    if slot["turns"] <= 0:
        _slots.pop((group_id, user_id), None)
    return True


def active(group_id: int, user_id: int) -> bool:
    """只读判断：这个人是否正在和机器人对话中（不扣额度）。"""
    slot = _slots.get((group_id, user_id))
    return bool(slot and time.time() <= slot["expires"] and slot["turns"] > 0)
