"""表格（sheet）同步单元测试：bookID 列映射、表头识别、追加行、判重。

全部 mock 掉 HTTP 层，不发网络请求。
运行： python tests/test_sheet.py
"""

import asyncio
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


async def main() -> None:
    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))

    import nonebot

    nonebot.init()

    from plugins.ai_chat import docsync

    # 绑定/过滤都写到临时目录
    tmp = Path(tempfile.mkdtemp(prefix="sheettest-"))
    docsync._BINDING_PATH = tmp / "doc_binding.json"
    docsync._FILTER_PATH = tmp / "doc_filter.json"
    docsync._bindings = None
    docsync._filter = None

    print("1. 纯函数")
    check("列号 A", docsync._col_letter(1) == "A")
    check("列号 Z", docsync._col_letter(26) == "Z")
    check("列号 AA", docsync._col_letter(27) == "AA")
    check("列号 AY", docsync._col_letter(51) == "AY")
    check("kind sheet", docsync.parse_kind("https://docs.qq.com/sheet/DABC123?tab=X") == "sheet")
    check("kind doc", docsync.parse_kind("https://docs.qq.com/doc/DABC123") == "doc")
    check("kind 空", docsync.parse_kind("https://example.com/x") == "")
    check(
        "表头识别",
        docsync._map_header(["名字", "分类", "介绍", "链接"]) == {"name": 0, "cat": 1, "intro": 2, "link": 3},
    )
    check(
        "表头乱序也能认",
        docsync._map_header(["介绍", "网址", "名称"]) == {"name": 2, "intro": 0, "link": 1},
    )
    check("没有链接列 → None", docsync._map_header(["名字", "分类"]) is None)

    print("2. _sheet_append_entry（mock HTTP）")
    calls: list[dict] = []

    async def fake_http(**kwargs):
        calls.append(kwargs)
        url = kwargs["url"]
        if "/sheets-info" in url:
            return {
                "ret": 0,
                "data": {"sheetData": [{"sheetID": "BB0000", "title": "工作表1", "isVisible": True,
                                          "rowCount": 5, "columnCount": 4}]},
            }
        if "/spreadsheet/v3/" in url:
            return {
                "gridData": {
                    "startRow": 0,
                    "rows": [
                        {"values": [{"cellValue": {"text": "名字"}}, {"cellValue": {"text": "分类"}},
                                      {"cellValue": {"text": "介绍"}}, {"cellValue": {"text": "链接"}}]},
                        {"values": [{"cellValue": {"text": "a"}}, {"cellValue": None},
                                      {"cellValue": {"text": "b"}}, {"cellValue": {"text": "https://old.com/1"}}]},
                    ],
                }
            }
        return {"ret": 0, "msg": "Succeed"}  # PUT 写入

    async def fake_headers():
        return {}

    docsync._http_json = fake_http
    docsync._auth_headers = fake_headers

    gid = 1
    docsync._bindings_map()[str(gid)] = {
        "file_id": "DTEST", "url": "https://docs.qq.com/sheet/DTEST",
        "kind": "sheet", "book_id": "300000000$TEST",
    }

    calls.clear()
    status = await docsync._sheet_append_entry(
        gid, "一个好网站", "https://new-site.com/page", docsync.binding(gid)
    )
    check("正常追加返回 ok", status == "ok")
    puts = [c for c in calls if c.get("method") == "PUT"]
    check("只发了一次 PUT", len(puts) == 1)
    body = (puts[0].get("payload") or {}).get("values") or []
    check("追加行在第 6 行", puts and "A6%3AD6" in puts[0]["url"], puts[0]["url"] if puts else "")
    check("链接进了链接列", body and body[0][3] == "https://new-site.com/page")
    check("名字列 = 标题", body and body[0][0] == "一个好网站")
    check("介绍列 = 标题", body and body[0][2] == "一个好网站")
    check("分类列留空", body and body[0][1] == "")

    # 判重：URL 已存在
    status2 = await docsync._sheet_append_entry(
        gid, "重复的", "https://old.com/1", docsync.binding(gid)
    )
    check("URL 已存在 → dup", status2 == "dup")

    print("3. 空表（先写表头再写条目）")
    async def fake_http_empty(**kwargs):
        url = kwargs["url"]
        calls.append(kwargs)
        if "/sheets-info" in url:
            return {"ret": 0, "data": {"sheetData": [
                {"sheetID": "BB0000", "title": "s1", "isVisible": True, "rowCount": 0, "columnCount": 0}]}}
        return {"ret": 0, "msg": "Succeed"}

    docsync._http_json = fake_http_empty
    calls.clear()
    status3 = await docsync._sheet_append_entry(
        gid, "第一条", "https://first.com/x", docsync.binding(gid)
    )
    check("空表追加返回 ok", status3 == "ok")
    puts3 = [c for c in calls if c.get("method") == "PUT"]
    check("空表发了两次 PUT（表头+条目）", len(puts3) == 2, len(puts3))
    header_row = (puts3[0].get("payload") or {}).get("values") or [[]]
    check("默认表头正确", header_row[0][:4] == ["名字", "分类", "介绍", "链接"])
    check("条目写在第 2 行", "A2%3AD2" in puts3[1]["url"], puts3[1]["url"])

    print("4. 无可识别表头（A-D 兜底）")
    async def fake_http_noheader(**kwargs):
        url = kwargs["url"]
        calls.append(kwargs)
        if "/sheets-info" in url:
            return {"ret": 0, "data": {"sheetData": [
                {"sheetID": "BB0000", "title": "s1", "isVisible": True, "rowCount": 3, "columnCount": 2}]}}
        if "/spreadsheet/v3/" in url:
            return {"gridData": {"startRow": 0, "rows": [
                {"values": [{"cellValue": {"text": "hello"}}, {"cellValue": {"text": "world"}}]},
                {"values": [{"cellValue": None}, {"cellValue": None}]},
                {"values": [{"cellValue": None}, {"cellValue": None}]},
            ]}}
        return {"ret": 0, "msg": "Succeed"}

    docsync._http_json = fake_http_noheader
    calls.clear()
    status4 = await docsync._sheet_append_entry(
        gid, "兜底条目", "https://fallback.com/", docsync.binding(gid)
    )
    check("无表头追加返回 ok", status4 == "ok")
    puts4 = [c for c in calls if c.get("method") == "PUT"]
    check("写在第 4 行", puts4 and "A4%3AD4" in puts4[0]["url"], puts4[0]["url"] if puts4 else "")
    row4 = (puts4[0].get("payload") or {}).get("values") or [[]]
    check("链接落 D 列", row4 and row4[0][3] == "https://fallback.com/")

    print()
    if fail:
        print(f"共 {ok + len(fail)} 项，{len(fail)} 项失败：")
        for name in fail:
            print("  -", name)
        sys.exit(1)
    print(f"全部 {ok} 项通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
