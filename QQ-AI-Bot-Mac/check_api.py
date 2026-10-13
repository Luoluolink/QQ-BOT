"""一键检测大模型 API 配置是否可用。

用法：
    python check_api.py
    python check_api.py --base https://open.bigmodel.cn/api/paas/v4 --key xxx --model glm-5.3-flash

不带参数时读取同目录下的 .env。
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def read_env(path: Path) -> dict:
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="检测大模型 API 是否可用")
    parser.add_argument("--base", help="接口地址，如 https://open.bigmodel.cn/api/paas/v4")
    parser.add_argument("--key", help="API Key")
    parser.add_argument("--model", help="模型 ID，如 glm-5.3-flash")
    parser.add_argument("--effort", help="思考强度 low/high/max，留空则不传该参数")
    parser.add_argument("--no-think", action="store_true",
                        help="发送 thinking:{type:disabled} 关闭思考")
    parser.add_argument("--think", action="store_true",
                        help="强制不关闭思考（覆盖 .env 里的 AI_DISABLE_THINKING）")
    args = parser.parse_args()

    env = read_env(Path(__file__).with_name(".env"))
    base = (args.base or env.get("AI_API_BASE", "")).rstrip("/")
    key = args.key or env.get("AI_API_KEY", "")
    model = args.model or env.get("AI_MODEL", "")
    effort = args.effort if args.effort is not None else env.get("AI_REASONING_EFFORT", "")
    disable_thinking = str(env.get("AI_DISABLE_THINKING", "")).lower() in ("1", "true", "yes")
    if args.no_think:
        disable_thinking = True
    if args.think:
        disable_thinking = False

    if not base or not key or not model:
        print("缺少配置：需要 AI_API_BASE / AI_API_KEY / AI_MODEL")
        print("请先编辑 .env，或用 --base / --key / --model 传入")
        return 1

    if "请填写" in base or "你的" in key or "xxx" in base.lower():
        print("配置里还有占位符没替换，请先编辑 .env")
        return 1

    print(f"接口地址 : {base}")
    print(f"模型 ID  : {model}")
    print(f"Key      : {key[:10]}...{key[-4:]}")
    print(f"思考模式 : {'关闭 (thinking.type=disabled)' if disable_thinking else '开启（默认）'}")
    print(f"思考强度 : {effort or '(不传)'}")
    print("-" * 52)

    request_body = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        # 给足额度：思考模型会先消耗思考 token，额度太小正文会是空的
        "max_tokens": 500,
    }
    if disable_thinking:
        request_body["thinking"] = {"type": "disabled"}
    if effort:
        request_body["reasoning_effort"] = effort

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(request_body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - started

        message = data["choices"][0]["message"]
        reply = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = details.get("reasoning_tokens")

        print("连通成功")
        print(f"耗时      : {elapsed:.2f}s")
        if reasoning_tokens is not None:
            print(f"思考 token: {reasoning_tokens}")
        print(f"token 用量: 输入 {usage.get('prompt_tokens', '?')} / "
              f"输出 {usage.get('completion_tokens', '?')}")

        if reply:
            print(f"模型回复  : {reply}")
        else:
            print("模型回复  : (空)")
            print()
            print("正文是空的，说明额度被思考过程占满了。三种处理方式：")
            print("  1) 在 .env 里设置 AI_DISABLE_THINKING=true 直接关掉思考")
            print("  2) 在 .env 里设置 AI_REASONING_EFFORT=low 降低思考开销")
            print("  3) 调大 AI_MAX_TOKENS")
            if reasoning:
                print(f"\n（它其实思考了这么多：{reasoning[:80]}...）")
        return 0

    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"失败：HTTP {exc.code}（耗时 {time.time() - started:.2f}s）")
        print(detail[:500])
        print()
        if exc.code == 400 and disable_thinking:
            print("400 且刚开了关思考：这个模型可能不认 thinking 字段，")
            print("      去掉 .env 里的 AI_DISABLE_THINKING 再试。")
        elif exc.code == 401:
            print("401 = Key 不被这个地址认可。请确认 Key 是从哪个平台申请的，")
            print("      然后把该平台的接口地址填到 .env 的 AI_API_BASE。")
        elif exc.code == 404:
            print("404 = 模型 ID 不对，或接口路径少了 /v1。")
        elif exc.code == 429:
            print("429 = 触发了限流或余额不足。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{exc!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
