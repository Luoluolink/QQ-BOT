"""群资源索引：抓取群文件 + 群相册（图片/视频），建立可本地检索的索引。

为什么这么做：
    模型无法自己去翻群文件。真正能列出资源的是 NapCat 提供的 OneBot 接口。
    所以流程是：抓取 → 建索引 → 本地关键词匹配 → 把候选交给模型组织语言。
    这样既准确（不会编造文件名），又省 token。

覆盖的资源来源：
    群文件    get_group_root_files / get_group_files_by_folder（支持多层目录）
    群相册    get_qun_album_list / get_group_album_media_list（图片 + 视频）

已知接口限制（NapCat issue #1670）：
    单个群最多只能取到 10 个相册，单个相册最多只能取到 30 个媒体，
    这是协议端自身的限制，不是本模块的问题。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from nonebot.adapters.onebot.v11 import Bot
from nonebot.log import logger

# 索引缓存时长（秒）
CACHE_TTL = 600

# 文件夹递归深度上限，防止异常结构导致无限递归
MAX_DEPTH = 4

# 相册抓取上限，避免一个相册特别多的群把接口打爆
MAX_ALBUMS = 12

# ---------------------------------------------------------------- 类型识别

_EXT_KIND = {}
for _kind, _exts in {
    "文档": (".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".wps", ".odt", ".pages"),
    "表格": (".xls", ".xlsx", ".csv", ".et", ".ods", ".numbers"),
    "演示": (".ppt", ".pptx", ".dps", ".odp", ".key"),
    "图片": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff", ".svg"),
    "视频": (".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".m4v", ".rmvb", ".3gp", ".mpg"),
    "音频": (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".amr"),
    "压缩包": (".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2"),
}.items():
    for _ext in _exts:
        _EXT_KIND[_ext] = _kind


def kind_of(name: str, url: str = "") -> str:
    """根据文件名或 URL 后缀猜类型。猜不出就归到「其他」。"""
    for candidate in (name, urlparse(url).path if url else ""):
        if not candidate:
            continue
        ext = os.path.splitext(candidate.split("?")[0])[1].lower()
        if ext in _EXT_KIND:
            return _EXT_KIND[ext]
    return "其他"


# 用户问句里出现的类型词 → 想要的资源类型
KIND_WORDS = {
    "图片": ("图片", "照片", "相片", "截图", "壁纸", "头像", "表情包", "图集"),
    "视频": ("视频", "录像", "影像", "影片", "短片", "录屏", "录播"),
    "音频": ("音频", "录音", "语音", "歌曲", "音乐"),
    "表格": ("表格", "报表", "excel", "xls", "台账", "清单"),
    "演示": ("ppt", "幻灯片", "演示文稿", "课件"),
    "文档": ("文档", "论文", "报告", "说明书", "讲义", "笔记", "手册"),
    "压缩包": ("压缩包", "压缩文件", "zip", "rar"),
}


def wanted_kinds(text: str) -> set[str]:
    """从问句里推断用户想找哪几类资源。"""
    low = text.lower()
    return {kind for kind, words in KIND_WORDS.items() if any(w in low for w in words)}


# ---------------------------------------------------------------- 数据结构


@dataclass
class ResourceEntry:
    name: str
    location: str  # 「财务/报销制度」或「群相册/团建照片」
    kind: str  # 文档 / 表格 / 演示 / 图片 / 视频 / 音频 / 压缩包 / 其他
    source: str  # 群文件 / 群相册
    size: int = 0
    uploader: str = ""
    url: str = ""
    # 群文件的 file_id：拿它调 get_group_file_url 换下载链接。
    # 只有群文件有；相册条目直接带 url，用不上这个。
    file_id: str = ""

    @property
    def where(self) -> str:
        """给用户看的位置描述。"""
        if self.source == "群相册":
            return f"群相册「{self.location}」"
        return f"群文件「{self.location}」"


@dataclass
class GroupIndex:
    group_id: int
    entries: list[ResourceEntry] = field(default_factory=list)
    built_at: float = 0.0
    error: str = ""

    @property
    def locations(self) -> list[str]:
        return sorted({e.location for e in self.entries})

    def count_of(self, source: str) -> int:
        return sum(1 for e in self.entries if e.source == source)


_cache: dict[int, GroupIndex] = {}


def fmt_size(num: int) -> str:
    if num <= 0:
        return "未知"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.0f}{units[idx]}" if idx == 0 else f"{value:.1f}{units[idx]}"


def _pick(data: dict, *keys, default=""):
    """从字典里按优先级取第一个有值的字段。

    不同 NapCat 版本返回的字段名不完全一致（比如上传者可能是
    uploader_name / uploader / uploaderNick），所以这里做宽容取值，
    而不是硬编码单一字段名。
    """
    for key in keys:
        value = data.get(key)
        if value not in (None, "", 0):
            return value
    return default


async def _call(bot: Bot, action: str, **params):
    """调用 OneBot API。

    注意：NoneBot 的 bot.call_api 已经把响应里的 data 字段拆出来了，
    所以返回值可能是 dict（对象），也可能是 list（数组）。

    早期版本只放行 dict，导致 get_group_album_list 返回的数组被当成空结果丢掉，
    群相册功能整个静默失效（不报错，只是永远查不到相册）—— 所以这里必须
    同时放行 list。
    """
    raw = await bot.call_api(action, **params)
    if isinstance(raw, dict) and isinstance(raw.get("data"), (dict, list)):
        return raw["data"]
    return raw


# ---------------------------------------------------------------- 群文件抓取


async def _walk_files(
    bot: Bot,
    group_id: int,
    node: dict,
    prefix: str,
    index: GroupIndex,
    depth: int,
) -> None:
    if depth > MAX_DEPTH or not isinstance(node, dict):
        return

    for item in node.get("files") or []:
        name = str(_pick(item, "file_name", "name", default="(未命名)"))
        index.entries.append(
            ResourceEntry(
                name=name,
                location=prefix or "根目录",
                kind=kind_of(name),
                source="群文件",
                size=int(_pick(item, "file_size", "size", default=0) or 0),
                uploader=str(_pick(item, "uploader_name", "uploader", "uploaderNick")),
                file_id=str(_pick(item, "file_id", "id")),
            )
        )

    for folder in node.get("folders") or []:
        folder_id = str(_pick(folder, "folder_id", "id"))
        folder_name = str(_pick(folder, "folder_name", "name", default="(未命名文件夹)"))
        sub_prefix = f"{prefix}/{folder_name}" if prefix else folder_name
        try:
            sub = await _call(
                bot, "get_group_files_by_folder", group_id=group_id, folder_id=folder_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 读取子目录「{sub_prefix}」失败: {exc}")
            continue
        await _walk_files(bot, group_id, sub, sub_prefix, index, depth + 1)


# ---------------------------------------------------------------- 群相册抓取


def _media_entries(album_name: str, media_list: list) -> list[ResourceEntry]:
    """把相册媒体列表转成索引条目。

    NapCat 文档只保证了 media_id 和 url 两个字段，其余（文件名、上传者、
    描述）各版本不一，所以全部走宽容取值；实在取不到名字就用
    「相册名 + 序号 + 扩展名」兜底。
    """
    out: list[ResourceEntry] = []
    for idx, media in enumerate(media_list, 1):
        if not isinstance(media, dict):
            continue

        url = str(_pick(media, "url", "originUrl", "origin_url", "thumbUrl", "thumb_url"))
        name = str(_pick(media, "file_name", "name", "desc", "title", "media_name"))

        if not name:
            ext = os.path.splitext(urlparse(url).path)[1] if url else ""
            name = f"{album_name} 第 {idx} 项{ext}"

        kind = kind_of(name, url)
        if kind == "其他":
            declared = str(_pick(media, "type", "media_type")).lower()
            if "video" in declared or "视频" in declared:
                kind = "视频"
            elif "image" in declared or "pic" in declared or "图片" in declared:
                kind = "图片"
            else:
                # 相册里绝大多数是图片，取不到线索时按图片算更合理
                kind = "图片"

        out.append(
            ResourceEntry(
                name=name,
                location=album_name,
                kind=kind,
                source="群相册",
                uploader=str(_pick(media, "uploader_name", "uploader", "nick", "uploaderNick")),
                url=url,
            )
        )
    return out


async def _fetch_albums(bot: Bot, group_id: int, index: GroupIndex) -> None:
    try:
        albums = await _call(bot, "get_qun_album_list", group_id=str(group_id))
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 该群没有群相册接口或读取失败，跳过: {exc}")
        return

    # 有的实现把列表放在 data.album_list，有的直接就是 data 本身
    if isinstance(albums, dict):
        album_list = albums.get("album_list") or albums.get("albums") or []
    elif isinstance(albums, list):
        album_list = albums
    else:
        album_list = []

    if len(album_list) > MAX_ALBUMS:
        logger.info(
            f"[ai_chat] 群 {group_id} 有 {len(album_list)} 个相册，"
            f"只取前 {MAX_ALBUMS} 个（协议端单群最多返回 10 个）"
        )

    for album in album_list[:MAX_ALBUMS]:
        if not isinstance(album, dict):
            continue
        album_id = str(_pick(album, "id", "album_id"))
        album_name = str(_pick(album, "name", "album_name", default="未命名相册"))
        if not album_id:
            continue
        try:
            data = await _call(
                bot,
                "get_group_album_media_list",
                group_id=str(group_id),
                album_id=album_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 读取相册「{album_name}」失败: {exc}")
            continue

        media_list = data.get("media_list") if isinstance(data, dict) else data
        if not isinstance(media_list, list):
            media_list = []
        index.entries.extend(_media_entries(album_name, media_list))


# ---------------------------------------------------------------- 建索引与缓存


async def build_index(bot: Bot, group_id: int) -> GroupIndex:
    index = GroupIndex(group_id=group_id, built_at=time.time())

    try:
        root = await _call(bot, "get_group_root_files", group_id=group_id)
        await _walk_files(bot, group_id, root, "", index, depth=0)
    except Exception as exc:  # noqa: BLE001
        index.error = str(exc)
        logger.warning(f"[ai_chat] 建立群文件索引失败: {exc}")
        return index

    # 相册失败不影响群文件部分，所以单独兜住异常
    await _fetch_albums(bot, group_id, index)

    logger.info(
        f"[ai_chat] 群 {group_id} 索引建立完成："
        f"群文件 {index.count_of('群文件')} 个，群相册 {index.count_of('群相册')} 个"
    )
    return index


async def get_index(
    bot: Bot, group_id: int, force: bool = False, ttl: float | None = None
) -> GroupIndex:
    """取索引，优先用缓存；force=True 强制重建。

    ttl 由调用方从配置传入；不传就用本模块的默认值。
    之前这里写死 CACHE_TTL，导致 .env 里的 AI_FILE_INDEX_TTL 改了也没用。
    """
    cached = _cache.get(group_id)
    life = CACHE_TTL if ttl is None else ttl
    if not force and cached and time.time() - cached.built_at < life:
        return cached
    index = await build_index(bot, group_id)
    _cache[group_id] = index
    return index


# ---------------------------------------------------------------- 检索

# 问句里的口水词，匹配前先去掉，避免污染打分
STOPWORDS = (
    "有没有人", "有没有", "在哪个", "在哪", "哪里", "哪个", "一下", "帮我",
    "请问", "什么", "怎么", "是不是", "群文件", "群相册", "群视频", "群图片",
    "群里", "那个", "这个", "吗", "的呢", "找找", "找一下", "搜一下", "搜搜",
)

_SPLIT_RE = re.compile(r"[\s,，、。；;：:!！?？/\\|（）()\[\]【】]+")


def _normalize(text: str) -> str:
    """去掉口水词和所有空白，只留下有信息量的部分。"""
    out = text.lower()
    for word in STOPWORDS:
        out = out.replace(word, " ")
    return re.sub(r"\s+", "", out)


def _longest_hit(needle: str, hay: str, min_len: int = 2, max_len: int = 6) -> int:
    """返回 needle 里能在 hay 中找到的最长子串长度。

    中文没有空格，「财务那边的表格」整句拿去匹配是匹配不上的，
    所以反过来找「查询里最长的一段、正好出现在文件名/目录名里」，
    命中越长说明相关度越高。
    """
    if not needle or not hay:
        return 0
    for length in range(min(max_len, len(needle)), min_len - 1, -1):
        for start in range(len(needle) - length + 1):
            if needle[start : start + length] in hay:
                return length
    return 0


# 剥掉类型词和口水词之后，如果还剩这些虚词，说明对方其实没说要找什么
_FILLER = "有是的了吗呢吧啊呀嘛在和与及我你他它它们个些点张条份部篇看要想要给发来去能会可以"


def _subject(text: str) -> str:
    """取出问句里的「主体词」——去掉口水词、类型词、虚词之后剩下的部分。

    这是区分「纯类型查询」和「带主题的查询」的关键：
        「有没有压缩包」      → 主体为空 → 纯类型查询，按类型返回即可
        「有没有量子力学的课件」→ 主体是「量子力学」→ 必须真的匹配上这个主体
    如果没有这层区分，类型加分会让「无关但类型对」的文件混进结果，
    模型就会顺着编出错误答案。
    """
    out = _normalize(text)
    for words in KIND_WORDS.values():
        for word in words:
            out = out.replace(word, "")
    for ch in _FILLER:
        out = out.replace(ch, "")
    return out.strip()


def search(index: GroupIndex, query: str, limit: int = 15) -> list[ResourceEntry]:
    """文件名优先、目录名次之的模糊检索，并考虑资源类型。

    评分规则：
        文件名命中长度 × 4     —— 命中越长越相关
        目录名/相册名命中 × 1.5 —— 「财务的表格」靠目录名也能找到
        整词命中 +12           —— 空格分词后精确命中的额外奖励
        类型相符 × 1.6         —— 问「照片」时图片排前面
        类型不符 × 0.4         —— 问「照片」时文档排到后面去

    重要：类型只用来「重新排序已有文本依据的候选」，绝不单独制造匹配。
    只有纯类型查询（「有没有压缩包」）才会按类型直接给基础分。
    """
    core = _normalize(query)
    if not core:
        return []
    terms = [t.lower() for t in _SPLIT_RE.split(query) if len(t) >= 2]
    wanted = wanted_kinds(query)
    kind_only = not _subject(query)

    scored: list[tuple[float, ResourceEntry]] = []
    for entry in index.entries:
        name = entry.name.lower()
        location = entry.location.lower()
        score = 0.0

        name_hit = _longest_hit(core, name)
        if name_hit:
            score += name_hit * 4

        location_hit = _longest_hit(core, location)
        if location_hit:
            score += location_hit * 1.5

        for term in terms:
            if term in name:
                score += 12
            elif term in location:
                score += 4

        if score <= 0:
            # 没有任何文本依据。只有「纯类型查询」才允许靠类型入选，
            # 否则直接丢弃——宁可不答，也不能给模型递错料。
            if kind_only and wanted and entry.kind in wanted:
                score = 10.0
            else:
                continue
        elif wanted and entry.kind != "其他":
            score = score * 1.6 if entry.kind in wanted else score * 0.4

        scored.append((score, entry))

    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [entry for _, entry in scored[:limit]]


def describe(entries: list[ResourceEntry]) -> str:
    """把候选资源整理成给模型看的文本。

    格式刻意写成「读起来像人话」的样子，而不是 `[类型] 名称 → 位置` 这种
    带方括号和箭头标记的形态 —— 因为实测模型会把标记原样抄进回复里，
    群成员就会看到「名为[表格] 2026年度预算表.xlsx」这种怪东西。
    现在即使被逐字照抄，读起来也是通顺的。
    """
    lines = []
    for idx, entry in enumerate(entries, 1):
        bits = [f"{entry.kind}，在{entry.where}"]
        if entry.uploader:
            bits.append(f"{entry.uploader}上传")
        if entry.size:
            bits.append(fmt_size(entry.size))
        lines.append(f"{idx}. {entry.name} —— " + "，".join(bits))
    return "\n".join(lines)
