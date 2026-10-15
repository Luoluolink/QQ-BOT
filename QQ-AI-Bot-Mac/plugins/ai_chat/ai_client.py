"""统一的模型客户端工厂：所有调用 OpenAI 兼容接口的地方都从这里拿客户端。

集中一处是为了让代理配置（AI_PROXY）只作用于模型请求本身——
机器人的其他出站请求（抓网页标题、QQ/NapCat 连接）不该被牵连。

用法：
    from .ai_client import make_client
    client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
"""

from __future__ import annotations

from openai import AsyncOpenAI


def make_client(api_key: str, base_url: str, proxy: str = "") -> AsyncOpenAI:
    """创建 AsyncOpenAI 客户端；proxy 非空时仅给模型请求挂代理。

    传入自定义 http_client 后，openai SDK 不会再套用它的默认超时，
    这里手动对齐 SDK 默认值（总超时 600s / 连接 10s）——
    不然 httpx2 自己默认的 5 秒总超时会把长回复直接掐死。

    典型值（.env 里 AI_PROXY=）：
        http://127.0.0.1:7890     Clash 系混合端口（推荐，零额外依赖）
        socks5://127.0.0.1:1080   v2rayN 系；需要 .venv 里补装 socksio 才能用
    """
    if not proxy:
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    import httpx2

    http = httpx2.AsyncClient(
        proxy=proxy,
        timeout=httpx2.Timeout(600.0, connect=10.0),
    )
    return AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http)
