"""模拟 NapCat 的测试客户端：不装 NapCat、不登 QQ 也能把机器人跑通。

原理：NapCat 和 NoneBot2 之间走的是「反向 WebSocket」——由 NapCat 主动连到
NoneBot2 的 /onebot/v11/ws，之后双方在这个连接上双向收发。所以只要写一个
程序扮演 NapCat 连上去，就能完整地走一遍真实链路：

    发群消息事件 → 机器人解析 → 调用 OneBot 接口取群文件/相册 → 调大模型 → 发回复

本脚本会伪造一批群文件和群相册数据来响应机器人的接口调用，
并把机器人最终发出的回复打印出来。

用法：
    1. 先启动机器人（另开一个终端）：
           python bot.py
    2. 再运行本脚本：
           python mock_napcat.py

依赖：websockets（已随 nonebot2 一起装好）
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 这些值默认从同目录的 .env 里读，所以不用手动改；
# 只有在 .env 找不到时才回退到下面这组默认值。
TOKEN = "qbot_change_me"
SELF_ID = 10000
GROUP_ID = 123456789
USER_ID = 10001

# 机器人昵称，只用于日志展示
BOT_NICKNAME = "测试机器人"

# ---------------------------------------------------------------- 读取 .env


def _load_env() -> None:
    """从同目录 .env 里覆盖 TOKEN / SELF_ID / WS_URL。

    这样测试脚本和机器人永远用的是同一套配置，不会因为改了 Token
    忘了同步而导致「连不上」。
    """
    global TOKEN, SELF_ID, WS_URL
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key == "ONEBOT_ACCESS_TOKEN" and value:
            TOKEN = value
        elif key == "PORT" and value.isdigit():
            WS_URL = f"ws://127.0.0.1:{value}/onebot/v11/ws"


WS_URL = "ws://127.0.0.1:8080/onebot/v11/ws"
_load_env()

# 每条测试消息之间的间隔，必须大于 .env 里的 AI_COOLDOWN，否则会被冷却挡掉。
# 想把整套流程跑快一点，可以设环境变量 MOCK_GAP_SECONDS=2（同时把 AI_COOLDOWN 调小）。
GAP_SECONDS = float(os.environ.get("MOCK_GAP_SECONDS") or 6.5)

# 全部发完后额外等待的时间，用来收最后一条回复
FINAL_WAIT_SECONDS = 12

# ---------------------------------------------------------------- 伪造的群数据

ROOT = {
    "files": [
        {
            "group_id": GROUP_ID, "file_id": "f0", "file_name": "资料汇总.zip",
            "busid": 102, "file_size": 104857600, "upload_time": 1700000000,
            "uploader": 10002, "uploader_name": "周八",
        }
    ],
    "folders": [
        {"group_id": GROUP_ID, "folder_id": "d1", "folder_name": "财务",
         "total_file_count": 1, "create_name": "张三"},
        {"group_id": GROUP_ID, "folder_id": "d2", "folder_name": "行政",
         "total_file_count": 2, "create_name": "王五"},
        {"group_id": GROUP_ID, "folder_id": "d3", "folder_name": "市场",
         "total_file_count": 1, "create_name": "赵六"},
    ],
}

FOLDERS = {
    "d1": {
        "files": [{"file_id": "f1", "file_name": "2026年度预算表.xlsx",
                   "file_size": 184320, "uploader_name": "张三"}],
        "folders": [{"folder_id": "d1a", "folder_name": "报销制度"}],
    },
    "d1a": {
        "files": [{"file_id": "f2", "file_name": "财务报销流程.docx",
                   "file_size": 45056, "uploader_name": "李四"}],
        "folders": [],
    },
    "d2": {
        "files": [
            {"file_id": "f3", "file_name": "公司组织架构图.png",
             "file_size": 220000, "uploader_name": "王五"},
            {"file_id": "f4", "file_name": "员工手册.pdf",
             "file_size": 990000, "uploader_name": "王五"},
        ],
        "folders": [],
    },
    "d3": {
        "files": [{"file_id": "f5", "file_name": "产品介绍.pptx",
                   "file_size": 3300000, "uploader_name": "赵六"}],
        "folders": [],
    },
}

ALBUMS = [
    {"id": "alb1", "name": "团建照片", "picNum": 3, "createnickname": "张三"},
    {"id": "alb2", "name": "年会视频", "picNum": 2, "createnickname": "李四"},
]

ALBUM_MEDIA = {
    "alb1": {
        "media_list": [
            {"media_id": "m1", "url": "http://example.com/tuanjian1.jpg",
             "file_name": "团建合影1.jpg", "uploader_name": "张三"},
            {"media_id": "m2", "url": "http://example.com/tuanjian2.jpg",
             "file_name": "团建合影2.jpg", "uploader_name": "张三"},
            {"media_id": "m3", "url": "http://example.com/tuanjian3.jpg",
             "file_name": "团建合影3.jpg", "uploader_name": "李四"},
        ]
    },
    "alb2": {
        "media_list": [
            {"media_id": "m4", "url": "http://example.com/nianhui.mp4",
             "file_name": "年会现场.mp4"},
            {"media_id": "m5", "url": "http://example.com/chanpin.mp4",
             "file_name": "产品演示.mp4"},
        ]
    },
}

# ---------------------------------------------------------------- 伪造的群精华
#
# 这里刻意用了三种不同的 content 形态，因为 NapCat 不同版本返回的不一样：
#   · 纯字符串（可能是 CQ 码）
#   · 消息段数组 [{"type": "text", "data": {...}}]
#   · 字符串数组
# 三种都得能被机器人正确解析，否则精华功能会在某些版本上静默失效。
ESSENCE = [
    {
        "msg_seq": 1, "msg_random": 111, "message_id": 5001,
        "sender_id": 10002, "sender_nick": "张三",
        "operator_id": 10000, "operator_nick": "测试机器人",
        "operator_time": 1780000000,
        "content": "【重要】本周五下午三点全员大会，地点在 3 楼会议室，请准时参加。",
    },
    {
        "msg_seq": 2, "msg_random": 222, "message_id": 5002,
        "sender_id": 10003, "sender_nick": "李四",
        "operator_id": 10000, "operator_nick": "测试机器人",
        "operator_time": 1781000000,
        "content": "群文件里「财务/报销制度」更新了，出差报销标准涨到每天 200。",
    },
    {
        # 消息段数组形态
        "msg_seq": 3, "msg_random": 333, "message_id": 5003,
        "sender_id": 10004, "sender_nick": "王五",
        "operator_id": 10000, "operator_nick": "测试机器人",
        "operator_time": 1782000000,
        "content": [
            {"type": "text", "data": {"text": "团建照片都传群相册「团建照片」了，"}},
            {"type": "image", "data": {"file": "tuanjian1.jpg"}},
        ],
    },
    {
        # CQ 码字符串形态
        "msg_seq": 4, "msg_random": 444, "message_id": 5004,
        "sender_id": 10005, "sender_nick": "赵六",
        "operator_id": 10000, "operator_nick": "测试机器人",
        "operator_time": 1783000000,
        "content": "[CQ:at,qq=10001] 记得下周一交周报，模板在群文件「行政」里",
    },
]

# 伪造的图片信息，用于 get_image。
# url 会在下面被替换成本机托管的真图地址（见「伪造图片」一节）。
FAKE_IMAGE = {
    "file": r"C:\fake\napcat\cache\photo.jpg",
    "url": "http://example.com/fake-photo.jpg",
    "file_size": "20480",
    "file_name": "photo.jpg",
}


# ---------------------------------------------------------------- 伪造图片
#
# 测识图需要一个「模型真的能下载到」的图片地址。
# 如果随手写个 example.com 的假链接，模型会拿到 404，
# 机器人就会回「模型那边好像出问题了」—— 看起来像识图坏了，其实是图取不到。
# 所以这里现场生成一张 PNG，再用一个极小的 HTTP 服务挂在本地。

MOCK_IMAGE_PORT = int(os.environ.get("MOCK_IMAGE_PORT") or 18099)

# 图片里画了什么，一眼能看出来：白底 + 红圆 + 蓝横条 + 绿三角
IMAGE_DESC = "白底，中间一个红色实心圆，下方一条蓝色横条，左上角一个绿色三角形"


def _make_test_png(width: int = 128, height: int = 128) -> bytes:
    """手写一个最小 PNG 编码器（标准库 zlib + struct），不引入 Pillow 依赖。"""

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        cx, cy, r = width / 2, height / 2, width * 0.28
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            return (220, 40, 40)  # 红圆
        if height * 0.78 <= y <= height * 0.88:
            return (40, 80, 220)  # 蓝横条
        if y < height * 0.28 and x < y * 1.6:
            return (40, 170, 80)  # 左上绿三角
        return (245, 245, 245)  # 白底

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # 每行的过滤器类型：0 = None
        for x in range(width):
            raw.extend(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


_PNG_BYTES = _make_test_png()

IMAGE_URL = f"http://127.0.0.1:{MOCK_IMAGE_PORT}/mock.png"
FAKE_IMAGE["url"] = IMAGE_URL


class _ImageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音访问日志
        pass

    def do_GET(self):
        if self.path.startswith("/mock.png"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG_BYTES)))
            self.end_headers()
            self.wfile.write(_PNG_BYTES)
        else:
            self.send_error(404)


def start_image_server() -> ThreadingHTTPServer | None:
    """起一个只服务那张测试图的小 HTTP 服务。端口被占就跳过，不影响主流程。"""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", MOCK_IMAGE_PORT), _ImageHandler)
    except OSError as exc:
        print(f"（测试图片服务端口 {MOCK_IMAGE_PORT} 起不来：{exc}，识图用例可能取不到图）")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

# ---------------------------------------------------------------- 测试消息
#
# 字符串 = 纯文本消息；
# 元组 (文字, 附加消息段) = 带图片等段的消息，文字可以为空。

MESSAGES = [
    "/files list",
    "/essence",
    "/essence 报销",
    "精华消息里有没有讲报销的",
    "群里精华消息说过团建吗",
    "精华消息里有没有人提过量子力学",
    ("这张图里有什么", [{"type": "image", "data": {"file": "photo.jpg", "url": FAKE_IMAGE["url"]}}]),
    ("", [{"type": "image", "data": {"file": "photo.jpg", "url": FAKE_IMAGE["url"]}}]),
    "群里有没有团建的照片",
    "有没有量子力学的课件",
    "财务那边的表格在哪",
    "今天好累啊",
]


def make_event(text: str, message_id: int, extra: list | None = None) -> dict:
    # 命令（以 / 开头）不需要 @，其余消息都带上 @ 触发
    is_command = text.startswith("/")
    segments: list = []
    if not is_command:
        segments.append({"type": "at", "data": {"qq": str(SELF_ID)}})
    if text:
        segments.append({"type": "text", "data": {"text": text if is_command else f" {text}"}})
    segments.extend(extra or [])

    raw = ("" if is_command else f"[CQ:at,qq={SELF_ID}] ") + text
    for seg in extra or []:
        if seg.get("type") == "image":
            raw += f"[CQ:image,file={seg.get('data', {}).get('file', '')}]"

    return {
        "time": 1790000000,
        "self_id": SELF_ID,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": GROUP_ID,
        "user_id": USER_ID,
        "raw_message": raw.strip(),
        "font": 0,
        "sender": {
            "user_id": USER_ID, "nickname": "测试员", "card": "",
            "role": "member", "sex": "unknown", "age": 0, "area": "", "level": "1",
        },
        "message": segments,
    }


def handle_action(msg: dict) -> dict:
    """响应机器人发来的 OneBot 接口调用。"""
    action = msg.get("action")
    params = msg.get("params") or {}
    echo = msg.get("echo")

    if action == "get_group_root_files":
        data = ROOT
    elif action == "get_group_files_by_folder":
        data = FOLDERS.get(str(params.get("folder_id")), {"files": [], "folders": []})
    elif action == "get_group_album_list":
        data = ALBUMS
    elif action == "get_group_album_media_list":
        data = ALBUM_MEDIA.get(str(params.get("album_id")), {"media_list": []})
    elif action == "get_essence_msg_list":
        data = ESSENCE
    elif action == "get_image":
        data = FAKE_IMAGE
    elif action in ("send_group_msg", "send_msg", "send_private_msg"):
        print(f"  【机器人回复】{extract_text(params)}")
        data = {"message_id": 9999}
    elif action == "get_login_info":
        data = {"user_id": SELF_ID, "nickname": BOT_NICKNAME}
    else:
        print(f"  [接口] {action}  <- 未伪造，返回空")
        data = {}

    return {"status": "ok", "retcode": 0, "data": data, "echo": echo}


def extract_text(params: dict) -> str:
    """从机器人发来的消息里把文本和图片抠出来，方便肉眼核对。"""
    message = params.get("message")
    if isinstance(message, str):
        return message
    parts = []
    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            parts.append(data.get("text", ""))
        elif kind == "at":
            parts.append(f"@{data.get('qq')}")
        elif kind == "image":
            parts.append(f"[图片 {data.get('file')}]")
        elif kind == "reply":
            parts.append(f"[引用 {data.get('id')}]")
    return "".join(parts)


async def connect(url: str, headers: dict):
    """建立连接，兼容 websockets 不同版本的关键字差异。

    显式传 proxy=None 关掉代理自动探测。原因：websockets 新版本会自动读取
    HTTP_PROXY / HTTPS_PROXY 环境变量，在开着代理的机器上，连本机回环地址
    （127.0.0.1:8080）也会被转到代理去，然后拿到一个莫名其妙的 502。
    本机回环地址本来就不该走代理。
    """
    import websockets

    for kwargs in (
        {"additional_headers": headers, "proxy": None},
        {"additional_headers": headers},
        {"extra_headers": headers, "proxy": None},
        {"extra_headers": headers},
    ):
        try:
            return await websockets.connect(url, **kwargs)
        except TypeError:
            continue
    raise RuntimeError("websockets 版本不兼容")


async def main() -> int:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-Self-ID": str(SELF_ID),
        "X-Client-Role": "Universal",
    }

    print(f"连接 {WS_URL} ...")
    try:
        ws = await connect(WS_URL, headers)
    except Exception as exc:  # noqa: BLE001
        print(f"连接失败：{exc!r}")
        print("请确认机器人已经启动（python bot.py），且端口和 Token 与 .env 一致。")
        return 1

    image_server = start_image_server()
    if image_server is not None:
        print(f"测试图片已托管：{IMAGE_URL}")
        print(f"  （内容是{IMAGE_DESC}，识图用例应该能描述出来）")

    print("已连上，开始测试\n")

    async with ws:
        stop = asyncio.Event()

        async def reader() -> None:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if "action" in msg:
                    await ws.send(json.dumps(handle_action(msg)))
                else:
                    print(f"  [事件] {msg.get('post_type')} {msg.get('notice_type', '')}")

        reader_task = asyncio.create_task(reader())

        try:
            for idx, item in enumerate(MESSAGES, 1):
                text, extra = item if isinstance(item, tuple) else (item, None)
                label = text or f"[纯图片 {len(extra or [])} 张]"
                print(f"[{idx}/{len(MESSAGES)}] 发送：{label}")
                await ws.send(json.dumps(make_event(text, 1000 + idx, extra)))
                await asyncio.sleep(GAP_SECONDS)
            # 收尾等待：最后一条消息的回复可能还在路上，多等一会儿再退出，
            # 否则会看不到它的回答。
            print(f"\n（等待最后一条回复，{FINAL_WAIT_SECONDS} 秒）")
            await asyncio.sleep(FINAL_WAIT_SECONDS)
        finally:
            stop.set()
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    print("\n测试结束")
    return 0


if __name__ == "__main__":
    if not Path(".env").exists():
        print("警告：当前目录下没有 .env，机器人可能起不来")
    sys.exit(asyncio.run(main()))
