"""群文件内容检索（RAG）：把群文件里的文字抽出来建索引，回答「文件里写了什么」。

和 resource_index.py 的分工：
    resource_index 回答「文件在哪个目录」（名字/目录级检索）
    本模块回答「文件内容里写了什么」（内容级检索）

流程：挑出可读的文档 → 拿 file_id 换下载链接 → 下载 → 抽文本 →
分块 → 关键词检索 → 把命中的原文段落交给模型组织语言。
「必须基于真实抽取到的原文」这条铁律由 __init__.py 的提示词兜底，
本模块的职责是保证递给模型的每一段都真的是从文件里抽出来的。

支持格式（都是纯标准库实现，环境里装不了第三方解析库）：
    .txt/.md/.csv/.json/.log/.py 等文本   直接解码（UTF-8 失败退 GBK）
    .docx                                 zip + word/document.xml 剥标签
    .pdf                                  尽力而为：解 FlateDecode 流，
                                          抽 Tj/TJ 文本操作符。
                                          中文 CID 字体的 PDF 大概率抽不出
                                          可用文字，抽不出就静默跳过，
                                          绝不把乱码喂给模型。

为什么用后台任务建索引：下载 20 个文件第一次可能要一两分钟，
不能让一条群聊消息干等。所以 get_index 立刻返回（可能是空索引），
构建在后台跑完自动生效；文本落盘缓存，重启机器人也不用重新下载。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import time
import zipfile
import zlib
from dataclasses import dataclass, field

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

# ---------------------------------------------------------------- 支持的格式

# 纯文本后缀：直接按文本解码
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py", ".js",
    ".ts", ".html", ".htm", ".xml", ".yaml", ".yml", ".ini", ".cfg",
    ".sql", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".bat",
}

# 抽不出文本就别浪费时间下载的后缀
_SKIP_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg",
    ".mp4", ".avi", ".mkv", ".mov", ".flv", ".mp3", ".wav", ".flac",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".apk", ".dmg", ".msi",
}


def ext_of(name: str) -> str:
    return os.path.splitext(name.split("?")[0])[1].lower()


def is_readable(name: str, size: int, max_size: int) -> bool:
    """判断这个文件值不值得下载去抽文本。"""
    ext = ext_of(name)
    if ext in _SKIP_EXTS:
        return False
    if ext not in TEXT_EXTS and ext not in (".docx", ".pdf"):
        # 未知后缀不看，免得把二进制拉回来浪费流量
        return False
    if size > max_size:
        return False
    return True


# ---------------------------------------------------------------- 文本抽取


def _decode_text(data: bytes) -> str:
    """文本文件解码：UTF-8 优先，失败退 GBK（国内群文件很常见），再失败退拉丁。"""
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _extract_docx(data: bytes) -> str:
    """docx = zip 包着一段 XML。只剥 word/document.xml 的标签。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""
    # 段落结束换行，其余标签全剥掉
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    # XML 实体还原最常见的几个
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&apos;", "'")):
        xml = xml.replace(ent, ch)
    return xml


_PDF_TEXT_RE = re.compile(
    rb"\((?:\\.|[^\\()])*\)\s*Tj"          # (text) Tj
    rb"|\[(?:[^\]]*)\]\s*TJ"               # [ (a) 1 (b) ] TJ
    rb"|\((?:\\.|[^\\()])*\)\s*'"          # (text) '
    rb"|\((?:\\.|[^\\()])*\)\s*\""         # (text) "
)
_PDF_STR_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")


def _pdf_unescape_bytes(raw: bytes) -> bytes:
    """还原 PDF 字符串里的转义：\\n \\t \\( \\) \\\\ 和八进制 \\ddd。"""
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != 0x5C:  # 反斜杠
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            break
        nxt = raw[i:i + 2]
        table = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b",
                 b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
        if nxt[:1] in table:
            out.extend(table[nxt[:1]])
            i += 1
        elif nxt[:1].isdigit():
            # 八进制转义，最多 3 位
            j = i
            while j < len(raw) and j < i + 3 and raw[j:j + 1].isdigit():
                j += 1
            out.append(int(raw[i:j], 8) & 0xFF)
            i = j
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _pdf_unescape(raw: bytes) -> str:
    """转义还原 + 编码猜测。

    编码判定顺序（实测踩过的坑：不能先试 UTF-16BE —— 偶数长度的纯 ASCII
    字符串如「heat」会被两两配对解成汉字乱码，还偏偏能解成功）：
    1. 全部是可打印 ASCII → 直接按单字节解；
    2. 否则尝试 GB18030（简单中文字体常用，且兼容 ASCII）；
    3. 再试 UTF-16BE（两字节编码的字符串里通常带 NUL 字节，
       它不是可打印 ASCII，所以走不到第 1 条）；
    4. 都不行就 latin-1 忽略错误兜底。
    """
    raw = _pdf_unescape_bytes(raw)
    if all(0x20 <= b <= 0x7E or b in (0x09, 0x0A, 0x0D) for b in raw):
        return raw.decode("ascii")
    for enc in ("gb18030", "utf-16-be"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="ignore")


