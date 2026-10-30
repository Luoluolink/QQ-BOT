"""回复拆条测试：句末切分、碎片合并、上限封顶、短句不拆。

tools.py 是无依赖纯函数，直接按路径加载，不经过插件系统
（否则和 test_mood 在同一进程里重复加载插件会冲突）。
"""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "tools_standalone",
    Path(__file__).resolve().parent.parent / "plugins" / "ai_chat" / "tools.py",
)
_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools)
split_reply_bubbles = _tools.split_reply_bubbles


class TestSplitBubbles(unittest.TestCase):
    def test_short_stays_one(self):
        self.assertEqual(split_reply_bubbles("哼，找我什么事？"), ["哼，找我什么事？"])

    def test_two_sentences_split(self):
        out = split_reply_bubbles("睡了一整天。别误会，才不是在等谁的消息。")
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].endswith("。"))

    def test_fragment_merges_back(self):
        out = split_reply_bubbles("这问题有点意思。嗯。我查查看。")
        for b in out:
            self.assertGreater(len(b), 3)   # 「嗯。」碎片不单独成条

    def test_cap_at_three(self):
        long = "一。二。三。四。五。六。七。"
        self.assertLessEqual(len(split_reply_bubbles(long)), 3)

    def test_long_sentence_without_punct_stays(self):
        out = split_reply_bubbles("超过三十个字符的长句子没有句末标点就不会被拆开这一条保持原样")
        self.assertEqual(len(out), 1)

    def test_empty(self):
        self.assertEqual(split_reply_bubbles(""), [])


if __name__ == "__main__":
    unittest.main()
