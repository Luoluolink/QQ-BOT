"""读文档（read_doc 意图）单元测试：_pick_rows/_rows_report 行挑选与汇报格式、
read_bound_text 的未绑定/表格/关键词分支（HTTP 层全 mock，不发网络请求）。

运行： python tests/test_docread.py
"""

import asyncio
import os
import sys
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

    # ---- _pick_rows ----
    rows = [
        ["名字", "分类", "介绍", "链接"],
        ["雪山教程", "旅行", "很美", "https://a.com/1"],
        ["猫咪图集", "宠物", "可爱", "https://a.com/2"],
        ["", "", "", ""],  # 空行要被跳过
        ["python入门", "编程", "基础教程", "https://a.com/3"],
    ]
    total, header, picked = docsync._pick_rows(rows, "", 10)
    check("pick 全量: 总数不含空行和表头", total == 3, f"total={total}")
    check("pick 全量: 表头识别", header[:2] == ["名字", "分类"])
    check("pick 全量: 不含表头", all(r[0] != "名字" for r in picked))

    total, header, picked = docsync._pick_rows(rows, "猫", 10)
    check("pick 关键词命中", len(picked) == 1 and picked[0][0] == "猫咪图集")

    total, header, picked = docsync._pick_rows(rows, "不存在的词", 10)
    check("pick 关键词未命中", total == 3 and picked == [])

    total, header, picked = docsync._pick_rows(rows, "", 2)
    check("pick limit 取最后2条", [r[0] for r in picked] == ["猫咪图集", "python入门"])

    total, header, picked = docsync._pick_rows([], "", 5)
    check("pick 空表", total == 0 and header == [] and picked == [])

    # ---- _rows_report ----
    data_rows = rows[1:]
    total, header, picked = docsync._pick_rows(rows, "", 10)
    text = docsync._rows_report(header, picked, total, "", "https://docs.qq.com/sheet/XYZ")
    check("report 带列名", "名字:雪山教程" in text, text[:80])
    check("report 多列用｜分隔", "｜" in text)
    check("report 含总数", "共 3 条" in text)
    check("report 含链接", "https://docs.qq.com/sheet/XYZ" in text)

    text = docsync._rows_report(header, picked[:1], total, "雪山", "https://docs.qq.com/sheet/XYZ")
    check("report 关键词措辞", "和「雪山」相关" in text, text[:80])

    text = docsync._rows_report([], [], 0, "", "https://x")
    check("report 空数据", "还没有收录" in text)

    # 无表头（长度不匹配时按裸值拼）
    text = docsync._rows_report([], [["a", "b"]], 1, "", "https://x")
    check("report 无表头兜底", "a｜b" in text)

    # 长文本截断
    long_cell = "很" * 100
    text = docsync._rows_report([], [[long_cell]], 1, "", "https://x")
    check("report 长文本截断", "…" in text and len(text) < 120, f"len={len(text)}")

    # ---- read_bound_text 分支 ----
    orig_binding = docsync.binding

    docsync.binding = lambda gid: None  # 未绑定
    t = await docsync.read_bound_text(12345)
    check("未绑定提示", "还没绑定" in t and "绑定文档" in t, t[:60])

    # 表格分支（mock 掉 sheet 层）
    docsync.binding = lambda gid: {
        "file_id": "DTEST123",
        "url": "https://docs.qq.com/sheet/DTEST123",
        "kind": "sheet",
        "book_id": "300000000$TEST",
    }

    async def fake_sheet_list(book_id):
        return [{"sheetID": "s1", "isVisible": True, "rowCount": 5, "columnCount": 4, "title": "Sheet1"}]

    async def fake_sheet_read(book_id, sid, n_rows, n_cols):
        return rows

    orig_list, orig_read = docsync._sheet_list, docsync._sheet_read
    docsync._sheet_list = fake_sheet_list
    docsync._sheet_read = fake_sheet_read
    t = await docsync.read_bound_text(12345)
    check("表格: 最近条目汇报", "最近收录的 3 条" in t and "python入门" in t, t[:80])
    t = await docsync.read_bound_text(12345, "猫")
    check("表格: 关键词汇报", "和「猫」相关" in t and "猫咪图集" in t, t[:80])

    # 空表
    async def fake_read_empty(book_id, sid, n_rows, n_cols):
        return []

    docsync._sheet_read = fake_read_empty
    t = await docsync.read_bound_text(12345)
    check("表格: 空表提示", "还没有收录" in t, t[:60])

    # 表格报错 → 兜底文案
    async def fake_read_boom(book_id, sid, n_rows, n_cols):
        raise RuntimeError("网络炸了")

    docsync._sheet_read = fake_read_boom
    t = await docsync.read_bound_text(12345)
    check("表格: 异常兜底", "出了点问题" in t, t[:60])

    # doc 分支
    docsync.binding = lambda gid: {
        "file_id": "DTESTDOC",
        "url": "https://docs.qq.com/doc/DTESTDOC",
        "kind": "doc",
    }

    async def fake_read_doc(fid):
        return "第一行是标题\nhttps://a.com/1\n第二行说明\nhttps://a.com/2\n最后一行", 10, 1

    orig_readdoc = docsync.read_doc
    docsync.read_doc = fake_read_doc
    t = await docsync.read_bound_text(12345)
    check("doc: 末尾内容", "文档末尾的内容" in t and "最后一行" in t, t[:80])
    t = await docsync.read_bound_text(12345, "标题")
    check("doc: 关键词", "和「标题」相关" in t and "第一行是标题" in t, t[:80])
    t = await docsync.read_bound_text(12345, "查无此词")
    check("doc: 关键词未命中", "没有提到" in t, t[:60])

    # 恢复
    docsync.binding = orig_binding
    docsync._sheet_list, docsync._sheet_read = orig_list, orig_read
    docsync.read_doc = orig_readdoc

    print(f"\n通过 {ok} 项" + (f"，失败 {len(fail)} 项: {fail}" if fail else ""))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
