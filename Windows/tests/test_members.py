"""离线测试：群成员识别 + 新成员入群欢迎。

全程不联网、不调模型：用一个假 Bot 顶替协议端，
只验证「@ 还原」「名字匹配」「限流闸门」这几条容易出错的地方。

运行： python tests/test_members.py [项目根目录]
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


# 假协议端：只实现用到的两个接口
RAW_MEMBERS = [
    {
        "user_id": 1001,
        "nickname": "咸鱼",
        "card": "小a",
        "role": "owner",
        "level": "9",
        "title": "摸鱼冠军",
    },
    {
        "user_id": 1002,
        "nickname": "小a的猫",
        "card": "小a的猫",
        "role": "admin",
        "level": "3",
    },
    {"user_id": 1003, "nickname": "路人乙", "card": "", "role": "member"},
    # 单字昵称：不该参与文本匹配，否则一句话里到处误伤
    {"user_id": 1004, "nickname": "鱼", "card": "", "role": "member"},
]


class FakeBot:
    def __init__(self, members=None, fail_times=0):
        self._members = RAW_MEMBERS if members is None else members
        self.calls = []
        self.sent = []
        self.fail_times = fail_times
        self._info_tries = 0

    async def call_api(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if api == "get_group_member_list":
            return self._members
        if api == "get_group_member_info":
            self._info_tries += 1
            if self._info_tries <= self.fail_times:
                # 模拟「入群通知刚到、成员信息还没建好」
                return {}
            for item in self._members:
                if int(item["user_id"]) == int(kwargs.get("user_id", 0)):
                    return item
            return {}
        raise RuntimeError(f"未实现的接口: {api}")

    async def send_group_msg(self, **kwargs):
        self.sent.append(kwargs)
        return {"message_id": 1}


class FakeIncreaseEvent:
    """够用的假入群事件：处理器只用到这三个字段。"""

    def __init__(self, group_id, user_id, self_id=999):
        self.group_id = group_id
        self.user_id = user_id
        self.self_id = self_id


def msg(*segments):
    from nonebot.adapters.onebot.v11 import Message

    return Message(segments)


async def main() -> None:
    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))

    import nonebot

    nonebot.init()

    import plugins.ai_chat as ac
    from plugins.ai_chat import members, welcome

    from nonebot.adapters.onebot.v11 import MessageSegment

    # 测试必须自洽，不能依赖用户本地的 .env。
    # 入群欢迎是「主动发消息」，用户很可能关掉它（AI_WELCOME_ENABLED=false），
    # 那样第 10 节就会一行不跑、报一个看不懂的 IndexError。
    # 这里把本测试要用到的开关钉死，测的是代码逻辑本身。
    ac.cfg.ai_member_enabled = True
    ac.cfg.ai_welcome_enabled = True
    ac.cfg.ai_welcome_cooldown = 15.0
    ac.cfg.ai_welcome_daily_limit = 20

    # ---------------------------------------------------------------- 解析
    print("1. 成员资料解析（字段名容错 + 群名片优先）")
    m = members._to_member(RAW_MEMBERS[0])
    check("QQ 解析正确", m.qq == 1001, m.qq)
    check("群名片优先于昵称", m.display == "小a", m.display)
    check("两个名字都留着", m.names == ["小a", "咸鱼"], m.names)
    check("群主翻译成中文", m.role_cn == "群主", m.role_cn)
    check("管理员翻译成中文", members._to_member(RAW_MEMBERS[1]).role_cn == "管理员")
    check("普通成员默认", members._to_member(RAW_MEMBERS[2]).role_cn == "群成员")
    check("没有 QQ 的条目被丢弃", members._to_member({"nickname": "x"}) is None)
    check("非字典输入被丢弃", members._to_member("x") is None)
    check("兼容 uin 字段名", members._to_member({"uin": 5}).qq == 5)
    check("兼容 card_name 字段名", members._to_member({"qq": 5, "card_name": "c"}).card == "c")

    print("\n2. 单字昵称不参与文本匹配（避免误伤）")
    single = members._to_member(RAW_MEMBERS[3])
    check("「鱼」不在 matchable 里", single.matchable == [], single.matchable)
    check("「小a」在 matchable 里", members._to_member(RAW_MEMBERS[0]).matchable == ["小a", "咸鱼"])

    # ---------------------------------------------------------------- @ 还原
    print("\n3. @ 段还原（get_plaintext 会把 @ 整个丢掉）")
    plain = msg(
        MessageSegment.text(""),
        MessageSegment.at(999),
        MessageSegment.text(" "),
        MessageSegment.at(1001),
        MessageSegment.text("他最近怎么样"),
    )
    at_qqs = members.extract_ats(plain, self_id=999)
    check("抽出了被 @ 的其他人", at_qqs == [1001], at_qqs)
    text = members.render_text(plain, {1001: "小a"}, self_id=999)
    check("机器人自己的 @ 被去掉", "@999" not in text, text)
    check("@ 还原成了名字", "@小a" in text, text)
    check("名字后补了空格", "小a 他最近" in text, text)

    allmsg = msg(MessageSegment.at("all"), MessageSegment.text("集合"))
    check("@全体成员被保留", members.render_text(allmsg, {}, 999) == "@全体成员 集合")
    check("extract_ats 不含 all", members.extract_ats(allmsg, 999) == [])

    dup = msg(MessageSegment.at(1001), MessageSegment.at(1001))
    check("重复 @ 去重", members.extract_ats(dup, 999) == [1001])

    noname = msg(MessageSegment.at(1001), MessageSegment.text("在吗"))
    check("查不到名字时退回 QQ 号", "@1001" in members.render_text(noname, {}, 999))

    # ---------------------------------------------------------------- 文本匹配
    print("\n4. 正文里的名字匹配")
    index = members.MemberIndex(group_id=1)
    index.members = [members._to_member(x) for x in RAW_MEMBERS]
    index.members = [x for x in index.members if x is not None]

    hits = members.find_mentioned(index, "小a怎么样")
    check("按名字命中 1 人", [h.qq for h in hits] == [1001], [h.qq for h in hits])

    hits = members.find_mentioned(index, "小a的猫可爱吗")
    check(
        "长名字优先，不误认成「小a」",
        [h.qq for h in hits] == [1002],
        [h.qq for h in hits],
    )

    hits = members.find_mentioned(index, "小a和小a的猫都来了")
    check(
        "两处都真的出现时，两个人都在",
        sorted(h.qq for h in hits) == [1001, 1002],
        [h.qq for h in hits],
    )

    hits = members.find_mentioned(index, "他怎么样", extra_qqs=[1003])
    check("@ 到的人即使正文没名字也能命中", [h.qq for h in hits] == [1003], [h.qq for h in hits])

    hits = members.find_mentioned(index, "小a怎么样", exclude_qqs=[1001])
    check("排除名单生效", hits == [], hits)

    hits = members.find_mentioned(index, "今天天气不错")
    check("没提到任何人时返回空", hits == [], hits)

    hits = members.find_mentioned(index, "鱼好吃吗")
    check("单字昵称不会误伤", hits == [], [h.qq for h in hits])

    hits = members.find_mentioned(index, "小a和咸鱼")
    check("同一人的两个名字只算一次", [h.qq for h in hits] == [1001], [h.qq for h in hits])

    # ---------------------------------------------------------------- 提示词
    print("\n5. 给模型的资料不能留幻觉的口子")
    block = members.describe([index.members[0]])
    check("带了防幻觉的固定说法", "【群成员识别结果】" in block, block[:40])
    check("写清了群身份", "群主" in block, block)
    check("写清了专属头衔", "摸鱼冠军" in block, block)
    check("要求不知道就说不知道", "别自己想象" in block, block)
    check("空名单返回空串", members.describe([]) == "")

    # ---------------------------------------------------------------- 全链路
    print("\n6. 全链路：_build_member_context")
    members._cache.clear()
    bot = FakeBot()
    ctx = await ac._build_member_context(bot, 200, "@小a 他怎么样", [1001], self_id=999)
    check("拿到了资料块", "【群成员识别结果】" in ctx, ctx[:40])
    check("资料里有小a", "小a" in ctx, ctx)
    check("拉名单只调了一次接口", sum(1 for c in bot.calls if c[0] == "get_group_member_list") == 1)

    ctx2 = await ac._build_member_context(bot, 200, "@小a 他怎么样", [1001], self_id=999)
    check(
        "第二次走缓存，不再调接口",
        sum(1 for c in bot.calls if c[0] == "get_group_member_list") == 1,
        bot.calls,
    )

    members.invalidate(200)
    await ac._build_member_context(bot, 200, "@小a 他怎么样", [1001], self_id=999)
    check(
        "invalidate 后重新拉取",
        sum(1 for c in bot.calls if c[0] == "get_group_member_list") == 2,
        bot.calls,
    )

    ctx3 = await ac._build_member_context(bot, 201, "今天天气怎么样", [], self_id=999)
    check("没提到人时不塞资料（省 token）", ctx3 == "", ctx3)

    # ---------------------------------------------------------------- 查名字
    print("\n7. 入群时查新成员的名字（要能扛住「信息还没建好」）")
    flaky = FakeBot(fail_times=2)
    name = await members.fetch_member_name(flaky, 300, 1001, retries=3, delay=0)
    check("重试后拿到群名片", name == "小a", name)

    hopeless = FakeBot(members=[], fail_times=99)
    name = await members.fetch_member_name(hopeless, 300, 7777, retries=2, delay=0)
    check("彻底查不到时退回 QQ 号，而不是空串", name == "7777", name)

    # ---------------------------------------------------------------- 欢迎语
    print("\n8. 欢迎语生成")
    msgs = welcome.build_messages(
        "今天的日期是 2026-10-11（星期六）。\n\n你是这个群里的助手。", "小a", 1001
    )
    check("两条消息（system + user）", len(msgs) == 2, len(msgs))
    check("人设进了 system", "你是这个群里的助手" in msgs[0]["content"])
    check("日期进了 system", "2026-10-11" in msgs[0]["content"])
    check("群名片进了 user", "小a" in msgs[1]["content"])
    check("QQ 进了 user", "1001" in msgs[1]["content"])
    check("要求只回一句话", "只回一句话" in msgs[1]["content"])

    tpl = welcome.pick_template("小a")
    check("模板填了名字", "小a" in tpl, tpl)
    check("模板不含未替换的占位符", "{" not in tpl, tpl)

    # ---------------------------------------------------------------- 限流
    print("\n9. 欢迎限流（主动发消息，必须比聊天更严）")
    welcome._day = ""
    welcome._last_welcome.clear()
    welcome._daily_count.clear()

    check("首次允许", welcome.check(1, 1000.0, 15.0, 20) == "", welcome.check(1, 1000.0, 15.0, 20))
    welcome.mark(1, 1000.0)
    check("计数变成 1", welcome.stats(1)["count"] == 1, welcome.stats(1))

    reason = welcome.check(1, 1005.0, 15.0, 20)
    check("冷却期内被拦下", "冷却" in reason or "秒" in reason, reason)
    check("刚好到冷却边界就放行", welcome.check(1, 1015.0, 15.0, 20) == "", welcome.check(1, 1015.0, 15.0, 20))

    welcome.mark(1, 1015.0)
    welcome.mark(1, 1030.0)
    reason = welcome.check(1, 1045.0, 15.0, 3)
    check("达到每日上限后被拦下", "上限" in reason, reason)
    check("每日上限按群独立", welcome.check(2, 1045.0, 15.0, 3) == "", welcome.check(2, 1045.0, 15.0, 3))

    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    check("关掉冷却和上限就永远放行", welcome.check(3, 1.0, 0.0, 0) == "")

    # ---------------------------------------------------------------- 端到端
    print("\n10. 端到端：直接跑入群欢迎处理器")

    class _Obj:
        """够用的鸭子对象：OpenAI 响应是 choices[0].message.content 这种嵌套。"""

        def __init__(self, **kw):
            self.__dict__.update(kw)

    def fake_resp(text):
        return _Obj(choices=[_Obj(message=_Obj(content=text))])

    calls: list = []

    async def fake_completion(messages, allow_search=False):
        calls.append((messages, allow_search))
        return fake_resp("欢迎，小a。有什么想问的直接 @ 我就行。")

    real_completion = ac._create_completion
    ac._create_completion = fake_completion
    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    welcome._day = ""

    bot2 = FakeBot()
    ev = FakeIncreaseEvent(group_id=500, user_id=1001)
    await ac.handle_group_increase(bot2, ev)

    check("调了一次模型", len(calls) == 1, len(calls))
    # 同上：断言失败要报 FAIL，不能让 IndexError 把整节吞掉。
    check("欢迎语不联网（allow_search=False）", bool(calls) and calls[0][1] is False)
    check("发了消息", len(bot2.sent) == 1, bot2.sent)
    if bot2.sent:
        sent_msg = bot2.sent[0]["message"]
        check("发到了正确的群", bot2.sent[0]["group_id"] == 500)
        check("消息里 @ 了新成员", any(s.type == "at" for s in sent_msg), [s.type for s in sent_msg])
        check("欢迎语用了模型返回的内容", "有什么想问的" in str(sent_msg), str(sent_msg))
    else:
        check("发到了正确的群", False, "上一条就没发出去")
        check("消息里 @ 了新成员", False, "上一条就没发出去")
        check("欢迎语用了模型返回的内容", False, "上一条就没发出去")
    check("记了一次额度", welcome.stats(500)["count"] == 1, welcome.stats(500))

    # 冷却期内再有人入群：应该直接跳过，不再调模型
    calls.clear()
    bot2.sent.clear()
    await ac.handle_group_increase(bot2, FakeIncreaseEvent(group_id=500, user_id=1002))
    check("冷却期内不再调模型", calls == [], calls)
    check("冷却期内不发消息", bot2.sent == [], bot2.sent)

    # 模型返回小作文：只留第一行，别在群里刷屏
    async def verbose_completion(messages, allow_search=False):
        return fake_resp("欢迎，小a。\n\n另外补充三点：\n1. 先看群规\n2. 别刷屏\n3. 有问题 @ 我")

    ac._create_completion = verbose_completion
    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    bot2.sent.clear()
    await ac.handle_group_increase(bot2, FakeIncreaseEvent(group_id=504, user_id=1001))
    if bot2.sent:
        body = str(bot2.sent[0]["message"])
        check("只保留第一行", "群规" not in body, body)
        check("第一行的内容还在", "欢迎，小a" in body, body)
    else:
        check("只保留第一行", False, "压根没发消息")
        check("第一行的内容还在", False, "压根没发消息")

    # 模型挂了：必须退回模板，而不是不吭声
    async def broken_completion(messages, allow_search=False):
        raise RuntimeError("模型炸了")

    ac._create_completion = broken_completion
    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    bot2.sent.clear()
    await ac.handle_group_increase(bot2, FakeIncreaseEvent(group_id=501, user_id=1001))
    check("模型挂了仍然发了欢迎", len(bot2.sent) == 1, bot2.sent)
    if bot2.sent:
        fallback = str(bot2.sent[0]["message"])
        check(
            "用的是模板文案（不依赖随机选到哪一条）",
            any(tpl.format(name="小a") in fallback for tpl in welcome.TEMPLATES),
            fallback,
        )
    else:
        check("用的是模板文案（不依赖随机选到哪一条）", False, "压根没发消息")

    # 关掉开关：一个字都不该说
    ac._create_completion = real_completion
    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    bot2.sent.clear()
    real_flag = ac.cfg.ai_welcome_enabled
    ac.cfg.ai_welcome_enabled = False
    await ac.handle_group_increase(bot2, FakeIncreaseEvent(group_id=502, user_id=1001))
    ac.cfg.ai_welcome_enabled = real_flag
    check("开关关掉后完全沉默", bot2.sent == [], bot2.sent)

    # 机器人自己进群，不自我欢迎
    welcome._last_welcome.clear()
    welcome._daily_count.clear()
    bot2.sent.clear()
    await ac.handle_group_increase(bot2, FakeIncreaseEvent(group_id=503, user_id=999, self_id=999))
    check("机器人自己进群不自我欢迎", bot2.sent == [], bot2.sent)

    print(f"\n{'=' * 46}")
    if fail:
        print(f"失败 {len(fail)} 项 / 共 {ok + len(fail)} 项：")
        for name in fail:
            print(f"  - {name}")
        sys.exit(1)
    print(f"全部通过：{ok} 项断言")


if __name__ == "__main__":
    asyncio.run(main())
