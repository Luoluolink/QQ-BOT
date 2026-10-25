"""实测 NapCat fetch_custom_face：看账号收藏表情的返回结构。

注意：调试 WS 通道里混着群里实时消息（event），接收时必须跳过
echo 不匹配的消息，否则正式响应被事件挤占，拿不到结果。
"""
import asyncio
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

import websockets

# 从 .env 拿 WS token（ NapCat 临时调试通道没设 token，一般用不上）
env_text = (ROOT / ".env").read_text(encoding="utf-8")
env = dict(
    re.findall(r"^([A-Z_]+)=(.*)$", env_text, re.M)
)
token = env.get("ONEBOT_ACCESS_TOKEN", "")
port = 8098  # NapCat 临时调试用正向 WS（配置里加的 debug server）


async def call(ws, action: str, params: dict, echo: str) -> dict:
    """发请求并等到 echo 匹配的响应，中途跳过群消息等 event。"""
    await ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        msg = json.loads(raw)
        if msg.get("echo") == echo:
            return msg


async def main():
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"ws://127.0.0.1:{port}/"
    async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
        msg = await call(ws, "fetch_custom_face", {"count": 200}, "test1")
        print("status:", msg.get("status"), "retcode:", msg.get("retcode"))
        data = msg.get("data") or []
        print("表情数量:", len(data) if isinstance(data, list) else type(data))
        if isinstance(data, list) and data:
            print("首条结构:", json.dumps(data[0], ensure_ascii=False)[:500])


asyncio.run(main())
