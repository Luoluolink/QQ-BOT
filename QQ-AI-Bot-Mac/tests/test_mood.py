"""情绪状态机测试：衰减、切换、持久化、触发词。"""

import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DRIVER", "~fastapi")
os.environ.setdefault("AI_MOOD_ENABLED", "true")

import nonebot  # noqa: E402

nonebot.init()
nonebot.load_plugins("plugins")

from plugins.ai_chat import mood  # noqa: E402


class TestMood(unittest.TestCase):
    def setUp(self):
        mood._state = {}
        if mood._STATE_PATH.exists():
            mood._STATE_PATH.unlink()

    def test_bump_and_describe(self):
        mood.bump("g1", "happy", 20, "被夸了")
        text = mood.describe("g1")
        self.assertIn("开心", text)
        self.assertIn("被夸了", text)

    def test_decay_to_calm(self):
        mood.bump("g2", "happy", 10, "被夸了")
        # 人为把时间拨回很久之前 → 衰减到平静
        state = mood._load()["g2"]
        state["ts"] -= 3600 * 5
        mood._state["g2"] = state
        self.assertEqual(mood.describe("g2"), "")

    def test_switch_emotion(self):
        mood.bump("g3", "annoyed", 30, "被怼了")
        mood.bump("g3", "happy", 35, "被夸了")
        self.assertEqual(mood._load()["g3"]["emotion"], "happy")

    def test_same_emotion_accumulates(self):
        mood.bump("g4", "annoyed", 20, "被怼了")
        mood.bump("g4", "annoyed", 20, "又被怼了")
        self.assertGreaterEqual(mood._load()["g4"]["value"], 40)

    def test_unknown_emotion_ignored(self):
        mood.bump("g5", "爆怒", 50, "不存在")
        self.assertNotIn("g5", mood._load())

    def test_react_to_text(self):
        mood.react_to_text("g6", "你也太厉害了吧")
        self.assertEqual(mood._load()["g6"]["emotion"], "happy")
        mood.react_to_text("g7", "你个笨蛋")
        self.assertEqual(mood._load()["g7"]["emotion"], "annoyed")

    def test_persistence(self):
        mood.bump("g8", "happy", 50, "被夸了")
        mood._state = None  # 模拟重启
        self.assertEqual(mood._load()["g8"]["emotion"], "happy")


if __name__ == "__main__":
    unittest.main()
