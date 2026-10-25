#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""腾讯文档参数（启动器网页 /api/tdocs*）的功能测试。

直接备份/还原真实 .env，起一个临时 HTTP 服务打接口，
全程不碰真实凭证（用假值测试，测完还原）。
"""
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import launcher  # noqa: E402

ENV = launcher.ENV_FILE
# 全新 checkout 没有 data/ 目录，备份文件写不进去会直接崩；顺手记住 .env
# 原本是否存在——不存在时测完要删掉测试产生的 .env，不能留个空文件污染环境
HAD_ENV = ENV.exists()
(ROOT / "data").mkdir(exist_ok=True)
BACKUP = ROOT / "data" / ".env.test-backup"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def http(method, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


shutil.copy2(ENV, BACKUP) if ENV.exists() else BACKUP.write_text("", encoding="utf-8")
PORT = 27771
srv = launcher.start_web(PORT, open_browser=False)
try:
    print("1. GET /api/tdocs（读当前 .env）")
    d = http("GET", "/api/tdocs")
    check("返回 ok", d.get("ok"))
    check("字段齐全", set(d.get("values", {})) == set(launcher.DOCS_ENV_KEYS))

    print("2. POST /api/tdocs（写入假凭证 + 校验其他行保留）")
    before = {
        ln.split("=")[0]: ln
        for ln in BACKUP.read_text(encoding="utf-8").splitlines()
        if "=" in ln and not ln.startswith("#") and ln.strip()
    }
    # 全新环境可能本来就没有 .env（AI_API_KEY 无从「保留」）；只在其原本存在时才比对
    had_api_key = "AI_API_KEY" in before
    api_key_val = before.get("AI_API_KEY", "").split("=", 1)[-1]
    http("POST", "/api/tdocs", {
        "AI_DOCS_CLIENT_ID": "test_client_id_0000000000000000",
        "AI_DOCS_ACCESS_TOKEN": "test_token_xxx",
        "AI_DOCS_OPEN_ID": "test_open_id_0000000000000000",
        "AI_DOCS_SYNC_MODE": "manual",
    })
    env = launcher.read_env()
    check("client_id 写入", env.get("AI_DOCS_CLIENT_ID") == "test_client_id_0000000000000000")
    check("sync_mode 写入", env.get("AI_DOCS_SYNC_MODE") == "manual")
    check("AI_API_KEY 保留", not had_api_key or env.get("AI_API_KEY") == api_key_val, env.get("AI_API_KEY"))
    check("AI_DOCS_ENABLED 等其他键保留", env.get("AI_DOCS_ENABLED", "true") in ("true", "false"))

    print("3. 非法 sync_mode 归一为 whitelist")
    http("POST", "/api/tdocs", {"AI_DOCS_SYNC_MODE": "hack"})
    env = launcher.read_env()
    check("非法值被归一", env.get("AI_DOCS_SYNC_MODE") == "whitelist")

    print("4. POST /api/tdocs/test（假凭证 → 明确报错而非 5xx）")
    d = http("POST", "/api/tdocs/test", {
        "AI_DOCS_CLIENT_ID": "deadbeef" * 4,
        "AI_DOCS_ACCESS_TOKEN": "fake",
        "AI_DOCS_OPEN_ID": "cafe" * 8,
    })
    check("假凭证返回 ok=False", d.get("ok") is False and bool(d.get("error")))

    d = http("POST", "/api/tdocs/test", {"AI_DOCS_CLIENT_ID": "x"})
    check("缺参数友好提示", not d.get("ok") and "Access Token" in (d.get("error") or ""))

    print("5. 清空保存（凭证留空 = 取消文档功能）")
    http("POST", "/api/tdocs", {"AI_DOCS_CLIENT_ID": "", "AI_DOCS_ACCESS_TOKEN": "",
                                "AI_DOCS_OPEN_ID": ""})
    env = launcher.read_env()
    check("凭证已清空", env.get("AI_DOCS_CLIENT_ID") == "")
    check("其他配置仍在", not had_api_key or env.get("AI_API_KEY") == api_key_val, env.get("AI_API_KEY"))

    print("6. save_docs_env 在 .env 不存在时也能建（用临时文件验证）")
    launcher.ENV_FILE = ROOT / "data" / ".env.docs-test-tmp"  # type: ignore[assignment]
    launcher.save_docs_env({"AI_DOCS_CLIENT_ID": "abc"})
    check("无 .env 时创建", launcher.ENV_FILE.exists() and "abc" in launcher.ENV_FILE.read_text())
    launcher.ENV_FILE.unlink(missing_ok=True)
    launcher.ENV_FILE = ENV  # type: ignore[assignment]
finally:
    srv.shutdown()
    if HAD_ENV:
        shutil.copy2(BACKUP, ENV)
    else:
        ENV.unlink(missing_ok=True)
    BACKUP.unlink(missing_ok=True)

bad = [r for r in results if not r[1]]
print(f"\n{'全部通过' if not bad else '有失败'}：{len(results) - len(bad)}/{len(results)}")
sys.exit(1 if bad else 0)
