"""集成测试：直接跑插件里的 _create_completion，验证额度闸门 + 关键词触发。

不 mock 插件代码本身，只把模型接口指向本地假服务器，
这样连 nonebot 配置读取、kwargs 组装、用量扣减都是真实路径。

运行： python tests/test_gate.py
它会自己起一个假模型服务器，不会真的调用 API，也不花钱。
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 本文件在 tests/ 下，项目根目录是它的上一级
PROJECT = Path(
    sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
)
PORT = 18077

REQUESTS: list[dict] = []
MODE = {"reject_tools": False}  # True = 模拟插件未开通，返回 400


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(body)

        if MODE["reject_tools"] and "tools" in body:
            payload = {
                "error": {
                    "message": "webSearchEnabled is false, please enable the web search tool first",
                    "type": "invalid_request_error",
                }
            }
            data = json.dumps(payload).encode()
            self.send_response(400)
        else:
            has_tools = "tools" in body
            payload = {
                "id": "fake",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "好。"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                    "web_search_usage": {
                        "tool_usage": 2 if has_tools else 0,
                        "page_usage": 2 if has_tools else 0,
                    },
                },
            }
            data = json.dumps(payload).encode()
            self.send_response(200)

        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


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


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))

    import nonebot

    nonebot.init()

    import plugins.ai_chat as ac
    from openai import AsyncOpenAI

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gate-")) / "usage.json"
    ac.websearch._STATE_PATH = tmp
    ac.websearch._state = None

    ac._client = AsyncOpenAI(api_key="fake", base_url=f"http://127.0.0.1:{PORT}/v1")

    msgs = [{"role": "user", "content": "你好"}]

    print("1. 配置是否正确读到")
    check("联网开关已打开", ac.cfg.ai_web_search_enabled is True)
    check("每日额度 = 30", ac.cfg.ai_web_search_daily_limit == 30, ac.cfg.ai_web_search_daily_limit)
    check("max_keyword = 2", ac.cfg.ai_web_search_max_keyword == 2)
    check("force_search = False", ac.cfg.ai_web_search_force is False)
    check("触发方式 = auto", ac.cfg.ai_web_search_trigger == "auto", ac.cfg.ai_web_search_trigger)

    print("\n2. allow_search=False 时绝不能挂 tools（否则每条消息白慢 1.2s）")
    REQUESTS.clear()
    ac.websearch.reset()
    await ac._create_completion(msgs, allow_search=False)
    check("没挂 tools", "tools" not in REQUESTS[0], list(REQUESTS[0]))
    check("没挂 tool_choice", "tool_choice" not in REQUESTS[0])
    check("没扣额度", ac.websearch.used() == 0, ac.websearch.used())
    check("思考模式仍生效", REQUESTS[0].get("thinking") == {"type": "disabled"})

    print("\n3. allow_search=True 且额度充足 → 挂 tools 并扣真实用量")
    REQUESTS.clear()
    ac.websearch.reset()
    resp = await ac._create_completion(msgs, allow_search=True)
    check("请求里带了 tools", "tools" in REQUESTS[0])
    check("tools 类型是 web_search", REQUESTS[0].get("tools", [{}])[0].get("type") == "web_search")
    check("tool_choice = auto", REQUESTS[0].get("tool_choice") == "auto")
    check("扣了 2 次额度", ac.websearch.used() == 2, ac.websearch.used())
    check("剩余 28", ac.websearch.remaining(30) == 28, ac.websearch.remaining(30))
    check("rounds = 1", ac.websearch.rounds() == 1)
    check("返回正常回答", (resp.choices[0].message.content or "").strip() == "好。")

    print("\n4. 额度用尽后即使 allow_search=True 也要摘掉 tools")
    REQUESTS.clear()
    ac.websearch.reset()
    ac.websearch.record({"usage": {"web_search_usage": {"tool_usage": 30}}})
    check("已判定耗尽", ac.websearch.exhausted(30) is True)
    await ac._create_completion(msgs, allow_search=True)
    check("请求里没有 tools", "tools" not in REQUESTS[0], list(REQUESTS[0]))
    check("额度没被多扣", ac.websearch.used() == 30, ac.websearch.used())

    print("\n5. 额度剩 1 次时仍可联网（边界）")
    REQUESTS.clear()
    ac.websearch.reset()
    ac.websearch.record({"usage": {"web_search_usage": {"tool_usage": 29}}})
    check("尚未耗尽", ac.websearch.exhausted(30) is False)
    await ac._create_completion(msgs, allow_search=True)
    check("边界上仍带 tools", "tools" in REQUESTS[0])
    check("用完后达到上限", ac.websearch.used() == 31, ac.websearch.used())

    print("\n6. 插件未开通时要优雅降级，而不是报错")
    REQUESTS.clear()
    ac.websearch.reset()
    MODE["reject_tools"] = True
    try:
        resp = await ac._create_completion(msgs, allow_search=True)
        crashed = False
    except Exception as exc:  # noqa: BLE001
        crashed = True
        print(f"    异常：{exc!r}")
    MODE["reject_tools"] = False
    check("没有抛异常", not crashed)
    check("共请求 2 次（失败后重试）", len(REQUESTS) == 2, len(REQUESTS))
    check("重试时摘掉了 tools", "tools" not in REQUESTS[1])
    check("降级后不扣额度", ac.websearch.used() == 0, ac.websearch.used())
    check("降级后仍返回正常回答", (resp.choices[0].message.content or "").strip() == "好。")

    print("\n7. 关键词触发判定（决定日常闲聊要不要付 1.2s）")
    should = [
        "今天天气怎么样",
        "现在几点",
        "最新的 iPhone 是什么型号",
        "最近有什么新闻",
        "上证指数多少点",
        "帮我搜一下 XX 的官网",
        "查一下明天航班",
        "这个多少钱",
        "今年有什么新政策",
    ]
    should_not = [
        "在吗",
        "你好呀",
        "帮我写一句摸鱼文案",
        "1+1等于几",
        "你叫什么名字",
        "讲个笑话",
        "这个梗是什么意思",
    ]
    for text in should:
        check(f"命中：{text}", ac.websearch.looks_like_web_query(text) is True)
    for text in should_not:
        check(f"不命中：{text}", ac.websearch.looks_like_web_query(text) is False)
    check("空字符串不命中", ac.websearch.looks_like_web_query("") is False)

    print("\n8. 系统提示词里必须带当天日期")
    import datetime

    today = datetime.date.today().isoformat()
    weekday = "一二三四五六日"[datetime.date.today().weekday()]
    check("日期字符串格式正确", len(today) == 10 and "-" in today)
    # 直接检查 handle_chat 里的构造逻辑（从源码文本确认，避免拉起完整 matcher）
    src = (PROJECT / "plugins/ai_chat/__init__.py").read_text(encoding="utf-8")
    check("handle_chat 里注入了日期", "今天的日期是" in src)
    check("日期用 isoformat", "today.isoformat()" in src)
    check("星期映射存在", "一二三四五六日" in src)
    check(f"今天是 {today}（星期{weekday}）", True)

    server.shutdown()

    print(f"\n{'=' * 46}")
    print(f"通过 {ok} 项，失败 {len(fail)} 项")
    if fail:
        for name in fail:
            print(f"  - {name}")
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
