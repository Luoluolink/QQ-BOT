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
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.rule import to_me

from .config import Config

cfg = get_plugin_config(Config)

_TOKEN_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "tdoc_token.json"
_BINDING_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "doc_binding.json"

_OAUTH_TOKEN_URL = "https://docs.qq.com/oauth/v2/token"
_DRIVE_BASE = "https://docs.qq.com/openapi/drive/v2"
_DOC_BASE = "https://docs.qq.com/openapi/doc/v3"

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
    """绑定已有文档。先读一次验证权限，读不了就报错（提示加协作者）。"""
    fid = parse_file_id(url_or_id) or url_or_id.strip()
    if not fid:
        raise DocsApiError("链接里找不到文档 ID，请发完整的 docs.qq.com/doc/xxx 链接")
    text, _, _ = await read_doc(fid)  # 验证可读
    info = {"file_id": fid, "url": f"https://docs.qq.com/doc/{fid}", "chars": len(text)}
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
    """把一条链接按文档现有格式追加到文末。

    返回人话状态：'ok' / '未绑定' / '未配置' / '错误: ...'，调用方只打日志。
    """
    if not cfg.ai_docs_enabled:
        return "未配置"
    if not configured():
        return "未配置"
    info = binding(gid)
    if not info:
        return "未绑定"
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
