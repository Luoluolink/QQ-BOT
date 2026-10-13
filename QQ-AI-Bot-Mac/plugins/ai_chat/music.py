"""点歌：@机器人 + 「来首/点歌/放一首/听一首 歌名」→ 分享音乐卡片到群里。

为什么用网易云音乐而不是 QQ 音乐：
- 网易云的搜索接口是公开的（music.163.com/api/search/get/web，无需签名）；
- QQ 音乐的搜索接口带签名校验，参数一变就失效，维护成本高。
两者在 QQ 里发出来的都是官方样式的音乐卡片（带封面、试听），
观感上和「小程序分享」一样，不是纯链接。

卡片不是我们自己拼的：直接发 OneBot v11 标准的 music 消息段
    {"type": "music", "data": {"type": "163", "id": "歌曲ID"}}
由 NapCat 负责拉取歌曲信息并渲染成官方卡片。发送失败时降级为文字链接。

原唱优先：网易云没有「只要原唱」的搜索参数，这里多拉 10 条候选，
按「歌名匹配度 + 疑似翻唱/现场/魔改版特征降权」打分重排，
把「翻自 xxx」「(Live)」「DJ 版」这些顶下去，优先发原唱正式版。

触发示例（priority=10 直接拦截，不进 AI 对话）：
    @机器人 来首晴天
    @机器人 点歌 晴天
    @机器人 放一首孤勇者
    @机器人 听一下起风了
    @机器人 分享音乐 晴天
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, MessageEvent
from nonebot.rule import Rule, to_me

from .config import Config
from .tools import at_me

cfg = get_plugin_config(Config)

# 意图动词必须打头（或紧跟 @ 后的正文开头），避免误伤普通聊天
# 注意：长动词要排在短动词前面（正则按顺序取第一个能匹配的分支）
MUSIC_RE = re.compile(
    r"^(?:帮我|麻烦|请|请你|麻烦你|能不能|可以|想|要|想听|给你们|给大家)*"
    r"(?:点首歌|点个歌|点歌|来首歌|来一首|来首|来个歌"
    r"|放一首|放首歌|放一下|放首|放个歌"
    r"|听一首|听首歌|听一下|听首|听个歌"
    r"|分享一首歌|分享首歌|分享歌曲|分享音乐"
    r")\s*[：:，,]?\s*(.+?)\s*(?:这首歌|这首|一下吧|吧|呗|啦|~|～)*$"
)


def _music_rule(bot: Bot, event: MessageEvent) -> bool:
    if not cfg.ai_music_enabled:
        return False
    if not isinstance(event, GroupMessageEvent):
        return False
    if not at_me(event, bot.self_id):
        return False
    text = event.get_plaintext().strip()
    return bool(MUSIC_RE.match(text))


music = on_message(rule=Rule(_music_rule), priority=10, block=True)

_SEARCH_TIMEOUT = 10.0


def _search_sync(keyword: str) -> List[Dict[str, Any]]:
    """网易云搜索（POST 表单，无签名）。返回前 10 首歌的摘要。

    多拉几条是为了给「原唱优先」留出候选池：搜索结果排序按热度/相关度，
    翻唱、Live 现场版经常混在前几名里，候选池太小就没得挑了。
    """
    form = urllib.parse.urlencode(
        {"s": keyword, "type": 1, "offset": 0, "limit": 10}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://music.163.com/api/search/get/web",
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
        },
        method="POST",
    )
    # 本机直连不该走系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=_SEARCH_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    data = json.loads(raw)
    songs = ((data.get("result") or {}).get("songs")) or []
    out: List[Dict[str, Any]] = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        artists = "/".join(
            a.get("name", "") for a in (s.get("artists") or []) if isinstance(a, dict)
        )
        out.append(
            {
                "id": str(s.get("id") or ""),
                "name": str(s.get("name") or ""),
                "artists": artists,
                "album": str((s.get("album") or {}).get("name") or ""),
            }
        )
    return out


# 疑似「非原唱正式版」的特征标记：歌名/专辑/歌手里出现就降权。
# 中文标记直接子串匹配；英文单词用词边界，避免误伤歌名里恰好含这些字母的歌。
# 注：歌名里带「(原唱 xxx)」的必是翻唱，原唱自己不会这么起名。
_NOT_ORIGINAL_PAT = re.compile(
    r"翻自|翻唱|现场|纯享|伴奏|钢琴版|吉他版|八音盒|纯音乐|抖音|燃剪|慢速|加速|降调|升调"
    r"|女声版|男声版|童声版|深情版|清唱|原唱"
    r"|\b(?:live|cover|remix|demo|inst|dj)\b",
    re.IGNORECASE,
)


def _score(song: Dict[str, Any], kw: str) -> int:
    """给候选歌打分：歌名匹配度加分，翻唱/现场/魔改版特征降分。"""
    name = song["name"].strip().lower()
    s = 0
    if name == kw:
        s += 10  # 歌名一字不差：大概率是正式版
    elif kw in name:
        s += 6
    else:
        s -= 2  # 歌名不沾边：基本是搜索凑数的结果
    blob = f"{song['name']} {song['album']} {song['artists']}".lower()
    hits = _NOT_ORIGINAL_PAT.findall(blob)
    s -= min(8, 4 * len(hits))  # 命中越多越可疑，封顶扣 8 分
    return s


def _pick(songs: List[Dict[str, Any]], keyword: str) -> Dict[str, Any] | None:
    """原唱优先：按打分选最高分；同分时保持网易云的相关度排序（先出现的赢）。"""
    kw = keyword.strip().lower()
    best: Dict[str, Any] | None = None
    best_score = 0
    for s in songs:
        sc = _score(s, kw)
        if best is None or sc > best_score:
            best, best_score = s, sc
    return best


@music.handle()
async def _handle_music(bot: Bot, event: GroupMessageEvent) -> None:
    raw = event.get_plaintext().strip()
    m = MUSIC_RE.match(raw)
    keyword = (m.group(1) if m else "").strip()
    if not keyword or len(keyword) > 50:
        await music.finish("告诉我歌名呀，比如：来首晴天 🎵")

    try:
        songs = await asyncio.to_thread(_search_sync, keyword)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 网易云搜索「{keyword}」失败: {exc!r}")
        await music.finish("搜索歌曲的时候出了点问题……稍后再试试？")
        return

    song = _pick(songs, keyword)
    if not song:
        await music.finish(f"没搜到「{keyword}」这首歌，换个关键词试试？")

    label = song["name"]
    if song["artists"]:
        label += f" - {song['artists']}"

    # OneBot v11 标准 music 段：卡片由 NapCat 渲染，样式同官方分享
    card = MessageSegment.music("163", song["id"])
    try:
        await bot.send(event, Message(card))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 音乐卡片发送失败({song['id']}): {exc!r}")

    # 降级：卡片发不出去就给文字链接（部分 NapCat 版本可能不支持 music 段）
    await music.finish(
        f"🎵 {label}\n"
        f"卡片发不出去（可能是 NapCat 版本问题），给你链接：\n"
        f"https://music.163.com/song?id={song['id']}"
    )
