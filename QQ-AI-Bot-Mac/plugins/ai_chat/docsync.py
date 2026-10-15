"""腾讯文档同步：把链接收藏按文档现有格式追加进群绑定的在线文档。

对接的是腾讯文档官方 OpenAPI（docs.qq.com/open，个人开发者免费额度
20000 次/月），不是逆向接口：

鉴权（个人开发者直发凭证模式，docs.qq.com/open 首页直接给三样）：
    Access-Token: <access_token，30 天有效，到期去开放平台首页点「重置」>
    Client-Id:    <client_id / 应用ID>
    Open-Id:      <授权用户的 open_id>
配置写在 .env：AI_DOCS_CLIENT_ID / AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID。
access_token 是带过期时间的 JWT，本模块自动解析 exp 判断是否过期，
过期后抛 DocsAuthError 提示重置，不做自动续期（该模式没有 refresh_token）。

用到的接口：
    POST /openapi/drive/v2/files            建文档（form: title + type=doc）
    GET  /openapi/drive/v2/files/{id}/metadata   查文件信息（验证权限）
    GET  /openapi/doc/v3/{fileId}           读文档 → 文档树（begin/end）+ version
    POST /openapi/doc/v3/{fileId}:batchUpdate   追加内容（insertText at index）

「格式跟随」策略（本模块的核心）：
    1. 读出文档纯文本；
    2. 空白文档 → 写入自己的默认格式（标题 + 格式说明 + 首条）；
    3. 非空白 → 找文中最新的含链接行，解析它的「形状」——列表符号、
       标题在前还是在后、markdown 链接、标题行+链接行、带日期行——
       新条目照这个形状生成；实在解析不出来就用默认格式。

群与文档的绑定关系存 data/doc_binding.json，指令：
    /绑定文档 <docs.qq.com/doc/xxx 链接>   —— 管理员绑定已有文档
    /创建文档 [标题]                        —— 管理员让机器人新建一篇并绑定
    /文档                                   —— 查看绑定状态
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import to_me

from .config import Config

cfg = get_plugin_config(Config)

_TOKEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "tdoc_token.json"
_BINDING_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "doc_binding.json"
_FILTER_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "doc_filter.json"

_OAUTH_TOKEN_URL = "https://docs.qq.com/oauth/v2/token"
_DRIVE_BASE = "https://docs.qq.com/openapi/drive/v2"
_DOC_BASE = "https://docs.qq.com/openapi/doc/v3"
# 表格（sheet）专用：读 sheets-info/写区域走 sheetbook v2，读区域走 spreadsheet v3
_SHEETBOOK = "https://docs.qq.com/openapi/sheetbook/v2"
_SPREAD_V3 = "https://docs.qq.com/openapi/spreadsheet/v3"

# access_token 过期前提前这么多秒就续期，避免临界点请求失败
_EXPIRY_MARGIN = 300.0
# 所有 HTTP 调用的超时
_HTTP_TIMEOUT = 15.0

_token: dict[str, Any] | None = None
_bindings: dict[str, dict[str, Any]] | None = None


class DocsNotConfigured(Exception):
    """AI_DOCS_* 三件套没配齐。"""


class DocsAuthError(Exception):
    """refresh_token 换 token 失败（凭证失效/被撤销）。"""


class DocsApiError(Exception):
    """OpenAPI 调用失败（网络/权限/参数）。"""


# ================================================================ 存取层


def _load_token() -> dict[str, Any]:
    global _token
    if _token is None:
        try:
            raw = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
            _token = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _token = {}
    return _token


def _save_token() -> None:
    try:
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(
            json.dumps(_token or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _bindings_map() -> dict[str, dict[str, Any]]:
    global _bindings
    if _bindings is None:
        try:
            raw = json.loads(_BINDING_PATH.read_text(encoding="utf-8"))
            _bindings = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _bindings = {}
    return _bindings


def _save_bindings() -> None:
    try:
        _BINDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BINDING_PATH.write_text(
            json.dumps(_bindings_map(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def configured() -> bool:
    """凭证是否齐了（access_token 可以来自 .env 或已落盘的缓存）。"""
    if not cfg.ai_docs_enabled:
        return False
    if not cfg.ai_docs_client_id:
        return False
    tok = _load_token()
    return bool(cfg.ai_docs_access_token or tok.get("access_token"))


def binding(gid: int) -> dict[str, Any] | None:
    return _bindings_map().get(str(gid))


# ================================================================ 收录过滤
#
# 聊天里的链接鱼龙混杂，全塞进共享文档很快会变垃圾场。分两层：
#   本地收藏库（linkarchive）：永远全收 —— 搜索/翻历史不受影响；
#   在线文档同步：过 should_sync() 这道闸，三档模式：
#       all       全同步
#       whitelist 域名白名单才同步（默认，预填常用内容站）
#       manual    从不自动同步，管理员手动收录
#   白/黑名单支持群级覆盖（data/doc_filter.json），.env 里的是全局默认。

# 白名单没配置时的内置兜底：常见「值得留档」的内容站。
# 设计成元组便于测试；群级或 .env 一旦配置就整体覆盖这份默认。
DEFAULT_WHITELIST: tuple[str, ...] = (
    "bilibili.com", "b23.tv", "github.com", "juejin.cn",
    "zhihu.com", "sspai.com", "mp.weixin.qq.com", "jikipedia.moe",
)

_MODES = ("all", "whitelist", "manual")

_filter: dict[str, dict[str, Any]] | None = None


def _filter_map() -> dict[str, dict[str, Any]]:
    global _filter
    if _filter is None:
        try:
            raw = json.loads(_FILTER_PATH.read_text(encoding="utf-8"))
            _filter = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            _filter = {}
    return _filter


def _save_filter() -> None:
    try:
        _FILTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FILTER_PATH.write_text(
            json.dumps(_filter_map(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _split_domains(raw: str) -> list[str]:
    """逗号/空格分隔的域名配置 → 干净的小写域名列表。"""
    return [
        d.strip().lower().lstrip(".")
        for d in re.split(r"[,，\s]+", raw or "")
        if d.strip()
    ]


def _domain_of(url: str) -> str:
    """取 URL 的注册域名：小写、去端口、去 www. 前缀。"""
    host = urllib.parse.urlparse(url).netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_matches(domain: str, list_: list[str]) -> bool:
    """bilibili.com 应同时命中 www.bilibili.com 和 m.bilibili.com，但不要命中
    fakebilibili.com —— 所以按「等于或以其为后缀」匹配。"""
    return any(domain == d or domain.endswith("." + d) for d in list_)


def _group_lists(gid: int) -> tuple[list[str], list[str]]:
    """本群生效的 (白名单, 黑名单)。群级配置覆盖 .env 全局默认。"""
    conf = _filter_map().get(str(gid)) or {}
    wl = conf.get("whitelist")
    bl = conf.get("blacklist")
    wl_list = [str(x) for x in wl] if isinstance(wl, list) else _split_domains(cfg.ai_docs_domain_whitelist)
    bl_list = [str(x) for x in bl] if isinstance(bl, list) else _split_domains(cfg.ai_docs_domain_blacklist)
    if not wl_list and not isinstance(wl, list):
        wl_list = list(DEFAULT_WHITELIST)
    return wl_list, bl_list


def sync_mode(gid: int) -> str:
    conf = _filter_map().get(str(gid)) or {}
    mode = conf.get("mode") or cfg.ai_docs_sync_mode
    return mode if mode in _MODES else "whitelist"


def should_sync(gid: int, url: str) -> tuple[bool, str]:
    """判定这条链接该不该自动写进在线文档。返回 (是否同步, 原因)。"""
    mode = sync_mode(gid)
    if mode == "manual":
        return False, "manual"
    domain = _domain_of(url)
    wl, bl = _group_lists(gid)
    if _domain_matches(domain, bl):
        return False, "blacklist"
    if mode == "all":
        return True, "all"
    if _domain_matches(domain, wl):
        return True, "whitelist"
    return False, "unlisted"


def set_filter(
    gid: int,
    mode: str | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """改本群的过滤配置。「加」= 收进白名单（同时移出黑名单）；
    「删」= 拉进黑名单（同时移出白名单）。域名可以是裸域名或完整链接。"""
    if mode is not None and mode not in _MODES:
        raise ValueError(f"未知模式 {mode!r}")
    conf = _filter_map().setdefault(str(gid), {})
    if mode is not None:
        conf["mode"] = mode
    if add or remove:
        wl, bl = _group_lists(gid)

        def clean(d: str) -> str:
            return _domain_of(d if "://" in d else "https://" + d)

        for d in add or []:
            d = clean(d)
            if d and d not in wl:
                wl.append(d)
            bl[:] = [x for x in bl if x != d]
        for d in remove or []:
            d = clean(d)
            if d and d not in bl:
                bl.append(d)
            wl[:] = [x for x in wl if x != d]
        conf["whitelist"] = wl
        conf["blacklist"] = bl
    _save_filter()


def filter_text(gid: int) -> str:
    """/文档过滤 无参时的人话状态。"""
    mode = sync_mode(gid)
    wl, bl = _group_lists(gid)
    mode_desc = {
        "all": "全部同步（群里发的所有链接都写进文档）",
        "whitelist": "白名单模式（只有白名单域名的链接才写进文档，本地收藏库不受影响）",
        "manual": "手动模式（从不自动写文档；对我说「把 xx 链接收进文档」才写）",
    }[mode]
    lines = [f"文档收录过滤：{mode_desc}"]
    if mode != "manual":
        lines.append("白名单：" + ("、".join(wl) if wl else "（空）"))
        if bl:
            lines.append("黑名单：" + "、".join(bl))
    lines.append(
        "改法：/文档过滤 模式 all|白名单|手动；/文档过滤 加 域名；/文档过滤 删 域名（管理员）"
    )
    return "\n".join(lines)


def _is_admin(event: GroupMessageEvent) -> bool:
    """群主/管理员/SUPERUSERS 可用（和 __init__._is_bot_admin 同规则；
    单独复制一份避免循环导入）。"""
    role = getattr(event.sender, "role", "") or ""
    if role in ("owner", "admin"):
        return True
    from nonebot import get_driver

    return str(event.user_id) in get_driver().config.superusers


# ================================================================ HTTP


def _http_json_blocking(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    payload: dict | None = None,
) -> dict:
    """标准库 HTTP。form 用 urlencoded（drive 接口），payload 用 JSON（doc 接口）。"""
    data: bytes | None = None
    hdrs = {"Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        body = resp.read(2 * 1024 * 1024)
    return json.loads(body.decode("utf-8"))


async def _http_json(**kwargs) -> dict:
    return await asyncio.to_thread(_http_json_blocking, **kwargs)


# ================================================================ token


def _jwt_exp(token: str) -> float:
    """从 JWT 里解出 exp（解不了返回 0，视为未知、不过期拦截）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _reset_hint() -> str:
    return (
        "请到 docs.qq.com/open 首页「开发者信息」里点 access_token 的「重置」"
        "生成新 token，然后更新 .env 里的 AI_DOCS_ACCESS_TOKEN 并重启机器人"
    )