def _extract_pdf(data: bytes) -> str:
    """尽力而为的 PDF 文本抽取。

    只处理 FlateDecode 流里的 Tj/TJ 文本操作符 —— 覆盖一部分
    英文/简单编码的 PDF。中文 PDF 多用 CID 字体 + 自定义映射，
    抽出来会是乱码，所以抽完做一次「可读率」检查，太低就整篇放弃。
    """
    parts: list[str] = []
    for m in re.finditer(rb"stream\r?\n", data):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        blob = data[start:end].rstrip(b"\r\n")
        try:
            blob = zlib.decompress(blob)
        except zlib.error:
            continue
        for tm in _PDF_TEXT_RE.finditer(blob):
            seg = tm.group(0)
            for sm in _PDF_STR_RE.finditer(seg):
                text = _pdf_unescape(sm.group(0)[1:-1])
                if text:
                    parts.append(text)
            parts.append(" ")
    text = "".join(parts)
    if not text:
        return ""
    # 可读率：字母/数字/常用中日韩字符/常用标点占比太低 = 乱码，弃用
    ok = sum(
        1 for ch in text
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" or ch in "，。；：、！？（）-—%.,;:!?()[]{}'\" \n\t"
    )
    if ok / len(text) < 0.75:
        return ""
    return text


def extract_text(data: bytes, name: str) -> str:
    """按后缀分发抽取。返回空串表示「抽不出」。"""
    ext = ext_of(name)
    if ext in TEXT_EXTS:
        return _decode_text(data)
    if ext == ".docx":
        return _extract_docx(data)
    if ext == ".pdf":
        return _extract_pdf(data)
    return ""


# ---------------------------------------------------------------- 分块


@dataclass
class Chunk:
    file_name: str
    location: str
    text: str
    offset: int  # 在该文件全文里的起始位置（用来展示「在第几段」）


def chunk_text(text: str, size: int = 450, overlap: int = 60) -> list[str]:
    """把全文切成带重叠的小块。

    带重叠的原因：答案正好骑在切块边界上的话，单看任何一块都缺一半，
    检索就漏了。重叠让边界内容同时出现在两块里。
    """
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    step = size - overlap
    for start in range(0, len(text), step):
        piece = text[start:start + size].strip()
        if len(piece) >= 30:  # 太短的尾巴没检索价值
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


# ---------------------------------------------------------------- 数据结构与缓存


@dataclass
class ContentIndex:
    group_id: int
    chunks: list[Chunk] = field(default_factory=list)
    files_done: int = 0
    files_failed: int = 0
    built_at: float = 0.0
    building: bool = False
    error: str = ""


_cache: dict[int, ContentIndex] = {}
_tasks: dict[int, asyncio.Task] = {}


def _cache_dir(group_id: int) -> str:
    base = os.path.join("data", "filetext_cache", str(group_id))
    os.makedirs(base, exist_ok=True)
    return base


def _cache_file(group_id: int, file_id: str, name: str) -> str:
    key = hashlib.sha1(f"{file_id}|{name}".encode("utf-8")).hexdigest()[:20]
    return os.path.join(_cache_dir(group_id), key + ".txt")


def invalidate(group_id: int) -> None:
    """管理命令重建时强制作废。"""
    _cache.pop(group_id, None)
    task = _tasks.pop(group_id, None)
    if task and not task.done():
        task.cancel()


# ---------------------------------------------------------------- 下载与构建


async def _download(url: str, timeout: float) -> bytes:
    """下载文件字节流。

    用标准库 urllib 在线程里跑（这个环境装不了 httpx/aiohttp，
    之前写过一版基于 httpx 的，实际 venv 里根本没有它，全部下载报
    ModuleNotFoundError）。ProxyHandler({}) 显式禁用一切代理直连，
    避免继承启动会话的本地代理变量把下载带进沟里。
    """
    import urllib.request

    def _fetch() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()

    return await asyncio.to_thread(_fetch)


async def _file_url(bot: Bot, group_id: int, file_id: str) -> str:
    """拿 file_id 换下载链接（NapCat 的 get_group_file_url）。"""
    data = await bot.call_api("get_group_file_url", group_id=group_id, file_id=file_id)
    if isinstance(data, dict):
        data = data.get("data", data)
    url = (data or {}).get("url", "") if isinstance(data, dict) else ""
    return str(url or "")


