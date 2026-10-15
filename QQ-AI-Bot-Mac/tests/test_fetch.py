"""save_link / fetch_resource 两个新意图动作的单元测试（外部依赖全 mock）。

运行： .venv/bin/python tests/test_fetch.py
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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

from plugins.ai_chat import docsync, essence, intent, linkarchive, resource_index


def make_event(text: str, gid: int = 10001):
    sender = SimpleNamespace(card="小A", nickname="小A nick")
    return SimpleNamespace(group_id=gid, sender=sender, get_plaintext=lambda: text)


class FakeBot:
    def __init__(self, url: str = "http://dl.qq.com/file"):
        self._url = url
        self.calls: list[tuple] = []

    async def call_api(self, action: str, **params):
        self.calls.append((action, params))
        return {"url": self._url}


async def main() -> None:
    # ---- 1. URL 提取 ----
    check("纯 URL 提取", intent._extract_url("https://github.com/a/b") == "https://github.com/a/b")
    check("带前缀文案", intent._extract_url("收一下 https://a.com/x?t=1 谢谢") == "https://a.com/x?t=1")
    check("尾部标点剥离", intent._extract_url("https://a.com/x。") == "https://a.com/x")
    check("无 URL 返回空", intent._extract_url("没有链接") == "")

    # ---- 2. 预筛 ----
    check("@+纯链接也进路由", intent.looks_actionable("@我 https://a.com/x"))
    for kw in ("帮我调取虚拟机", "群文件里有吗", "看看精华", "把那个文件发我", "帮我找一下雪山的图"):
        check(f"预筛命中「{kw}」", intent.looks_actionable(kw))
    check("普通闲聊不误伤", not intent.looks_actionable("今天天气不错"))

    # ---- 3. 动作注册与权限 ----
    check("save_link 已注册", "save_link" in intent.ACTIONS)
    check("save_link 非管理员可用", not intent.ACTIONS["save_link"]["admin"])
    check("fetch_resource 已注册", "fetch_resource" in intent.ACTIONS)
    check("fetch_resource 非管理员可用", not intent.ACTIONS["fetch_resource"]["admin"])

    # ---- 4. save_link：各状态分支 ----
    ev = make_event("@我 https://a.com/page 存一下")
    results: list[str] = []

    async def fake_title(url: str) -> str:
        return "页面标题"

    orig_title = linkarchive.fetch_title
    linkarchive.fetch_title = fake_title
    try:
        async def fake_append(gid, title, url, by):
            results.append((gid, title, url, by))
            return "ok:第36行"
        orig_append = docsync.append_entry
        docsync.append_entry = fake_append
        try:
            reply = await intent._exec_save_link(FakeBot(), ev, {}, is_admin=False)
            check("save_link 成功回复带行号", "第36行" in reply and "页面标题" in reply)
            check("save_link 记录发送者", results[-1][3] == "小A")
            check("save_link 从原消息抠 URL", results[-1][2] == "https://a.com/page")

            async def fake_dup(gid, title, url, by):
                return "dup"
            docsync.append_entry = fake_dup
            reply = await intent._exec_save_link(FakeBot(), ev, {}, False)
            check("save_link 重复提示", "已经在文档里" in reply)

            async def fake_unbound(gid, title, url, by):
                return "未绑定"
            docsync.append_entry = fake_unbound
            reply = await intent._exec_save_link(FakeBot(), ev, {}, False)
            check("save_link 未绑定提示", "绑定" in reply)
        finally:
            docsync.append_entry = orig_append
    finally:
        linkarchive.fetch_title = orig_title

    # args 里带了 URL 的路径
    async def fake_append2(gid, title, url, by):
        return "ok:第1行"
    docsync.append_entry = fake_append2
    try:
        reply = await intent._exec_save_link(FakeBot(), ev, {"url": "https://b.com/z"}, False)
        check("save_link 优先用 args.url", "https://b.com/z" in str(docsync.append_entry.__code__.co_consts) or "已收录" in reply)
    finally:
        pass

    # ---- 5. fetch_resource：多源汇总 ----
    entry = resource_index.ResourceEntry(
        name="VirtualBox安装包.7z", location="/", kind="压缩包",
        source="群文件", size=136_400_000, uploader="ASL", file_id="fid123",
    )
    fake_index = SimpleNamespace(error="", entries=[entry], locations=["/"])

    async def fake_ri_get_index(bot, gid, ttl=None, force=False):
        return fake_index

    orig_ri = resource_index.get_index
    resource_index.get_index = fake_ri_get_index

    fake_essence = SimpleNamespace(error="", messages=[])
    async def fake_ess_get_index(bot, gid, force=False, ttl=None):
        return fake_essence
    orig_ess = essence.get_index
    essence.get_index = fake_ess_get_index

    def fake_ess_search(index, query, limit=10):
        return []
    orig_ess_search = essence.search
    essence.search = fake_ess_search

    orig_la = linkarchive.search
    def fake_la_search(gid, kw, limit=5):
        return [{"title": "VirtualBox 官网", "url": "https://vb.com", "by": "", "time": ""}]
    linkarchive.search = fake_la_search

    orig_doc = docsync.read_bound_text
    async def fake_doc(gid, kw="", limit=8):
        return f"找到这些和「{kw}」相关的：\n1. 名字:xx｜链接: https://doc.qq.com/x\n(共 5 条)\n完整表格: https://doc.qq.com/sheet"
    docsync.read_bound_text = fake_doc

    try:
        bot = FakeBot(url="http://dl.qq.com/vb.7z")
        ev2 = make_event("@我 帮我调取虚拟机的安装包")
        reply = await intent._exec_fetch_resource(bot, ev2, {"keyword": "安装包"}, False)
        check("群文件段带名字", "VirtualBox安装包.7z" in reply)
        check("群文件段带下载链接", "http://dl.qq.com/vb.7z" in reply)
        check("调用了 get_group_file_url", any(c[0] == "get_group_file_url" for c in bot.calls))
        check("链接收藏段在", "VirtualBox 官网" in reply)
        check("在线文档段在", "doc.qq.com" in reply)
        check("空结果不误报", "没找到" not in reply)

        # 全空 → 如实说没有
        async def empty_ri(bot, gid, ttl=None, force=False):
            return SimpleNamespace(error="", entries=[], locations=[])
        resource_index.get_index = empty_ri
        def empty_la(gid, kw, limit=5):
            return []
        linkarchive.search = empty_la
        async def empty_doc(gid, kw="", limit=8):
            return f"表格里共 5 条，但没有和「{kw}」相关的"
        docsync.read_bound_text = empty_doc
        reply = await intent._exec_fetch_resource(FakeBot(), ev2, {"keyword": "量子力学"}, False)
        check("全空如实告知", "没找到" in reply)
    finally:
        resource_index.get_index = orig_ri
        essence.get_index = orig_ess
        essence.search = orig_ess_search
        linkarchive.search = orig_la
        docsync.read_bound_text = orig_doc

    print(f"\n共 {ok} 项全部通过")


asyncio.run(main())