async def _auth_headers() -> dict[str, str]:
    """返回三个鉴权 header。token 过期直接报错提示重置（无自动续期）。"""
    if not configured():
        raise DocsNotConfigured("腾讯文档 OpenAPI 凭证未配置")
    tok = _load_token()
    open_id = tok.get("open_id") or cfg.ai_docs_open_id
    if not open_id:
        raise DocsAuthError("缺 open_id（.env 里填 AI_DOCS_OPEN_ID）")
    # .env 里的 token 优先级更高（用户重置后会更新它）；缓存里有过期时间的兜底
    token = cfg.ai_docs_access_token or tok.get("access_token") or ""
    if not token:
        raise DocsAuthError("缺 access_token，" + _reset_hint())
    expires_at = tok.get("expires_at") or 0
    if cfg.ai_docs_access_token and cfg.ai_docs_access_token != tok.get("access_token"):
        # .env 换了新 token → 重新解析 exp 并落盘
        expires_at = _jwt_exp(token)
        tok.update({"access_token": token, "expires_at": expires_at})
        if cfg.ai_docs_open_id:
            tok["open_id"] = cfg.ai_docs_open_id
        _save_token()
    if expires_at and time.time() >= expires_at - _EXPIRY_MARGIN:
        raise DocsAuthError(
            f"access_token 已过期（{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}），"
            + _reset_hint()
        )
    return {
        "Access-Token": token,
        "Client-Id": cfg.ai_docs_client_id,
        "Open-Id": open_id,
    }


