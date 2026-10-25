"""对话衔接三件套的单元测试：群聊背景（ambient）、引用还原、图片历史降级。

全部离线：ambient/降级是纯函数；引用还原用 SimpleNamespace 假 event，
不碰协议端，不调模型，不花钱。
运行： python tests/test_ambient.py
"""

import asyncio
import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

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

    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    import plugins.ai_chat as ac
    from plugins.ai_chat import ambient, tools, vision

    print("1. 群聊背景缓冲（ambient）")
    gid = 987654
    check("没有记录时 describe 为空串", ambient.describe(gid) == "")
    ambient.record(gid, "小a", "我今天去看了沙丘3")
    ambient.record(gid, "小b", "好看吗？我还没去")
    ambient.record(gid, "小a", "特效炸裂，强烈推荐")
    lines = ambient.recent_lines(gid)
    check("按时间正序取出", lines == [
        "【小a】我今天去看了沙丘3",
        "【小b】好看吗？我还没去",
        "【小a】特效炸裂，强烈推荐",
    ], lines)
    check("limit 生效", len(ambient.recent_lines(gid, limit=2)) == 2)
    block = ambient.describe(gid, count=6)
    check("背景块带说明与发言人标记", "最近群聊背景" in block and "【小a】" in block and "【小b】" in block)
    check("背景块声明『不是对你说的』", "不是对你说的" in block)
    long_text = "长" * 200
    ambient.record(gid, "小c", long_text)
    check("超长消息截断", max(len(l) for l in ambient.recent_lines(gid)) <= 80 + len("【小c】"))
    check("空文本不记录", (ambient.record(gid, "小d", "  "), len(ambient.recent_lines(gid)) == 4)[1])
    check("配置开关默认开启", ac.cfg.ai_ambient_enabled is True)

    print("2. 引用还原（reply_quote_text）")
    ev = SimpleNamespace(reply=SimpleNamespace(
        sender=SimpleNamespace(card="", nickname="张三", user_id=1001),
        message=Message("我觉得这个方案可行"),
    ))
    got = tools.reply_quote_text(ev)
    check("正文引用带名字", got == "【引用 张三 的消息】我觉得这个方案可行", got)
    ev_card = SimpleNamespace(reply=SimpleNamespace(
        sender=SimpleNamespace(card="群名片A", nickname="昵称A", user_id=1002),
        message=Message("hh"),
    ))
    check("群名片优先", "【引用 群名片A 的消息】" in tools.reply_quote_text(ev_card))
    ev_img = SimpleNamespace(reply=SimpleNamespace(
        sender=SimpleNamespace(card="", nickname="李四", user_id=1003),
        message=Message([MessageSegment.image("file://x.jpg")]),
    ))
    check("纯图片引用给占位", tools.reply_quote_text(ev_img) == "【引用 李四 的消息】[图片]")
    check("没有引用返回空串", tools.reply_quote_text(SimpleNamespace()) == "")
    ev_long = SimpleNamespace(reply=SimpleNamespace(
        sender=SimpleNamespace(card="", nickname="王五", user_id=1004),
        message=Message("长" * 300),
    ))
    got_long = tools.reply_quote_text(ev_long)
    check("超长引用截断", len(got_long) <= len("【引用 王五 的消息】") + 150)

    print("3. 图片历史降级（content_to_text / _demote_image_content）")
    content = [
        {"type": "text", "text": "【小a】这是什么猫"},
        {"type": "image_url", "image_url": {"url": "https://cdn.example/cat.jpg"}},
    ]
    flat = vision.content_to_text(content)
    check("数组压成文本", flat == "【小a】这是什么猫 [图片]", flat)
    check("纯字符串原样返回", vision.content_to_text("你好") == "你好")
    history = deque([
        {"role": "user", "content": content},
        {"role": "assistant", "content": "是布偶猫"},
    ])
    ac._demote_image_content(history)
    check("更早一轮的图片被降级成占位",
          history[0]["content"] == "【小a】这是什么猫 [图片]", history[0]["content"])
    check("assistant 消息不受影响", history[1]["content"] == "是布偶猫")

    print("4. 对话衔接相关配置默认值")
    check("群冷却 = 3 秒", ac.cfg.ai_cooldown == 3.0, ac.cfg.ai_cooldown)
    check("历史轮数 = 12", ac.cfg.ai_history_rounds == 12, ac.cfg.ai_history_rounds)
    check("背景注入条数 = 6", ac.cfg.ai_ambient_context_count == 6)

    print("5. 意图路由提示词：观点类问句必须判 chat")
    prompt = ac.intent._router_prompt()
    check("索取句式要求具体资源名词", "资源名词" in prompt)
    check("观点类问句示例存在", "有没有必要读研" in prompt)

    print()
    if fail:
        print(f"共 {ok + len(fail)} 项，{len(fail)} 项失败：")
        for name in fail:
            print("  -", name)
        sys.exit(1)
    print(f"全部 {ok} 项通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
