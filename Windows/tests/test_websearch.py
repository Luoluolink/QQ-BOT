"""验证 websearch.py 的额度账本逻辑。

运行： python tests/test_websearch.py
"""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# 本文件在 tests/ 下，项目根目录是它的上一级
SRC = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat" / "websearch.py"

spec = importlib.util.spec_from_file_location("ws_under_test", SRC)
ws = importlib.util.module_from_spec(spec)
sys.modules["ws_under_test"] = ws
spec.loader.exec_module(ws)

TMP = Path(tempfile.mkdtemp(prefix="wstest-"))
STATE = TMP / "web_search_usage.json"

ok = 0
fail = []


def check(name, cond, extra=""):
    global ok
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail.append(name)
        print(f"  [FAIL] {name} {extra}")


def fresh():
    """模拟重启：清内存缓存 + 指向临时文件。"""
    ws._STATE_PATH = STATE
    ws._state = None


# ---------- 1. 响应解析：dict 形态 ----------
print("1. extract_usage 解析")
fresh()
r = {"usage": {"web_search_usage": {"tool_usage": 3, "page_usage": 3}}}
check("dict 形态取到 3 次", ws.extract_usage(r) == (3, 1), ws.extract_usage(r))

r0 = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
check("没有联网字段 = 0 次", ws.extract_usage(r0) == (0, 0), ws.extract_usage(r0))

r1 = {"usage": {"web_search_usage": {"tool_usage": 0}}}
check("tool_usage=0 = 没触发联网", ws.extract_usage(r1) == (0, 0), ws.extract_usage(r1))


# ---------- 2. 响应解析：pydantic 对象形态（SDK 真实返回）----------
class FakeModel:
    """模拟 OpenAI SDK 的 pydantic 模型：自定义字段落在 model_extra。"""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class FakePydantic(FakeModel):
    def __init__(self, model_extra=None, **fields):
        super().__init__(**fields)
        self.model_extra = model_extra or {}


resp_obj = FakeModel(
    usage=FakePydantic(
        prompt_tokens=100,
        model_extra={"web_search_usage": FakeModel(tool_usage=2, page_usage=2)},
    )
)
check("对象形态取到 2 次", ws.extract_usage(resp_obj) == (2, 1), ws.extract_usage(resp_obj))

resp_plain = FakeModel(usage=FakePydantic(prompt_tokens=50))
check("对象无联网字段 = 0 次", ws.extract_usage(resp_plain) == (0, 0), ws.extract_usage(resp_plain))

check("usage 为 None 不炸", ws.extract_usage(FakeModel(usage=None)) == (0, 0))
check("resp 为 None 不炸", ws.extract_usage(None) == (0, 0))
check(
    "tool_usage 是脏字符串也能容错",
    ws.extract_usage({"usage": {"web_search_usage": {"tool_usage": "3"}}}) == (3, 1),
)

# ---------- 3. 记账与持久化 ----------
print("\n2. 记账与持久化")
fresh()
check("初始为 0", ws.used() == 0 and ws.remaining(30) == 30)
check("初始未耗尽", ws.exhausted(30) is False)

ws.record({"usage": {"web_search_usage": {"tool_usage": 3}}})
check("记 3 次后 used=3", ws.used() == 3, ws.used())
check("剩余 27", ws.remaining(30) == 27, ws.remaining(30))
check("rounds=1", ws.rounds() == 1, ws.rounds())

ws.record({"usage": {"web_search_usage": {"tool_usage": 2}}})
check("再记 2 次后 used=5", ws.used() == 5, ws.used())
check("rounds=2", ws.rounds() == 2, ws.rounds())

ws.record({"usage": {"prompt_tokens": 1}})
check("未联网的调用不扣额度", ws.used() == 5, ws.used())
check("未联网的调用不计 rounds", ws.rounds() == 2, ws.rounds())

saved = json.loads(STATE.read_text(encoding="utf-8"))
check("已落盘", saved.get("used") == 5, saved)

fresh()  # 模拟重启
check("重启后额度不丢", ws.used() == 5, ws.used())
check("重启后 rounds 不丢", ws.rounds() == 2, ws.rounds())

# ---------- 4. 耗尽判定 ----------
print("\n3. 耗尽判定")
fresh()
ws.record({"usage": {"web_search_usage": {"tool_usage": 30}}})
check("用满 30 后耗尽", ws.exhausted(30) is True)
check("剩余 0（不为负）", ws.remaining(30) == 0, ws.remaining(30))

fresh()
ws.record({"usage": {"web_search_usage": {"tool_usage": 31}}})
check("超额也判耗尽", ws.exhausted(30) is True)
check("剩余仍是 0", ws.remaining(30) == 0)

fresh()
check("limit=0 直接视为不允许联网", ws.exhausted(0) is True)

# ---------- 5. 重置与跨天 ----------
print("\n4. 重置与跨天")
fresh()
ws.record({"usage": {"web_search_usage": {"tool_usage": 7}}})
ws.reset()
check("reset 后归零", ws.used() == 0 and ws.rounds() == 0)
check("reset 已落盘", json.loads(STATE.read_text(encoding="utf-8"))["used"] == 0)

fresh()
ws.record({"usage": {"web_search_usage": {"tool_usage": 9}}})
ws._state["day"] = "2000-01-01"  # 手动改成昨天
check("跨天后自动归零", ws.used() == 0, ws.used())
check("跨天后 day 更新", ws._load()["day"] == ws._today())

# ---------- 6. 文件损坏容错 ----------
print("\n5. 容错")
fresh()
STATE.write_text("{ 这不是合法 json", encoding="utf-8")
check("状态文件损坏时不炸，从零开始", ws.used() == 0)

fresh()
ws._STATE_PATH = TMP / "no" / "such" / "dir" / "x.json"
check("目录不存在也能记（写入失败静默）", ws.record({"usage": {"web_search_usage": {"tool_usage": 1}}}) == 1)
check("写入失败时内存计数仍正确", ws.used() == 1, ws.used())

# ---------- 7. snapshot ----------
print("\n6. snapshot")
fresh()
ws.record({"usage": {"web_search_usage": {"tool_usage": 4}}})
snap = ws.snapshot(30)
check("snapshot 字段齐全", set(snap) == {"day", "used", "rounds", "limit", "remaining"}, snap)
check("snapshot 数值正确", snap["used"] == 4 and snap["remaining"] == 26, snap)

print(f"\n{'=' * 46}")
print(f"通过 {ok} 项，失败 {len(fail)} 项")
if fail:
    for name in fail:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