def set_access_token(token: str, open_id: str = "", expires_at: float = 0.0) -> None:
    """把凭证写进缓存（open_id 必须保存，接口要用）。"""
    tok = _load_token()
    tok.update(
        {
            "access_token": token,
            "open_id": open_id or tok.get("open_id") or "",
            "expires_at": expires_at or _jwt_exp(token),
        }
    )
    _save_token()


# ================================================================ 文档操作


def parse_file_id(text: str) -> str:
    """从 docs.qq.com 链接里抽 file_id（/doc/xxx 或 /sheet/xxx 的最后一段）。"""
    m = re.search(r"docs\.qq\.com/(?:doc|sheet|slide)/([A-Za-z0-9]+)", text or "")
    return m.group(1) if m else ""


def parse_kind(text: str) -> str:
    """从链接判断文档类型：'doc' / 'sheet' / ''（认不出）。"""
    if re.search(r"docs\.qq\.com/sheet/", text or ""):
        return "sheet"
    if re.search(r"docs\.qq\.com/doc/", text or ""):
        return "doc"
    return ""


async def _doc_tree(file_id: str) -> dict:
    headers = await _auth_headers()
    data = await _http_json(url=f"{_DOC_BASE}/{file_id}", headers=headers)
    if isinstance(data.get("document"), dict):
        return data
    raise DocsApiError(f"读文档返回异常: {str(data)[:200]}")


def _tree_text(node: dict, out: list[str]) -> None:
    """深度优先把文档树拍平成纯文本。Text 节点给字，Paragraph 节点给换行。"""
    ntype = node.get("type")
    if ntype == "Text":
        out.append(str(node.get("text") or ""))
    for child in node.get("children") or []:
        if isinstance(child, dict):
            _tree_text(child, out)
    if ntype == "Paragraph":
        out.append("\n")


async def read_doc(file_id: str) -> tuple[str, int, int]:
    """返回 (纯文本, 文档末尾位置 index, version)。"""
    data = await _doc_tree(file_id)
    doc = data["document"]
    out: list[str] = []
    _tree_text(doc, out)
    return "".join(out), int(doc.get("end") or 0), int(data.get("version") or 0)


async def append_text(file_id: str, text: str) -> None:
    """在文档末尾追加一段文本。index 用读到的 end，version 用读到的版本。"""
    _, end, version = await read_doc(file_id)
    headers = await _auth_headers()
    body = {
        "requests": [{"insertText": {"text": text, "location": {"index": end}}}],
        "version": version,
    }
    await _http_json(
        url=f"{_DOC_BASE}/{file_id}:batchUpdate",
        method="POST",
        headers=headers,
        payload=body,
    )


# ---- 表格（在线表格/sheet）操作 ----
# 官方接口（实测验证 2026-10）：
#   GET  /openapi/drive/v2/util/converter?type=2&value=<encodedID>  URL里的ID→bookID
#   GET  /openapi/sheetbook/v2/{bookID}/sheets-info                 工作表列表+行列数
#   GET  /openapi/spreadsheet/v3/files/{bookID}/{sheetID}/{A1}      读区域
#   PUT  /openapi/sheetbook/v2/{bookID}/values/{sheetID}!{A1}       写区域（二维数组）
# 注意：sheet 的 fileID（bookID，形如 300000000$XXX）和链接里的 encodedID
# （形如 DXXXX）不是同一个东西，URL 进来必须先转换。


