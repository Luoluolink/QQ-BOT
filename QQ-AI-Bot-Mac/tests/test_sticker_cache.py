"""表情包缓存测试：安全文件名、存取往返、容量上限、主题命中。"""

import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DRIVER", "~fastapi")

import nonebot  # noqa: E402

nonebot.init()
nonebot.load_plugins("plugins")

from plugins.ai_chat import imagegen  # noqa: E402


class TestStickerCache(unittest.TestCase):
    def setUp(self):
        # 缓存目录指向临时位置，避免污染真实 stickers/
        imagegen.cfg = imagegen.cfg.model_copy(
            update={"ai_sticker_dir": str(self.tmp())}
        ) if hasattr(imagegen.cfg, "model_copy") else imagegen.cfg
        imagegen._cache_dir()

    def tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="stickercache_")
        return Path(d) / "stickers"

    def test_safe_name(self):
        self.assertEqual(imagegen._safe_name("生气的吉普莉尔/瞪眼"), "生气的吉普莉尔_瞪眼")
        self.assertEqual(imagegen._safe_name(""), "表情包")

    def test_save_find_roundtrip(self):
        imagegen._cache_save("生气的吉普莉尔", b"fakepng")
        hit = imagegen._cache_find("生气的吉普莉尔")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.read_bytes(), b"fakepng")

    def test_find_miss(self):
        self.assertIsNone(imagegen._cache_find("从没画过的主题"))

    def test_cap_evicts_oldest(self):
        for i in range(62):
            imagegen._cache_save(f"主题{i}", f"png{i}".encode())
        files = list(imagegen._cache_dir().glob("*.png"))
        self.assertLessEqual(len(files), 62)


if __name__ == "__main__":
    unittest.main()
