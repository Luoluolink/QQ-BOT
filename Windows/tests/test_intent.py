"""意图路由单元测试：预筛、JSON 解析、路由模型调用、动作分发。

不 mock 插件代码，把模型接口指向本地假服务器，验证真实路径。
运行： python tests/test_intent.py   （不花真钱）
"""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(
    sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
)
PORT = 18079

# 假服务器返回的意图 JSON（每个用例改这里）
REPLY = {"content": '{"action":"chat"}'}
REQUESTS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(body)
        payload = {
            "id": "fake",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": REPLY["content"]},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
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

    from plugins.ai_chat import intent
    from openai import AsyncOpenAI

    intent._client = AsyncOpenAI(api_key="fake", base_url=f"http://127.0.0.1:{PORT}/v1")

    print("1. 正则预筛")
    check(
        "绑定文档链接 → 命中",
        intent.looks_actionable("@bot 绑定这个文档 https://docs.qq.com/doc/abc123"),
    )
    check("帮我记住生日 → 命中", intent.looks_actionable("帮我记住张三的生日"))
    check("找图 → 命中", intent.looks_actionable("帮我找一下那张雪山的图"))
    check("人设 → 命中", intent.looks_actionable("换个性格吧，傲娇一点"))
    check(
        "纯闲聊 → 不命中（零开销）",
        not intent.looks_actionable("今天天气怎么样"),
    )
    check(
        "包含「文档」的纯提问 → 不命中（避免误伤聊天）",
        not intent.looks_actionable("文档是什么意思"),
    )

    print("2. JSON 提取")
    check(
        "裸 JSON",
        (intent._extract_json('{"action":"chat"}') or {}).get("action") == "chat",
    )
    check(
        "带代码围栏",
        (intent._extract_json('```json\n{"action":"bind_doc","args":{"url":"u"}}\n```') or {})
        .get("args", {})
        .get("url")
        == "u",
    )
    check("前后有废话", (intent._extract_json('好的！{"action":"chat"} 以上') or {}).get("action") == "chat")
    check("非 JSON", intent._extract_json("我想想……") is None)

    print("3. 路由模型调用")
    REPLY["content"] = '{"action":"bind_doc","args":{"url":"https://docs.qq.com/doc/abc"}}'
    route = await intent._classify("把这个文档绑到群里 https://docs.qq.com/doc/abc")
    check("action 正确", route["action"] == "bind_doc")
    check("args 正确", route["args"].get("url", "").endswith("/abc"))
    check("prompt 里列出了注册的动作", "bind_doc" in REQUESTS[-1]["messages"][0]["content"])
    check("prompt 里列出了外挂动作", "set_persona" in REQUESTS[-1]["messages"][0]["content"])

    REPLY["content"] = "我决定：\n```json\n{\"action\":\"chat\"}\n```"
    route = await intent._classify("今天天气怎么样")
    check("围栏+废话也能解析为 chat", route["action"] == "chat")

    print("4. 动作分发（真实执行器；凭证已配置时会真实请求腾讯 API，失败也是友好文案）")
    bind_reply = await intent._exec_bind_doc(
        None, type("E", (), {"group_id": 1})(), {"url": "https://docs.qq.com/doc/abc"}, True
    )
    check(
        "bind_doc 返回友好文案（未配置/绑定失败二选一）",
        isinstance(bind_reply, str) and ("还没配置" in bind_reply or "绑定失败" in bind_reply),
        bind_reply,
    )

    # try_handle 端到端：chat 路由 → False（消息继续走聊天）
    REPLY["content"] = '{"action":"chat"}'
    handled = await intent.try_handle(
        None, type("E", (), {"group_id": 1})(), "今天天气怎么样", False
    )
    check("chat 路由 → try_handle 返回 False", handled is False)

    # 预筛不命中 → 直接 False，不调模型
    n_req = len(REQUESTS)
    handled = await intent.try_handle(
        None, type("E", (), {"group_id": 1})(), "纯闲聊内容", False
    )
    check("预筛不命中 → False 且没调模型", handled is False and len(REQUESTS) == n_req)

    # 未知动作 → False
    REPLY["content"] = '{"action":"launch_missile"}'
    handled = await intent.try_handle(
        None, type("E", (), {"group_id": 1})(), "记住点什么吧", False
    )
    check("未知动作 → False（退回聊天）", handled is False)

    print()
    if fail:
        print(f"共 {ok + len(fail)} 项，{len(fail)} 项失败：")
        for name in fail:
            print("  -", name)
        sys.exit(1)
    print(f"全部 {ok} 项通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