def _col_letter(n: int) -> str:
    """1-based 列号 → A1 表示法的字母（1→A, 26→Z, 27→AA）。"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


async def _to_book_id(encoded_id: str) -> str:
    """URL 里的 encodedID → sheet 接口要用的 bookID。"""
    headers = await _auth_headers()
    data = await _http_json(
        url=f"{_DRIVE_BASE}/util/converter?type=2&value={urllib.parse.quote(encoded_id, safe='')}",
        headers=headers,
    )
    if data.get("ret") == 0:
        book = str((data.get("data") or {}).get("fileID") or "")
        if book:
            return book
    raise DocsApiError(f"fileID 转换失败: {str(data)[:150]}")


async def _sheet_list(book_id: str) -> list[dict[str, Any]]:
    """工作表列表，每项含 sheetID/title/rowCount/columnCount。"""
    headers = await _auth_headers()
    data = await _http_json(
        url=f"{_SHEETBOOK}/{urllib.parse.quote(book_id, safe='')}/sheets-info",
        headers=headers,
    )
    if data.get("ret") != 0:
        raise DocsApiError(f"读工作表列表失败: {str(data)[:150]}")
    sheets = (data.get("data") or {}).get("sheetData") or []
    return [s for s in sheets if isinstance(s, dict)]


async def _sheet_read(book_id: str, sid: str, n_rows: int, n_cols: int) -> list[list[str]]:
    """读区域 → 二维文本表（空单元格补空串，行内长度统一成 n_cols）。"""
    headers = await _auth_headers()
    a1 = f"A1:{_col_letter(n_cols)}{max(n_rows, 1)}"
    path = urllib.parse.quote(f"{book_id}/{sid}/{a1}", safe="")
    data = await _http_json(url=f"{_SPREAD_V3}/files/{path}", headers=headers)
    grid = data.get("gridData") or {}
    out: list[list[str]] = []
    for row in grid.get("rows") or []:
        vals: list[str] = []
        for cell in (row.get("values") or [])[:n_cols]:
            cv = cell.get("cellValue") or {}
            vals.append(str(cv.get("text") or ""))
        vals += [""] * (n_cols - len(vals))
        out.append(vals)
    return out


async def _sheet_write(book_id: str, sid: str, start_row: int, values: list[list[str]]) -> None:
    """从第 start_row 行第 1 列开始写入一块二维数据。"""
    headers = await _auth_headers()
    n_rows = len(values)
    n_cols = max(len(r) for r in values)
    a1 = f"{_col_letter(1)}{start_row}:{_col_letter(n_cols)}{start_row + n_rows - 1}"
    path = urllib.parse.quote(f"{book_id}/values/{sid}!{a1}", safe="")
    await _http_json(
        url=f"{_SHEETBOOK}/{path}",
        method="PUT",
        headers=headers,
        payload={"values": values},
    )


# 表头关键词 → 功能列。机器人只知道页面标题和链接，分类列留空。
_SHEET_HEADER_KEYS: dict[str, tuple[str, ...]] = {
    "name": ("名字", "名称", "标题", "网站", "资源"),
    "cat": ("分类", "类别", "类型"),
    "intro": ("介绍", "说明", "描述", "简介", "内容"),
    "link": ("链接", "网址", "地址", "url"),
}
_SHEET_DEFAULT_HEADER = ("名字", "分类", "介绍", "链接")


def _map_header(header: list[str]) -> dict[str, int] | None:
    """识别表头行：返回 功能名→列下标；认不出链接列就返回 None。"""
    mapping: dict[str, int] = {}
    for func, keys in _SHEET_HEADER_KEYS.items():
        for i, cell in enumerate(header):
            if cell.strip() and any(k in cell.strip().lower() for k in keys):
                mapping[func] = i
                break
    return mapping if "link" in mapping else None


async def _sheet_append_entry(gid: int, title: str, url: str, info: dict[str, Any]) -> str:
    """把一条链接追加进绑定表格：读表头识别「名字/分类/介绍/链接」各在哪列，
    在最后一行下面照列写入；空表先写默认表头；URL 已存在则跳过（dup）。"""
    book_id = str(info.get("book_id") or "")
    if not book_id:
        book_id = await _to_book_id(info["file_id"])
        info["book_id"] = book_id
        _save_bindings()
    sheets = await _sheet_list(book_id)
    sheet = next((s for s in sheets if s.get("isVisible")), sheets[0] if sheets else None)
    if not sheet:
        return "错误: 表格里没有可见的工作表"
    sid = str(sheet["sheetID"])
    rc = int(sheet.get("rowCount") or 0)   # 有数据的最后一行
    cc = int(sheet.get("columnCount") or 0)
    n_cols = max(cc, len(_SHEET_DEFAULT_HEADER))

    rows = await _sheet_read(book_id, sid, rc, n_cols) if rc else []
    header = rows[0] if rows else []
    mapping = _map_header(header)

    # 判重：任何一行的任何单元格出现过这个 URL 就不重复写
    if any(url in cell for row in rows for cell in row):
        return "dup"

    if not mapping:
        if rc == 0:
            # 空表 → 先写默认表头，数据从第 2 行开始
            await _sheet_write(book_id, sid, 1, [list(_SHEET_DEFAULT_HEADER)])
            rows, header = [], []
            mapping = {"name": 0, "cat": 1, "intro": 2, "link": 3}
            target = 2
        else:
            # 有数据但没有可识别的表头 → 照 A-D 列尽力写
            mapping = {"name": 0, "cat": 1, "intro": 2, "link": 3}
            target = rc + 1
    else:
        target = rc + 1

    name = title[:20] if title else url
    entry = [""] * n_cols
    entry[mapping["link"]] = url
    entry[mapping["name"]] = name
    if "intro" in mapping:
        entry[mapping["intro"]] = title
    # 分类列机器人判不了，留空给人填
    await _sheet_write(book_id, sid, target, [entry])
    logger.info(f"[ai_chat] 表格收录: {name[:30]} -> 工作表「{sheet.get('title')}」第 {target} 行")
    return f"ok:第{target}行"


# ---- 读文档/表格（供意图路由 read_doc 动作使用）----

_READ_MAX_ROWS = 500      # 表格最多往下读多少行，防止超大表拖爆
_READ_LINE_CLIP = 60      # 汇报里每行最多显示多少字


def _clip(s: str, n: int = _READ_LINE_CLIP) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:n] + "…" if len(s) > n else s


def _pick_rows(
    rows: list[list[str]], keyword: str, limit: int
) -> tuple[int, list[str], list[list[str]]]:
    """从表格里挑要汇报的行：(数据总条数, 表头行, 挑出的数据行)。

    第一行当表头；keyword 非空 → 数据行里过滤包含关键词的行
    （不区分大小写，任意单元格命中即可）；否则取最后 limit 行（最新的在底部）。"""
    nonempty = [r for r in rows if any(str(c).strip() for c in r)]
    if not nonempty:
        return 0, [], []
    header = [str(c).strip() for c in nonempty[0]]
    data = nonempty[1:]
    kw = keyword.strip().lower()
    hit = [r for r in data if any(kw in str(c).lower() for c in r)] if kw else data
    return len(data), header, hit[-limit:]


def _rows_report(
    header: list[str], entries: list[list[str]], total: int, keyword: str, url: str
) -> str:
    """把挑出的数据行拼成人话汇报（表头行当列名用）。"""
    if not entries:
        return f"表格里还没有收录任何内容：{url}"
    lines = []
    for i, r in enumerate(entries, 1):
        # 有表头 → 「列名: 值」拼一起；没有 → 直接拼值
        if header and len(header) == len(r):
            cells = [
                f"{header[j]}:{_clip(r[j], 40)}"
                for j in range(len(r))
                if str(r[j]).strip()
            ]
        else:
            cells = [_clip(c, 40) for c in r if str(c).strip()]
        if not cells:
            continue
        lines.append(f"{i}. " + "｜".join(cells))
    if not lines:
        return f"表格里还没有收录任何内容：{url}"
    if keyword.strip():
        head = f"找到这些和「{keyword}」相关的"
        tail = f"共 {total} 条，展示 {len(lines)} 条"
    else:
        head = f"最近收录的 {len(lines)} 条"
        tail = f"共 {total} 条"
    return f"{head}：\n" + "\n".join(lines) + f"\n({tail})\n完整表格: {url}"


async def _sheet_report(info: dict[str, Any], keyword: str, limit: int) -> str:
    book_id = str(info.get("book_id") or "")
    if not book_id:
        book_id = await _to_book_id(info["file_id"])
        info["book_id"] = book_id
        _save_bindings()
    sheets = await _sheet_list(book_id)
    sheet = next((s for s in sheets if s.get("isVisible")), sheets[0] if sheets else None)
    if not sheet:
        return "表格里没有可见的工作表"
    sid = str(sheet["sheetID"])
    rc = min(int(sheet.get("rowCount") or 0), _READ_MAX_ROWS)
    cc = int(sheet.get("columnCount") or 0)
    n_cols = max(cc, len(_SHEET_DEFAULT_HEADER))
    rows = await _sheet_read(book_id, sid, rc, n_cols) if rc else []
    total, header, picked = _pick_rows(rows, keyword, limit)
    if total == 0:
        return f"表格里还没有收录任何内容：{info.get('url', '')}"
    if not picked:
        return f"表格里共 {total} 条，但没有和「{keyword}」相关的"
    return _rows_report(header, picked, total, keyword, info.get("url", ""))


async def _doc_report(info: dict[str, Any], keyword: str, limit: int) -> str:
    text, _, _ = await read_doc(info["file_id"])
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"文档还是空的：{info.get('url', '')}"
    kw = keyword.strip().lower()
    if kw:
        hits = [ln for ln in lines if kw in ln.lower()]
        if not hits:
            return f"文档里没有提到「{keyword}」"
        shown = hits[-limit:]
        body = "\n".join(f"{i}. {_clip(ln)}" for i, ln in enumerate(shown, 1))
        return (
            f"文档里和「{keyword}」相关的有 {len(hits)} 处"
            f"（展示最近 {len(shown)} 处）：\n{body}\n完整文档: {info.get('url', '')}"
        )
    shown = lines[-limit:]
    body = "\n".join(f"{i}. {_clip(ln)}" for i, ln in enumerate(shown, 1))
    return (
        f"文档末尾的内容（共 {len(lines)} 行，展示最后 {len(shown)} 行）：\n{body}\n"
        f"完整文档: {info.get('url', '')}"
    )


async def read_bound_text(gid: int, keyword: str = "", limit: int = 8) -> str:
    """读本群绑定的在线文档/表格，返回人话汇报（给意图路由 read_doc 用）。"""
    info = binding(gid)
    if not info:
        return "本群还没绑定在线文档：先对我说「绑定文档」或「创建文档」"
    kind = info.get("kind") or parse_kind(info.get("url") or "") or "doc"
    try:
        if kind == "sheet":
            return await _sheet_report(info, keyword, limit)
        return await _doc_report(info, keyword, limit)
    except DocsNotConfigured:
        return "腾讯文档 API 还没配置好（.env 需要 AI_DOCS 三项凭证）"
    except DocsAuthError as exc:
        return f"凭证失效了，需要重新生成 access_token 填进 .env（{exc}）"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 读绑定文档失败: {exc!r}")
        return f"读的时候出了点问题：{exc}\n如果是权限问题，确认我的授权账号还是文档协作者"


# ---- 条目格式 ----

_DATE_PAT = re.compile(r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?")
_URL_PAT = re.compile(r"https?://\S+")
_MD_LINK_PAT = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BULLET_PAT = re.compile(r"^\s*(-|\*|•|·|\d+[.、)])\s*")


def _default_entry(title: str, url: str, by: str, today: str) -> str:
    return f"「{title}」\n{today} · {by}\n{url}"


def _entry_from_template(tmpl_line: str, title: str, url: str, by: str, today: str) -> str:
    """照着模板行的形状生成新条目（只处理单行内的形状）。

    编号列表会自增序号：「1. python入门：url」的下一行是「2. 新标题：url」。
    """
    md = _MD_LINK_PAT.search(tmpl_line)
    bullet_m = _BULLET_PAT.match(tmpl_line)
    bullet = bullet_m.group(0) if bullet_m else ""
    if bullet:
        # 编号列表自增序号：「1.」的下一行用「2.」；其他符号（- * •）原样
        num_m = re.search(r"\d+", bullet)
        if num_m:
            bullet = bullet.replace(num_m.group(0), str(int(num_m.group(0)) + 1), 1)
    body = tmpl_line[bullet_m.end() :] if bullet_m else tmpl_line
    if md:
        return f"{bullet}[{title}]({url})"
    mu = _URL_PAT.search(body)
    if not mu:
        return ""
    before = body[: mu.start()].strip()
    after = body[mu.end() :].strip()
    if not before and not after:
        # 链接独占一行 → 多行形状，交给 _entry_multiline
        return ""
    if after:
        # url 在前：url 标题
        return f"{bullet}{url} {title}"
    # 标题在前：可能有「python入门：url」「- 旧文 url」等
    if re.search(r"[：:]\s*$", before):
        return f"{bullet}{title}：{url}"
    return f"{bullet}{title} {url}"


def _entry_multiline(lines: list[str], idx: int, title: str, url: str, by: str, today: str) -> str:
    """链接独占一行时的形状：标题行 + （可选日期行）+ 链接行。"""
    # 往上找：紧邻的非空行当作标题行，再往前若是纯日期行也一并保留形状
    parts: list[str] = []
    j = idx - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0 or _URL_PAT.search(lines[j]):
        return ""
    has_date = False
    if j - 1 >= 0 and _DATE_PAT.fullmatch(lines[j - 1].strip()) and not _URL_PAT.search(lines[j - 1]):
        parts.append(today)
        has_date = True
        j -= 1
    title_line = lines[j].strip()
    m = _BULLET_PAT.match(title_line)
    parts.append(f"{m.group(0) if m else ''}{title}")
    if not has_date and _DATE_PAT.search(title_line):
        pass  # 标题行里带日期的情况不动，保持简单
    parts.append(url)
    return "\n".join(parts)


def _format_entry(text: str, title: str, url: str, by: str, today: str) -> tuple[str, bool]:
    """按文档现有格式生成新条目。返回 (条目文本, 是否命中了现有格式)。"""
    lines = text.split("\n")
    url_line_idx = [i for i, l in enumerate(lines) if _URL_PAT.search(l)]
    if not url_line_idx:
        return _default_entry(title, url, by, today), False
    idx = url_line_idx[-1]
    line = lines[idx].strip()
    single = _entry_from_template(line, title, url, by, today)
    if single:
        return single, True
    multi = _entry_multiline(lines, idx, title, url, by, today)
    if multi:
        return multi, True
    return _default_entry(title, url, by, today), False


_HEADER_BLOCK = (
    "📚 群链接收藏\n"
    "\n"
    "（本节由群机器人自动维护，新链接自动追加在文末）\n"
    "\n"
)


# ================================================================ 对外动作


async def bind_doc(gid: int, url_or_id: str) -> dict[str, Any]:
    """绑定已有文档/表格。按链接类型分流验证，读不了就报错（提示加协作者）。

    只接受 docs.qq.com 链接（或裸 file_id）——按文档名字绑定不可靠：
    云盘搜索是模糊匹配且搜不到协作者共享的文件，宁可明确让用户发链接。
    """
    raw = (url_or_id or "").strip()
    fid = parse_file_id(raw)
    if not fid and re.fullmatch(r"[A-Za-z0-9_$-]{8,}", raw):
        fid = raw  # 裸 file_id（例如 300000000$XXXX）
    if not fid:
        if re.search(r"[\u4e00-\u9fff]", raw):
            raise DocsApiError(
                f"「{raw[:20]}」看起来是文档名字，但我认不了名字只能认链接——"
                "把文档打开，复制浏览器地址栏里 docs.qq.com 开头的完整链接发我。\n"
                "另外记得把机器人授权的那个腾讯文档账号加为文档协作者（可编辑），"
                "不然我打开不了。"
            )
        raise DocsApiError(
            "链接里找不到文档 ID——请发完整的 docs.qq.com/doc/… 或 /sheet/… 链接"
        )
    kind = parse_kind(url_or_id)
    if kind == "sheet":
        # 表格：URL 里的 ID 要转成 bookID 才能用，顺便验证权限
        book_id = await _to_book_id(fid)
        sheets = await _sheet_list(book_id)
        info = {
            "file_id": fid,
            "url": f"https://docs.qq.com/sheet/{fid}",
            "kind": "sheet",
            "book_id": book_id,
            "sheets": len(sheets),
        }
    else:
        text, _, _ = await read_doc(fid)  # 验证可读
        info = {
            "file_id": fid,
            "url": f"https://docs.qq.com/doc/{fid}",
            "kind": "doc",
            "chars": len(text),
        }
    _bindings_map()[str(gid)] = info
    _save_bindings()
    return info


async def create_doc(gid: int, title: str) -> dict[str, Any]:
    """新建一篇在线文档并绑到群里，写入默认格式头。"""
    headers = await _auth_headers()
    data = await _http_json(
        url=f"{_DRIVE_BASE}/files",
        method="POST",
        headers=headers,
        form={"title": title, "type": "doc"},
    )
    payload = data.get("data") or data
    fid = str(payload.get("ID") or payload.get("file_id") or "")
    if not fid:
        raise DocsApiError(f"创建文档返回异常: {str(data)[:200]}")
    url = str(payload.get("url") or f"https://docs.qq.com/doc/{fid}")
    info = {"file_id": fid, "url": url, "chars": 0}
    _bindings_map()[str(gid)] = info
    _save_bindings()
    try:
        await append_text(fid, _HEADER_BLOCK)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 新文档写格式头失败（文档已创建）: {exc!r}")
    return info


async def append_entry(gid: int, title: str, url: str, by: str = "") -> str:
    """把一条链接按绑定文档/表格的现有格式追加进去。

    返回人话状态：'ok' / 'dup' / '未绑定' / '未配置' / '错误: ...'，调用方只打日志。
    """
    if not cfg.ai_docs_enabled:
        return "未配置"
    if not configured():
        return "未配置"
    info = binding(gid)
    if not info:
        return "未绑定"
    kind = info.get("kind") or parse_kind(info.get("url") or "") or "doc"
    if kind == "sheet":
        try:
            return await _sheet_append_entry(gid, title, url, info)
        except DocsAuthError as exc:
            return f"错误: 凭证失效（{exc}）"
        except Exception as exc:  # noqa: BLE001
            return f"错误: 写表格失败 {exc!r}"
    fid = info["file_id"]
    today = time.strftime("%Y-%m-%d")
    try:
        text, end, version = await read_doc(fid)
    except DocsNotConfigured:
        return "未配置"
    except DocsAuthError as exc:
        return f"错误: 凭证失效（{exc}）"
    except Exception as exc:  # noqa: BLE001
        return f"错误: 读文档失败 {exc!r}"

    if not text.strip():
        entry = _default_entry(title, url, by, today)
        block = "\n" + _HEADER_BLOCK + entry
    else:
        entry, _hit = _format_entry(text, title, url, by, today)
        sep = "" if text.endswith("\n") else "\n"
        block = sep + entry
    try:
        headers = await _auth_headers()
        await _http_json(
            url=f"{_DOC_BASE}/{fid}:batchUpdate",
            method="POST",
            headers=headers,
            payload={
                "requests": [
                    {"insertText": {"text": block, "location": {"index": end}}}
                ],
                "version": version,
            },
        )
    except DocsAuthError as exc:
        return f"错误: 凭证失效（{exc}）"
    except Exception as exc:  # noqa: BLE001
        return f"错误: 写入失败 {exc!r}"
    return "ok"


# ================================================================ 指令


def _binding_text(gid: int) -> str:
    lines = []
    if configured():
        tok = _load_token()
        exp = tok.get("expires_at") or 0
        lines.append("腾讯文档 API：已配置 ✅")
        if exp:
            lines.append(f"token 有效期至：{time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}")
    else:
        lines.append(
            "腾讯文档 API：未配置 ❌（.env 里需要 AI_DOCS_CLIENT_ID / "
            "AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID 三项，"
            "去 docs.qq.com/open 首页「开发者信息」里复制）"
        )
    info = binding(gid)
    if info:
        lines.append(f"本群绑定文档：{info['url']}")
    else:
        lines.append(
            "本群绑定文档：无。管理员可发 /绑定文档 <文档链接> 绑定，"
            "或 /创建文档 标题 让我新建一篇"
        )
    return "\n".join(lines)


bind_doc_cmd = on_command("绑定文档", rule=to_me(), priority=10, block=True)
create_doc_cmd = on_command("创建文档", rule=to_me(), priority=10, block=True)
filter_cmd = on_command("文档过滤", rule=to_me(), priority=10, block=True)
collect_cmd = on_command("收录", rule=to_me(), priority=10, block=True)


@filter_cmd.handle()
async def _handle_filter(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    parts = args.extract_plain_text().split()
    gid = event.group_id
    if not parts:
        await filter_cmd.finish(filter_text(gid))
    if not _is_admin(event):
        await filter_cmd.finish("只有群主或管理员可以改收录过滤规则")
    sub = parts[0]
    try:
        if sub in ("模式", "模式设为", "set") and len(parts) >= 2:
            mode_raw = parts[1]
            mode = {"all": "all", "全部": "all", "全部同步": "all",
                    "白名单": "whitelist", "whitelist": "whitelist",
                    "手动": "manual", "manual": "manual"}.get(mode_raw, mode_raw)
            set_filter(gid, mode=mode)
        elif sub in ("加", "添加", "add") and len(parts) >= 2:
            set_filter(gid, add=parts[1:])
        elif sub in ("删", "删除", "移除", "del", "remove") and len(parts) >= 2:
            set_filter(gid, remove=parts[1:])
        else:
            await filter_cmd.finish(
                "用法：/文档过滤 —— 看当前规则\n"
                "/文档过滤 模式 all|白名单|手动\n"
                "/文档过滤 加 bilibili.com github.com\n"
                "/文档过滤 删 xxx.com"
            )
    except ValueError as exc:
        await filter_cmd.finish(str(exc))
    await filter_cmd.finish("已更新 ✅\n" + filter_text(gid))


@collect_cmd.handle()
async def _handle_collect_cmd(
    bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()
) -> None:
    """手动收录：/收录 关键词（在收藏库里找）或 /收录 完整链接。"""
    if not _is_admin(event):
        await collect_cmd.finish("只有群主或管理员可以手动收录进文档")
    target = args.extract_plain_text().strip()
    from . import linkarchive  # 延迟导入避免包初始化顺序问题

    gid = event.group_id
    entry: dict[str, Any] | None = None
    if target.startswith("http"):
        url = linkarchive.normalize_url(target)
        for e in reversed(linkarchive._load().get(str(gid), [])):
            if e["url"] == url:
                entry = e
                break
        if entry is None:
            # 收藏库里没有（比如刚被挤掉）也照收：现场抓标题
            title = await linkarchive.fetch_title(url) or url
            entry = {"url": url, "title": title, "by": "", "time": ""}
    else:
        hits = linkarchive.search(gid, target, limit=1) if target else linkarchive.recent(gid, limit=1)
        entry = hits[0] if hits else None
    if entry is None:
        await collect_cmd.finish(
            "没找到要收录的链接——发 /收录 关键词（在收藏库找），或 /收录 完整链接"
        )
    by = entry.get("by") or "手动收录"
    status = await append_entry(gid, entry["title"], entry["url"], by)
    if status == "ok" or status.startswith("ok:"):
        line = status[3:] if status.startswith("ok:") else ""
        await collect_cmd.finish(
            f"已收录进在线文档 ✅《{entry['title'][:40]}》" + (f"（写在工作表{line}，在表格最底部）" if line else "")
        )
    if status == "dup":
        await collect_cmd.finish(f"《{entry['title'][:40]}》已经在文档里了，不用重复收录")
    if status == "未绑定":
        await collect_cmd.finish("本群还没绑定在线文档：先 /绑定文档 链接 或 /创建文档 标题")
    if status == "未配置":
        await collect_cmd.finish("腾讯文档 API 还没配置好（.env 需要 AI_DOCS 三项凭证）")
    await collect_cmd.finish(f"收录失败：{status}")


@bind_doc_cmd.handle()
async def _handle_bind(bot, event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    target = args.extract_plain_text().strip()
    if not target:
        # 不带参数 = 查看状态
        await bind_doc_cmd.finish(_binding_text(event.group_id))
    if not _is_admin(event):
        await bind_doc_cmd.finish("只有群主或管理员可以绑定文档")
    try:
        info = await bind_doc(event.group_id, target)
    except DocsNotConfigured:
        await bind_doc_cmd.finish(
            "腾讯文档 API 还没配置，先在 .env 里填 AI_DOCS_CLIENT_ID / "
            "AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID（docs.qq.com/open 首页复制）"
        )
    except Exception as exc:  # noqa: BLE001
        await bind_doc_cmd.finish(f"绑定失败：{exc}")
    await bind_doc_cmd.finish(f"已绑定 ✅ {info['url']}\n以后群里发的链接会自动按文档现有格式收录进去")


@create_doc_cmd.handle()
async def _handle_create(bot, event: GroupMessageEvent, args: Message = CommandArg()) -> None:
    if not _is_admin(event):
        await create_doc_cmd.finish("只有群主或管理员可以创建文档")
    title = args.extract_plain_text().strip() or "群链接收藏"
    try:
        info = await create_doc(event.group_id, title)
    except DocsNotConfigured:
        await create_doc_cmd.finish(
            "腾讯文档 API 还没配置，先在 .env 里填 AI_DOCS_CLIENT_ID / "
            "AI_DOCS_ACCESS_TOKEN / AI_DOCS_OPEN_ID（docs.qq.com/open 首页复制）"
        )
    except Exception as exc:  # noqa: BLE001
        await create_doc_cmd.finish(f"创建失败：{exc}")
    await create_doc_cmd.finish(
        f"已创建并绑定 ✅\n{info['url']}\n记得把我的授权账号加为文档协作者，"
        "之后群里发的链接就会自动收录进去"
    )
