"""AI_PROXY 配置与统一客户端工厂的单元测试。

运行： .venv/bin/python tests/test_aiproxy.py
"""

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
ok = 0


def check(name: str, cond: bool) -> None:
    global ok
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}")
        raise SystemExit(f"测试失败: {name}")


os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

import nonebot

nonebot.init()

from plugins.ai_chat.config import Config
from plugins.ai_chat.ai_client import make_client

# ---- 1. 配置字段 ----
cfg = Config()
check("ai_proxy 默认为空", cfg.ai_proxy == "")
cfg2 = Config(ai_proxy="http://127.0.0.1:7890")
check("ai_proxy 可被环境变量风格覆盖", cfg2.ai_proxy == "http://127.0.0.1:7890")

# ---- 2. 无代理：返回标准客户端 ----
from openai import AsyncOpenAI

c1 = make_client("sk-test", "https://api.example.com/v1")
check("无代理返回 AsyncOpenAI", isinstance(c1, AsyncOpenAI))
check("无代理 base_url 正确", str(c1.base_url).rstrip("/") == "https://api.example.com/v1")

# ---- 3. 有代理：注入带 proxy 的 httpx2.AsyncClient ----
import httpx2

c2 = make_client("sk-test", "https://api.openai.com/v1", "http://127.0.0.1:7890")
check("有代理返回 AsyncOpenAI", isinstance(c2, AsyncOpenAI))
check("有代理注入了 httpx2.AsyncClient", isinstance(c2._client, httpx2.AsyncClient))
proxied = c2._client
has_proxy = bool(
    getattr(proxied, "_mounts", None)
    or getattr(proxied, "_proxy", None)
    or getattr(proxied, "proxy", None)
)
check("http_client 带代理配置痕迹", has_proxy)

# ---- 4. socks5 代理：未装 socksio 时报清晰错误（提醒用户换 http 端口或补依赖）----
try:
    make_client("sk-a", "https://a.example.com/v1", "socks5://127.0.0.1:1080")
    socksio_missing = False
except ImportError as e:
    socksio_missing = "socksio" in str(e)
check("socks5 缺依赖时报清晰 ImportError", socksio_missing)

print(f"\n共 {ok} 项全部通过")
