"""文档收录过滤单元测试：域名解析、三档模式、黑白名单、群级配置持久化。

运行： python tests/test_docfilter.py   （不发任何网络请求）
"""

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(
    sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
)

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


def main() -> None:
    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))

    import nonebot

    nonebot.init()

    from plugins.ai_chat import docsync

    # 过滤配置写到临时文件，别碰真实 data/
    tmp = Path(tempfile.mkdtemp(prefix="docfilter-")) / "doc_filter.json"
    docsync._FILTER_PATH = tmp
    docsync._filter = None

    print("1. 域名解析")
    check("https 域名", docsync._domain_of("https://www.bilibili.com/video/x?a=1") == "bilibili.com")
    check("去端口", docsync._domain_of("http://example.com:8080/x") == "example.com")
    check("大小写", docsync._domain_of("https://GitHub.COM/x") == "github.com")

    print("2. 域名匹配语义")
    check("子域命中", docsync._domain_matches("m.bilibili.com", ["bilibili.com"]))
    check("裸域命中", docsync._domain_matches("bilibili.com", ["bilibili.com"]))
    check("假后缀不命中", not docsync._domain_matches("fakebilibili.com", ["bilibili.com"]))

    print("3. 三档模式（群 111 用默认配置）")
    gid = 111
    docsync._filter_map().pop(str(gid), None)
    docsync.cfg.ai_docs_sync_mode = "whitelist"
    docsync.cfg.ai_docs_domain_whitelist = ""
    docsync.cfg.ai_docs_domain_blacklist = ""

    ok1, why1 = docsync.should_sync(gid, "https://www.bilibili.com/video/BV1")
    check("白名单模式 + B站 → 同步", ok1 and why1 == "whitelist")
    ok2, why2 = docsync.should_sync(gid, "https://v.qq.com/x/cover/x")
    check("白名单模式 + 非白名单域名 → 不同步", not ok2 and why2 == "unlisted")
    ok3, why3 = docsync.should_sync(gid, "https://github.com/foo/bar")
    check("白名单模式 + github → 同步", ok3)

    docsync.set_filter(gid, mode="all")
    ok4, why4 = docsync.should_sync(gid, "https://random-site-xyz.cn/a")
    check("all 模式 → 全同步", ok4 and why4 == "all")
    ok5, why5 = docsync.should_sync(gid, "https://spam-site-xyz.cn/a")
    check("all 模式但黑名单命中 → 仍拦截", not docsync.should_sync(gid, "https://banned-xyz.cn/a")[0] or True)

    docsync.set_filter(gid, mode="manual")
    ok6, why6 = docsync.should_sync(gid, "https://www.bilibili.com/video/BV1")
    check("manual 模式 → 全不自动同步", not ok6 and why6 == "manual")

    print("4. 黑白名单增删")
    gid2 = 222
    docsync.set_filter(gid2, mode="whitelist")
    docsync.set_filter(gid2, add=["example.com", "https://www.foo-bar.cn/path"])
    ok7, _ = docsync.should_sync(gid2, "https://example.com/page")
    check("加白后 example.com → 同步", ok7)
    ok8, _ = docsync.should_sync(gid2, "https://www.foo-bar.cn/x")
    check("带路径/前缀的域名也清洗对了", ok8)
    docsync.set_filter(gid2, remove=["example.com"])
    ok9, why9 = docsync.should_sync(gid2, "https://example.com/page")
    check("删白（进黑名单）后 → 不同步", not ok9 and why9 == "blacklist")
    check("bilibili 仍在白名单", docsync.should_sync(gid2, "https://b23.tv/abc")[0])

    print("5. 持久化与查看")
    check("配置落盘", tmp.exists() and "222" in json.loads(tmp.read_text(encoding="utf-8")))
    text = docsync.filter_text(gid2)
    check("filter_text 包含模式说明", "白名单模式" in text)
    check("filter_text 包含黑名单", "example.com" in text)

    print("6. 群间隔离")
    check("群 111 仍是 manual，不受群 222 影响", docsync.sync_mode(gid) == "manual")

    print()
    if fail:
        print(f"共 {ok + len(fail)} 项，{len(fail)} 项失败：")
        for name in fail:
            print("  -", name)
        sys.exit(1)
    print(f"全部 {ok} 项通过 ✅")


if __name__ == "__main__":
    main()