async def _build(bot: Bot, group_id: int, candidates, max_files: int,
                 max_file_size: int, chunk_size: int, timeout: float) -> ContentIndex:
    index = ContentIndex(group_id=group_id, building=True, built_at=time.time())
    for entry in candidates[:max_files]:
        cache_path = _cache_file(group_id, entry.file_id, entry.name)
        full_text = ""
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    full_text = f.read()
            except OSError:
                full_text = ""

        if not full_text:
            try:
                url = await _file_url(bot, group_id, entry.file_id)
                if not url:
                    index.files_failed += 1
                    continue
                data = await _download(url, timeout)
                full_text = extract_text(data, entry.name)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    f"[ai_chat] 文件「{entry.name}」内容抽取失败，跳过: {exc!r}"
                )
                index.files_failed += 1
                continue
            if full_text:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        f.write(full_text)
                except OSError:
                    pass  # 缓存写不进去不影响检索，下次重新下载而已

        if not full_text:
            continue
        for piece in chunk_text(full_text, size=chunk_size):
            index.chunks.append(
                Chunk(file_name=entry.name, location=entry.where, text=piece, offset=0)
            )
        index.files_done += 1

    index.building = False
    logger.info(
        f"[ai_chat] 群 {group_id} 文件内容索引完成："
        f"{index.files_done} 个文件 / {len(index.chunks)} 段"
        f"（{index.files_failed} 个抽不出或失败）"
    )
    return index


async def get_index(
    bot: Bot,
    group_id: int,
    candidates,
    *,
    max_files: int,
    max_file_size: int,
    chunk_size: int,
    timeout: float,
    ttl: float = 1800,
    force: bool = False,
) -> ContentIndex:
    """取内容索引；过期/不存在就**后台**开建，立刻返回现状。

    返回的 index.building 为 True 表示后台正在构建，
    本次检索结果可能不全 —— 提示词里会交代模型「可能还没建完」。
    就算缓存过期，已落盘的文本也还在，重建只需要下载新出现的文件。
    """
    cached = _cache.get(group_id)
    if (
        not force
        and cached
        and not cached.error
        and (cached.chunks or cached.building)
        and time.time() - cached.built_at < ttl
    ):
        return cached
    if force:
        invalidate(group_id)
    task = _tasks.get(group_id)
    if task and not task.done():
        # 已经在建了，直接返回现状
        return cached or ContentIndex(group_id=group_id, building=True)

    # 复制一份候选，避免构建期间上游索引被刷新导致列表变化
    cand = list(candidates)

    async def runner():
        try:
            built = await _build(
                bot, group_id, cand, max_files, max_file_size, chunk_size, timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 文件内容索引构建失败: {exc!r}")
            failed = ContentIndex(
                group_id=group_id, built_at=time.time(), error=str(exc)
            )
            _cache[group_id] = failed
            return
        _cache[group_id] = built
        _tasks.pop(group_id, None)

    task = asyncio.create_task(runner())
    _tasks[group_id] = task
    placeholder = ContentIndex(group_id=group_id, building=True, built_at=time.time())
    _cache[group_id] = placeholder
    return placeholder


# ---------------------------------------------------------------- 检索


def _score(text: str, core: str, terms: list[str]) -> int:
    """给一段内容打分：命中的查询子串越长越高，词频作加分。"""
    low = text.lower()
    score = 0
    # 最长命中（和 resource_index 一个思路：中文要按子串匹配）
    for length in range(min(8, len(core)), 1, -1):
        if any(core[i:i + length] in low for i in range(len(core) - length + 1)):
            score += length * 6
            break
    for term in terms:
        if term in low:
            score += 10 + min(low.count(term), 4) * 2
    return score


def _snippet(text: str, core: str, width: int = 120) -> str:
    """截出命中位置附近的一段，让用户和模型都能看到上下文。"""
    pos = text.lower().find(core[:2]) if core else -1
    if pos < 0:
        for length in range(min(4, len(core)), 1, -1):
            pos = text.lower().find(core[:length])
            if pos >= 0:
                break
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    return text[start:start + width].replace("\n", " ").strip()


def search(index: ContentIndex, query: str, limit: int = 6):
    """按查询词检索内容块，返回 [(chunk, score, snippet)]。"""
    core = re.sub(r"\s+", "", query.lower())
    if not core or not index.chunks:
        return []
    terms = [t.lower() for t in re.split(r"[\s,，、。；;：:!！?？]+", query) if len(t) >= 2]
    scored = []
    for chunk in index.chunks:
        s = _score(chunk.text, core, terms)
        if s > 0:
            scored.append((s, chunk))
    scored.sort(key=lambda pair: -pair[0])
    out = []
    for s, chunk in scored[:limit]:
        out.append((chunk, s, _snippet(chunk.text, core)))
    return out


def describe(hits) -> str:
    """把命中内容整理成给模型看的资料。"""
    lines = []
    for idx, (chunk, _, snippet) in enumerate(hits, 1):
        lines.append(
            f"{idx}. 出自文件「{chunk.file_name}」（{chunk.location}）："
            f"「{snippet}」"
        )
    return "\n".join(lines)
