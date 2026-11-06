"""AI 生图表情包：把群聊话题即兴画成吉普莉尔表情包。

链路：群聊话题 → DeepSeek 改写成生图提示词 → GPT 生图（gpt-image-2）
→ PIL 后处理加手写体文字 → base64 直接发出。

为什么文字是后处理的：原版表情包（stickers/jibril/ 那 16 张）都是
「角色画面 + 手写体短语」，而 gpt-image 在图上直接渲染文字基本会画出
乱码。所以生图只管画面（提示词里明确 no text），文字由机器人用
ZCOOL KuaiLe（站酷快乐体，开源）P 上去——永远清晰、风格统一。

两种触发：
1. 明确索要 —— @机器人 + 「画一张/画个XX」，必定画
2. 即兴创作 —— 主动搭话命中时，按 ai_imagegen_probability 概率
   改走生图分支（异步：不占当次回复，图生成好单独发出）

风格基准（对齐 stickers/jibril/ 切片）：赛璐璐上色、粗黑描边、
明亮饱和色块、胸像构图、浅色背景加小闪光装饰。

额度按自然日落盘 data/imagegen_usage.json（防重启清零刷限额）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import shutil
import json
import random
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import DefaultDict, Dict

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment

from .config import Config

cfg = get_plugin_config(Config)

# 项目根目录下的 data/（与 group_digest.json 同目录）
_STATE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "imagegen_usage.json"
)

# 明确索要的生图触发词。注意顺序：长的放前面，
# 否则「画个图」会先被「画个」切走，topic 只剩「图」
IMAGEGEN_TRIGGERS = (
    "画一张", "画一个", "画一幅", "画个图", "画个", "画一下", "画张", "来张图",
    "生成图", "整个图", "画我", "画只",
)

# 话题命中这些词 → 角色模式（画机器人自己：edits + 官方立绘参考图 + 短语文字）。
# 未命中 → 自由模式（用户想画什么画什么：纯文本生图，不加文字）。
# 例：「画一个自画像」命中「自画像」→ 角色模式；
#     「画一个小鸟在天上飞」不命中 → 自由模式。
_CHARACTER_HINTS = (
    "自拍", "自画像", "你自己", "画你", "你本人",
    "吉普莉尔", "杰普莉尔",
    "你的脸", "你的样子", "你长什么样", "你长什么样子",
    "你哭", "你笑", "你生气", "你开心", "你无语", "你困", "你饿",
    "你馋", "你累", "你震惊", "你害羞", "你傲娇", "你卖萌",
    "表情包", "你的表情",
)

# 吉普莉尔的画面特征（拼进每条提示词，保证角色一致性）
_CHARACTER_TRAITS = (
    "Jibril from No Game No Life: young woman with long pink hair, red eyes, "
    "a gem hair ornament, white off-shoulder dress, black long gloves"
)

# 风格基准：对齐 stickers/jibril/ 那 16 张切片的视觉特征
_STYLE_TRAITS = (
    "anime chat sticker style, bold clean black outlines, flat cel shading, "
    "bright saturated colors, character bust close-up, exaggerated expressive face, "
    "simple pastel background with small sparkles, square composition"
)

# 生图内容审核敏感词：改写结果命中任一即换兜底表情（_rewrite_prompt 用）。
# 都是实测/已知会触发 gpt-image 系列 content_policy_violation 的描写。
_BANNED_EXPR_WORDS = (
    "drooling",
    "vomit",
    "puke",
    "blood",
    "bleeding",
    "injur",
    "wound",
    "bruise",
    "tears of pain",
    "crying bitterly",
    "sobbing",
)

# 提示词改写系统提示（DeepSeek 用）。
# 角色形象由参考图锁定，DeepSeek 只负责「表情和动作」+「图上短语」。
# 输出两行：第一行英文表情描述，第二行图上短语（中文 ≤6 字，带标点）。
_REWRITE_SYSTEM = (
    "你是表情包画师。把群聊话题改写成一句「表情和动作」描述。\n"
    "注意：角色形象（发型/服装/光环）由参考图锁定，你绝对不要描述角色外观，"
    "只描述表情和身体动作。\n"
    "要求：\n"
    "1. 根据话题给出夸张生动的表情和动作，情绪要对得上话题\n"
    "2. 英文，20 词以内\n"
    "3. 严格按下面两行格式输出，不要任何多余内容：\n"
    "   第一行：英文表情描述\n"
    "   第二行：文字:短语（中文 2-6 字，带情绪标点，如 太棒了！/ 哼…/ ？）\n"
    "4. 内容安全红线：绝不使用 drooling（流口水）、vomiting（呕吐）、blood（血）、\n"
    "   injury（受伤）、crying tears of pain（痛哭）等词——生图模型的内容审核会\n"
    "   因此拒画。表达「馋/饿」用 hugging own belly with a happy grin、sparkling\n"
    "   eyes reaching for food 这类安全写法\n"
    "示例：\n"
    "laughing with tears streaming down, hands on cheeks, mouth wide open\n"
    "文字:笑死我了！"
)

# 参考图方案的生图提示词骨架：锁定官方立绘的角色设计，只换表情姿势。
# rainbow halo 是天翼种最标志的特征，必须显式钉住（实测不写会丢）。
# Q 版化：立绘是正常比例，prompt 要明确「只取设计、重画 Q 版比例」，
# 否则模型会倾向保持参考图的身体比例（实测过）。
_EDIT_PROMPT_TEMPLATE = (
    "Keep the exact character design from the reference image: "
    "rainbow halo ring fully visible above her head, long pink hair with "
    "gradient tips, red eyes, pink top with yellow gem, purple long gloves. "
    "Keep only her design (face, hair, outfit, halo) and colors from the "
    "reference, but REDRAW her in CHIBI Q-version proportions: oversized "
    "round head, tiny body, big sparkling eyes, short stubby limbs. "
    "Only change the expression and pose: {expr}. "
    "Sticker composition: thick clean white border around the character, "
    "small scattered decorations around her like tiny speech bubbles, "
    "spiral zzz marks and sparkles. Bold clean outlines, bright flat "
    "colors, no text, no watermark, centered composition."
)

# ---------- 额度账本（按天，落盘防重启清零） ----------
_usage: Dict[str, int] = {}
_usage_day: str = date.today().isoformat()


def _roll_day_if_needed() -> None:
    global _usage_day
    today = date.today().isoformat()
    if today != _usage_day:
        _usage.clear()
        _usage_day = today
        _save()


def _load() -> None:
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("day") == _usage_day:
            used = raw.get("used")
            if isinstance(used, int):
                _usage["global"] = max(0, used)
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"day": _usage_day, "used": _usage.get("global", 0)}),
            encoding="utf-8",
        )
        tmp.replace(_STATE_PATH)
    except OSError as exc:
        logger.warning(f"[ai_chat] 生图额度落盘失败: {exc!r}")


_load()


def is_imagegen_request(text: str) -> bool:
    """明确索要判断（给独立 matcher 的 rule 用）。"""
    if not text:
        return False
    return any(word in text for word in IMAGEGEN_TRIGGERS)


def is_implicit_imagegen_request(text: str) -> bool:
    """没说「画」字，但明显在要求看机器人某个样子——也按生图拦下。

    「你卖萌是什么样子」「你生气的样子」「你长什么样」→ 画；
    「你今天很开心」这类陈述/评论 → 不拦，留给正常对话。

    判定两条路：
    1. 强命令：「卖个萌」「撒个娇」等，省略主语也是在对机器人发号施令
    2. 疑问/指点：同时提到机器人自己（你/吉普莉尔）+ 在说「样子/什么样」
    """
    if not text:
        return False
    if any(w in text for w in ("卖个萌", "卖一下萌", "卖萌", "撒个娇", "撒娇")):
        return True
    refers_to_bot = any(w in text for w in ("你", "吉普莉尔", "杰普莉尔", "自己"))
    asks_appearance = "样子" in text or "什么样" in text
    return refers_to_bot and asks_appearance


def is_character_topic(topic: str) -> bool:
    """话题是不是在画机器人自己（自画像/表情包/卖萌类）。

    命中 → 角色模式（edits + 官方立绘参考图 + 短语文字）；
    未命中 → 自由模式（纯文本生图，画用户指定的任意内容，不加文字）。
    """
    if not topic:
        return True  # 没话题就当画自己
    return any(h in topic for h in _CHARACTER_HINTS)


def _has_quota() -> bool:
    _roll_day_if_needed()
    return _usage.get("global", 0) < cfg.ai_imagegen_daily_limit


def _take_quota() -> bool:
    """检查并预扣一次额度（先扣后画，防并发超限）。"""
    if not _has_quota():
        return False
    _usage["global"] = _usage.get("global", 0) + 1
    _save()
    return True


def stats() -> dict:
    _roll_day_if_needed()
    return {
        "used": _usage.get("global", 0),
        "limit": cfg.ai_imagegen_daily_limit,
    }


def _find_font() -> Path | None:
    """找手写字体文件：配置路径优先，其次项目 fonts/ 目录。"""
    candidates = []
    if cfg.ai_imagegen_font:
        candidates.append(Path(cfg.ai_imagegen_font).expanduser())
    project_fonts = Path(__file__).resolve().parents[2] / "fonts"
    if project_fonts.is_dir():
        candidates += sorted(project_fonts.glob("*.ttf")) + sorted(
            project_fonts.glob("*.otf")
        )
    for p in candidates:
        if p.is_file():
            return p
    return None


def _add_text(image_bytes: bytes, phrase: str) -> bytes:
    """把短语 P 到图上（顶部居中，吉普莉尔粉字 + 白描边），模仿原表情包的手写文字。

    任何一步失败（没字体、PIL 报错）都原样返回图——文字是锦上添花，
    不能为它把整张图赔进去。
    """
    if not phrase:
        return image_bytes
    try:
        from PIL import Image, ImageColor, ImageDraw, ImageFont

        font_path = _find_font()
        if font_path is None:
            logger.info("[ai_chat] 未找到手写字体，跳过文字后处理（图照发）")
            return image_bytes

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = image.size
        # 字号按图宽缩放（1024 宽 → 约 72px），长句自动缩
        size = max(36, int(w * 0.075 * min(1.0, 6.0 / max(1, len(phrase)))))
        font = ImageFont.truetype(str(font_path), size)
        draw = ImageDraw.Draw(image)
        bbox = draw.textbbox((0, 0), phrase, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (w - tw) // 2
        y = int(h * 0.05)
        try:
            fill = ImageColor.getrgb(cfg.ai_imagegen_text_color)
        except ValueError:
            fill = (247, 143, 183)  # 颜色写错时退回吉普莉尔粉
        # 白描边（8 向）+ 粉色主字：粉字在浅背景上靠白边撑出对比
        stroke = (255, 255, 255)
        for dx in (-3, 0, 3):
            for dy in (-3, 0, 3):
                if dx or dy:
                    draw.text((x + dx, y + dy), phrase, font=font, fill=stroke)
        draw.text((x, y), phrase, font=font, fill=fill)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 文字后处理失败，发无字版: {exc!r}")
        return image_bytes


async def _rewrite_prompt(topic: str) -> tuple[str, str]:
    """让 DeepSeek 把群聊话题改写成（英文表情描述, 图上短语）。失败用兜底。"""
    fallback_expr = "exaggerated cute reaction, sparkling eyes, cheeks puffed"
    if not topic:
        return fallback_expr, ""
    try:
        from .ai_client import chat_kwargs, make_client

        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        # max_tokens 必须容纳推理预算：曾给 160 被 reasoning 吃到只剩
        # 'drooling' 一个词甚至空串，这里按主链路同级的 500 给。
        resp = await client.chat.completions.create(
            **chat_kwargs(
                [
                    {"role": "system", "content": _REWRITE_SYSTEM},
                    {"role": "user", "content": f"群聊话题：{topic[:200]}"},
                ],
                cfg,
                max_tokens=500,
                temperature=0.7,
            )
        )
        raw = (resp.choices[0].message.content or "").strip()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        prompt = ""
        phrase = ""
        for ln in lines:
            if ln.startswith("文字:"):
                phrase = ln[3:].strip().strip('"').strip()[:8]
            elif not prompt and len(ln) >= 20:
                prompt = ln.strip('"').strip()
        if prompt:
            # 双保险：模型偶尔不听安全约束时，敏感词直接换兜底表情
            # （生图服务端的内容审核会因这些词整单拒画）
            lowered = prompt.lower()
            if any(w in lowered for w in _BANNED_EXPR_WORDS):
                logger.warning(f"[ai_chat] 改写结果含审核敏感词，用兜底表情: {prompt!r}")
                return fallback_expr, phrase
            return prompt, phrase
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 生图提示词改写失败，用兜底表情: {exc!r}")
    return fallback_expr, ""


# 自由模式的改写系统提示（用户点啥画啥，不锁角色）。
_REWRITE_FREE_SYSTEM = (
    "你是插画师。把用户的作画请求改写成一段英文生图提示词。\n"
    "要求：\n"
    "1. 完整描述主体、动作、表情、环境、构图，20-40 个英文词\n"
    "2. 只画用户要求的内容，不要自行添加角色设定\n"
    "3. 内容必须健康安全：不画血腥、暴力、暴露、歧视、违法内容\n"
    "4. 提示词末尾统一加上画风：anime illustration style, vibrant colors, "
    "clean composition, high detail\n"
    "5. 只输出提示词本身，不要任何解释、前缀、引号\n"
    "示例：\n"
    "用户：画一个小鸟在天上飞\n"
    "a small blue bird flying freely in a bright sky with soft white clouds, "
    "wings spread wide, cheerful and lively, anime illustration style, "
    "vibrant colors, clean composition, high detail"
)


async def _rewrite_free_prompt(topic: str) -> str:
    """自由模式：把话题改写成完整英文生图提示词（单行）。失败用兜底。"""
    fallback = (
        "a cute anime illustration, vibrant colors, clean composition, "
        "high detail, cheerful and lively"
    )
    if not topic:
        return fallback
    try:
        from .ai_client import chat_kwargs, make_client

        client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
        resp = await client.chat.completions.create(
            **chat_kwargs(
                [
                    {"role": "system", "content": _REWRITE_FREE_SYSTEM},
                    {"role": "user", "content": topic[:200]},
                ],
                cfg,
                max_tokens=500,
                temperature=0.7,
            )
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 取第一行非空内容当提示词（模型偶尔会加解释，只认第一行）
        for ln in raw.splitlines():
            ln = ln.strip().strip('"').strip()
            if len(ln) >= 10:
                lowered = ln.lower()
                if any(w in lowered for w in _BANNED_EXPR_WORDS):
                    logger.warning(f"[ai_chat] 自由模式改写含敏感词，用兜底: {ln!r}")
                    return fallback
                return ln
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 自由模式提示词改写失败，用兜底: {exc!r}")
    return fallback


def _find_reference() -> Path | None:
    """找角色参考图（官方立绘）。找不到返回 None（降级为纯文本生图）。"""
    candidates = []
    if cfg.ai_imagegen_ref_image:
        candidates.append(Path(cfg.ai_imagegen_ref_image).expanduser())
    project_assets = Path(__file__).resolve().parents[2] / "assets"
    candidates += sorted(project_assets.glob("jibril_reference.*")) if project_assets.is_dir() else []
    for p in candidates:
        if p.is_file():
            return p
    return None


async def _generate(prompt: str, *, sticker: bool = True) -> bytes | None:
    """调 GPT 生图，返回图片 bytes。失败返回 None。

    sticker=True（角色/表情包模式）：有参考图走 /images/edits（multipart
    上传立绘，角色设计由参考图锁定，只换表情姿势——实测比纯文本描述
    像得多，光环发色都不会丢）；没有参考图降级走 /images/generate
    （拼角色特征，效果差一截）。
    sticker=False（自由模式）：走 /images/generate 纯文本，prompt 就是
    用户点名的完整内容，不锁角色。

    带一次重试：中转站的生图通道时不时 503/空 data，等 5 秒重来一次
    常常就过了。
    """
    from .ai_client import make_client

    client = make_client(
        cfg.ai_imagegen_api_key, cfg.ai_imagegen_api_base, cfg.ai_proxy
    )
    reference = _find_reference() if sticker else None
    edit_prompt = _EDIT_PROMPT_TEMPLATE.format(expr=prompt or "cute exaggerated expression")

    for attempt in (1, 2):
        try:
            if sticker and reference is not None:
                with open(reference, "rb") as f:
                    resp = await asyncio.wait_for(
                        client.images.edit(
                            model=cfg.ai_imagegen_model,
                            image=f,
                            prompt=edit_prompt,
                            size=cfg.ai_imagegen_size,
                            n=1,
                        ),
                        timeout=120.0,
                    )
            else:
                if sticker:
                    logger.info("[ai_chat] 无角色参考图，降级为纯文本生图（不像别怪我）")
                    gen_prompt = f"{_CHARACTER_TRAITS}, {_STYLE_TRAITS}, {prompt}, no text"
                else:
                    gen_prompt = prompt
                resp = await asyncio.wait_for(
                    client.images.generate(
                        model=cfg.ai_imagegen_model,
                        prompt=gen_prompt,
                        size=cfg.ai_imagegen_size,
                        quality=cfg.ai_imagegen_quality,
                        n=1,
                    ),
                    timeout=120.0,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 生图失败（第 {attempt} 次）: {exc!r}")
            if attempt == 1:
                await asyncio.sleep(5.0)
            continue

        data = resp.data[0] if resp.data else None
        # 中转站会把内容审核等错误放在 resp.error（HTTP 层还是 200，
        # data 为 null）。只看 data 会静默失败，必须先把 error 亮出来。
        # 注意：空 error 对象（无 code 无 message）是渠道抽风的形态，
        # 不当拒绝处理，交给下面的重试。
        resp_error = getattr(resp, "error", None)
        err_code = getattr(resp_error, "code", None) if resp_error is not None else None
        err_msg = (
            (getattr(resp_error, "message", None) or "").strip()
            if resp_error is not None
            else ""
        )
        if err_code or err_msg:
            logger.warning(
                f"[ai_chat] 生图被服务端拒绝（{err_code or 'unknown'}）: {err_msg[:120]}"
            )
            # 内容审核类错误重试同一 prompt 也没用，直接放弃；
            # 其余（疑似渠道抽风）交给下面的重试。
            if "policy" in str(err_code).lower() or "content" in str(err_code).lower():
                return None
        if data is None:
            logger.warning(f"[ai_chat] 生图返回空 data（第 {attempt} 次），疑似渠道抽风")
            if attempt == 1:
                await asyncio.sleep(5.0)
            continue
        # gpt-image 系列默认返回 b64_json；个别中转返回 url，两种都兼容
        b64 = getattr(data, "b64_json", None)
        if b64:
            return base64.b64decode(b64)
        url = getattr(data, "url", None)
        if url:
            return await _download(str(url))
        return None
    return None


async def _download(url: str) -> bytes | None:
    """下载图片 bytes（httpx2，和 websearch 同一个库）。"""
    try:
        import httpx2

        async with httpx2.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 生图下载失败: {exc!r}")
        return None




# ---------------------------------------------------------------- 生成缓存
#
# 生成过的表情包按主题存进 stickers/generated/，文件名就是主题——
# stickers 模块的语义匹配按文件名找表情，存进去自动进可复用池。
# 目录超上限时挤掉最旧的，防止无限膨胀。

def _cache_dir() -> Path:
    d = Path(__file__).resolve().parents[2] / cfg.ai_sticker_dir / "generated"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(topic: str) -> str:
    """主题 → 安全文件名：去路径符/标点，留中英数字，截 24 字。"""
    keep = re.sub(r"[^\w\u4e00-\u9fff一-龥]+", "_", topic).strip("_")
    return (keep or "表情包")[:24]


def _cache_find(topic: str) -> Path | None:
    """按主题找已生成过的表情：文件名包含主题关键词就算命中。"""
    key = _safe_name(topic)
    if not key:
        return None
    d = _cache_dir()
    for p in sorted(d.glob("*.png")):
        if key[:8] in p.stem:      # 前 8 字吻合即视为同一主题
            return p
    return None


def _cache_save(topic: str, image: bytes) -> None:
    """生成成功后把成图存进缓存目录，超上限挤掉最旧的。"""
    try:
        d = _cache_dir()
        name = _safe_name(topic)
        stamp = time.strftime("%m%d")
        target = d / f"{name}_{stamp}.png"
        n = 1
        while target.exists() and n < 50:   # 同主题同日多张：编号区分
            target = d / f"{name}_{stamp}_{n}.png"
            n += 1
        target.write_bytes(image)
        files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime)
        while len(files) > 60:               # 缓存上限 60 张
            files.pop(0).unlink(missing_ok=True)
            files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 表情包缓存保存失败（不影响发送）: {exc!r}")


async def generate_and_send(
    bot: Bot, event: GroupMessageEvent, topic: str, mode: str = "auto"
) -> bool:
    """全流程：改写提示词 → 生图 →（角色模式加文字）→ 发出。成功返回 True。

    mode 两种：
    - "character" 角色/表情包模式：edits + 官方立绘参考图，只换表情姿势，
      画完 P 上手写体短语。用于自画像、卖萌、即兴表情包等。
    - "free" 自由模式：纯文本生图，用户点啥画啥（小鸟/风景/物品…），
      不加文字。
    - "auto"（默认）按 is_character_topic(topic) 自动分流。

    设计为后台任务（调用方 create_task）：生图要 10-30 秒，
    不能占着对话流程。任何一步失败都静默返回 False——
    顶多少一张图，不能让聊天流程陪葬。
    """
    if not cfg.ai_imagegen_enabled or not cfg.ai_imagegen_api_key:
        if cfg.ai_imagegen_enabled:
            logger.warning("[ai_chat] 生图已启用但没配 Key，跳过（配 AI_IMAGEGEN_API_KEY）")
        return False
    if not _take_quota():
        logger.info("[ai_chat] 生图日限已用完，跳过")
        return False

    from . import ambient  # noqa: PLC0415  函数内导入避免环

    # ---- 缓存命中：同样的主题画过就直接发旧的，省生成费和时间
    if cfg.ai_imagegen_cache:
        hit = _cache_find(topic)
        if hit is not None:
            logger.info(f"[ai_chat] 表情包缓存命中：{hit.name}")
            try:
                await bot.send(
                    event,
                    Message(MessageSegment.image("base64://" + base64.b64encode(hit.read_bytes()).decode())),
                )
                # 记进群聊背景：否则「你刚发的是什么」机器人自己答不上来
                ambient.record(event.group_id, "我", f"发了一张表情包（主题：{topic[:60]}）")
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_chat] 缓存表情发送失败，重新生成: {exc!r}")


    if mode == "auto":
        mode = "character" if is_character_topic(topic) else "free"

    if mode == "character":
        prompt, phrase = await _rewrite_prompt(topic)
        logger.info(f"[ai_chat] [角色模式] 提示词: {prompt[:120]} | 文字: {phrase}")
        image = await _generate(prompt, sticker=True)
        if not image:
            return False
        if phrase:
            image = _add_text(image, phrase)
    else:
        prompt = await _rewrite_free_prompt(topic)
        logger.info(f"[ai_chat] [自由模式] 提示词: {prompt[:120]}")
        image = await _generate(prompt, sticker=False)
        if not image:
            return False

    try:
        await bot.send(
            event,
            Message(MessageSegment.image("base64://" + base64.b64encode(image).decode())),
        )
        logger.info(
            f"[ai_chat] 图片已发出（{mode} 模式，{len(image) // 1024}KB，"
            f"今日第 {_usage['global']} 张）"
        )
        # 同上：把自己发的图记进群聊背景，「你发的这张图」才接得住
        ambient.record(
            event.group_id, "我",
            f"发了一张{'表情包' if mode == 'character' else '图片'}（主题：{topic[:60]}）",
        )
        if cfg.ai_imagegen_cache and mode in ("character", "auto"):
            _cache_save(topic, image)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 图片发送失败: {exc!r}")
        return False


# ---------------------------------------------------------------- 群聊四格漫画
#
# 「把刚才的聊天画成四格漫画」全链路：
# 1. 选段（两段式）：规则滑窗海选候选对话段（便宜、可控、可解释），
#    模型当编辑从候选里挑最好笑的——幽默是语义判断，规则到不了；
#    但让模型扫全量聊天又贵又慢，所以只喂它得分最高的 3 段。
# 2. 分镜：模型把选中的对话改编成 4 格（画面 + 气泡台词 + 说话人）。
# 3. 角色锚定：说话人的 QQ 头像直接当参考图走 edits，画出来像本人；
#    edits 失败退回纯文本生成（头像先让视觉模型写一句外形描述）。

_COMIC_INFLIGHT: set[int] = set()
_last_comic: dict[int, bytes] = {}   # 群号 → 最近画好的漫画原图（发空间用）


def get_last_comic(group_id: int) -> bytes | None:
    return _last_comic.get(group_id)


# ---- 漫画每群每日限额：普通群友每群 3 次/天，管理员指令不受限 ----
# 持久化到 data/comic_usage.json：重启太频繁，内存计数一重启就清零形同虚设
_COMIC_USAGE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "comic_usage.json"
)
_comic_usage: dict[str, int] = {}
_comic_day: str = ""


def _load_comic_usage() -> None:
    global _comic_usage, _comic_day
    today = date.today().isoformat()
    if _comic_day == today and _comic_usage:
        return
    if _comic_day == today:
        return
    try:
        raw = json.loads(_COMIC_USAGE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("day") == today:
            _comic_usage = {
                k: v for k, v in raw.get("groups", {}).items() if isinstance(v, int)
            }
        else:
            _comic_usage = {}
        _comic_day = today
    except (OSError, ValueError):
        _comic_usage = {}
        _comic_day = today


def _save_comic_usage() -> None:
    try:
        _COMIC_USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _COMIC_USAGE_PATH.write_text(
            json.dumps(
                {"day": _comic_day, "groups": _comic_usage}, ensure_ascii=False
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def comic_quota_left(group_id: int) -> int:
    _load_comic_usage()
    return max(0, cfg.ai_comic_daily_limit - _comic_usage.get(str(group_id), 0))


def take_comic_quota(group_id: int) -> bool:
    """占用一次该群今天的漫画额度。管理员路径不应调用此函数。"""
    _load_comic_usage()
    key = str(group_id)
    if _comic_usage.get(key, 0) >= cfg.ai_comic_daily_limit:
        return False
    _comic_usage[key] = _comic_usage.get(key, 0) + 1
    _save_comic_usage()
    return True

_FUN_PAT = re.compile(
    r"笑死|绷不住|离谱|逆天|炸裂|好活|蚌埠|太真实|真实|哈哈哈|6{2,}|？|！"
)


def _line_fun(text: str) -> int:
    """单条消息的趣味信号分（0~5）。规则只负责「值得看一眼」。"""
    score = 0
    if _FUN_PAT.search(text):
        score += 2
    body = text.rstrip()
    if body.endswith(("？", "?")):
        score += 1      # 提问/抬杠是对话推进器
    if body.endswith(("！", "!")):
        score += 1
    if 4 <= len(text) <= 60:
        score += 1      # 长度天然适合做漫画台词
    return score


def _parse_ambient_line(line: str) -> tuple[str, str] | None:
    m = re.match(r"^【(.+?)】(.*)$", line)
    return (m.group(1), m.group(2)) if m else None


def _pick_candidates(lines: list[str], top: int = 3) -> list[list[tuple[str, str]]]:
    """滑动窗口海选候选对话段：≥2 人、总长合适、机器人不占主、趣味分高。"""
    parsed = [p for p in (_parse_ambient_line(x) for x in lines) if p and p[1].strip()]
    cands: list[tuple[float, int, list[tuple[str, str]]]] = []
    for size in range(4, 9):
        for i in range(0, max(0, len(parsed) - size + 1)):
            win = parsed[i:i + size]
            speakers = {s for s, _ in win}
            body = sum(len(t) for _, t in win)
            if len(speakers) < 2 or not 20 <= body <= 400:
                continue
            # 机器人自言自语凑的段没有对手戏
            if sum(1 for s, _ in win if s == "我") > size // 2:
                continue
            fun = sum(_line_fun(t) for _, t in win) / (size ** 0.5)
            if len(speakers) >= 3:
                fun += 1.5   # 三人以上的互动天然有戏
            texts = [t for _, t in win]
            if len(set(texts)) < len(texts):
                fun += 1.0   # 复读/接龙是群名场面
            # 时效加权：位置越靠后（越新）加分越多，但只是加分不是门槛——
            # 老片段足够有趣（分差拉开）依然会赢，符合「新的优先、有趣破例」
            recency = (i + size / 2) / max(1, len(parsed))
            fun += recency * cfg.ai_imagegen_comic_recency
            cands.append((fun, i, win))
    cands.sort(key=lambda e: -e[0])
    picked: list[list[tuple[str, str]]] = []
    used_starts: list[int] = []
    for _score, start, win in cands:
        if any(abs(start - u) < 4 for u in used_starts):
            continue     # 和已选候选重叠太多，不重复喂给模型
        picked.append(win)
        used_starts.append(start)
        if len(picked) >= top:
            break
    return picked


def has_comic_material(group_id: int) -> bool:
    """群里最近的聊天够不够画一段（没有就直接告诉用户先聊会儿）。"""
    from . import ambient  # noqa: PLC0415

    return bool(_pick_candidates(ambient.recent_lines(group_id, limit=40)))


def quota_left() -> int:
    """今天还能画几张（给命令入口预检查，免得先应了再哑火）。"""
    return max(0, cfg.ai_imagegen_daily_limit - _usage.get("global", 0))


async def _llm_panels_json(prompt: str) -> dict | None:
    """分镜 LLM 调用公共体：JSON 截取 + 失败加倍 max_tokens 重试。"""
    from .ai_client import chat_kwargs, make_client  # noqa: PLC0415

    client = make_client(cfg.ai_api_key, cfg.ai_api_base, cfg.ai_proxy)
    last_exc: Exception | None = None
    for cap in (1600, 3200):   # MiMo 开着思考：小预算会被思考吃光导致正文为空，
        try:                   # 失败一次就加倍重试
            resp = await client.chat.completions.create(
                **chat_kwargs(
                    [{"role": "user", "content": prompt}],
                    cfg,
                    max_tokens=cap,
                    temperature=0.7,
                ),
                timeout=60,
            )
            reply = (resp.choices[0].message.content or "").strip()
            # 有的模型会在正文前混思考文字：只取最外层花括号之间的 JSON
            start, end = reply.find("{"), reply.rfind("}")
            if start < 0 or end <= start:
                raise ValueError(f"回复里没有 JSON：{reply[:80]!r}")
            data = json.loads(reply[start:end + 1])
            panels = data.get("panels") if isinstance(data, dict) else None
            if not isinstance(panels, list) or len(panels) < 4:
                raise ValueError(f"panels 结构不对：{str(panels)[:80]!r}")
            data["panels"] = panels[:4]
            return data
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    logger.info(f"[ai_chat] 漫画分镜生成失败（静默）: {last_exc!r}")
    return None


async def _comic_storyboard(cands: list[list[tuple[str, str]]]) -> dict | None:
    """模型当编辑：挑候选 + 写四格分镜。失败返回 None。"""
    blocks = []
    for idx, win in enumerate(cands, 1):
        body = "\n".join(f"【{s}】{t}" for s, t in win)
        blocks.append(f"候选{idx}：\n{body}")
    prompt = (
        "你是四格漫画编辑。下面是几个群聊片段，挑最有喜剧张力的一个，"
        "改编成四格漫画分镜。台词必须贴合原话的意思和语气，"
        "可以调整顺序、轻度润色，但不得无中生有；每格气泡台词不超过 18 字。\n"
        "重要：每格的 who 必须是这句台词原本的说话人，绝不能互换角色——"
        "漫画会把头像画成这个人，换人=张冠李戴。\n"
        '只输出 JSON（不要任何其他文字）：\n'
        '{"pick": 候选编号, "topic": "一句话主题", '
        '"panels": [{"who": "说话人名字(可空)", "scene": "画面动作描述，不要出现文字", '
        '"bubble": "气泡台词，可为空串"}]}，panels 固定 4 个元素。\n'
        "who 只能填片段里出现过的说话人名字（不含括号里的号码）。\n\n"
        + "\n\n".join(blocks)
    )
    return await _llm_panels_json(prompt)


async def _comic_storyboard_from_digest(digest_ctx: str) -> dict | None:
    """ambient 太薄（如刚重启，内存缓冲被清）时退而求其次：拿滚动摘要编小剧场。

    摘要没有带号码的原文，所以这条路画不出头像——角色按普通 Q 版处理。
    """
    prompt = (
        "你是四格漫画编剧。下面是一个 QQ 群今晚的聊天话题摘要，"
        "据此编一段贴合话题的四格漫画小剧场：主题必须来自摘要，"
        "可以轻度演绎但不得涉及敏感内容；每格气泡台词不超过 18 字。\n"
        '只输出 JSON（不要任何其他文字）：\n'
        '{"topic": "一句话主题", '
        '"panels": [{"who": "角色名(可空)", "scene": "画面动作描述，不要出现文字", '
        '"bubble": "气泡台词，可为空串"}]}，panels 固定 4 个元素。\n'
        'who 用摘要里出现过的名字，没有就用「甲」「乙」这样的通称。\n\n'
        f"摘要：{digest_ctx[:300]}"
    )
    return await _llm_panels_json(prompt)


def _img_bytes(obj) -> bytes | None:
    """从生图响应对象取图片字节：中转站有时回 b64_json，有时回 url。"""
    raw = getattr(obj, "b64_json", None)
    if raw:
        return base64.b64decode(raw)
    url = getattr(obj, "url", None)
    if url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read(32 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 生图结果下载失败: {exc!r}")
    return None


def _fetch_avatar(qq: str) -> bytes | None:
    """拉 QQ 头像（q1.qlogo.cn 对任意 nk 都有图，拿不到会返回 html 报错页）。"""
    for host in ("q1.qlogo.cn", "q.qlogo.cn"):
        try:
            req = urllib.request.Request(
                f"https://{host}/g?b=qq&nk={qq}&s=640",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read(4 * 1024 * 1024)
            if data and not data.lstrip()[:1] == b"<":
                return data
        except Exception:  # noqa: BLE001
            continue
    return None


def _comic_prompt(
    board: dict, char_desc: dict[str, str], avatar_names: list[str] | None = None
) -> str:
    """把分镜拼成生图提示词。

    avatar_names 是按上传顺序排列的头像角色名——必须显式告诉模型
    「第几张图是谁」，否则它自己猜谁是谁，画出来人和台词对不上。
    """
    parts = ["四格漫画（2x2 分格，粗描边，可爱 Q 版风格，明亮配色）"]
    if avatar_names:
        binds = "；".join(
            f"第{i}张参考图是「{name}」的头像" for i, name in enumerate(avatar_names, 1)
        )
        parts.append(
            f"漫画共 {len(avatar_names)} 个主要角色。参考图说明：{binds}。"
            "每一张参考图都必须严格遵循——对应名字的角色出场时长成自己参考图的样子"
            "（发型/发色/标志配件保持一致），两张不能长得像、不能混合特征、"
            "不能只画其中一张的样子；"
            "没有参考图的角色必须画出明显不同的外形（不同的发色发型衣服），"
            "绝不能和有参考图的角色混淆，更不能替有参考图的角色出场说话"
        )
    descs = "；".join(f"{name}：{d}" for name, d in char_desc.items() if d)
    if descs:
        parts.append(f"角色外形（四格中务必保持一致）：{descs}")
    parts.append(f"主题：{board.get('topic') or '群聊小剧场'}")
    for i, p in enumerate(board["panels"], 1):
        if not isinstance(p, dict):
            continue
        who = str(p.get("who") or "")
        scene = str(p.get("scene") or "").strip()
        bubble = str(p.get("bubble") or "").strip()
        line = f"第{i}格：{who + '——' if who else ''}{scene}"
        if bubble:
            line += f"；气泡文字：「{bubble}」"
        parts.append(line)
    parts.append(
        "所有气泡文字必须是清晰可读的简体中文、无错别字；每格最多一个气泡，"
        "每格的气泡必须画在 who 指定的那个角色头上；第4格收尾要有笑点定格感。"
    )
    return "\n".join(parts)


_AVATAR_CLASSIFY_PROMPT = (
    "这是一个 QQ 头像。判断它的类型，并给出画 Q 版漫画角色要用的描述。\n"
    "类型四选一：character=动漫/真人人物；animal=动物；"
    "logo=标志/徽章/文字/图标；scene=风景/植物/物品等其他。\n"
    "严格按下面三行格式输出（不要 JSON、不要任何其他文字）：\n"
    "类型：character/animal/logo/scene 之一\n"
    "描述：<一句话。character/animal 描述外形特征（发型发色/眼睛/服饰/物种）；"
    "logo 描述标志的形状图案和文字；scene 简述画面>\n"
    "配色：<2-3个颜色词>"
)


async def _classify_avatar(bot: Bot, qq: str) -> dict:
    """识别头像类型，决定角色锚定方式（参考图 / 印标志 / 取配色）。失败返回 {}。"""
    from . import vision as _vision  # noqa: PLC0415

    url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    try:
        desc = await _vision.describe_images(
            bot, [{"url": url}], mode="url", prompt=_AVATAR_CLASSIFY_PROMPT
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[ai_chat] 头像识别失败 qq={qq}: {exc!r}")
        return {}
    if not desc:
        return {}
    tm = re.search(r"类型\s*[：:]\s*(character|animal|logo|scene)", desc)
    dm = re.search(r"描述\s*[：:]\s*(.+)", desc)
    cm = re.search(r"配色\s*[：:]\s*(.+)", desc)
    if not tm:
        # 没按格式回：整段当普通描述，按配色角色处理
        return {"type": "scene", "desc": desc.strip()[:60], "colors": ""}
    return {
        "type": tm.group(1),
        "desc": dm.group(1).strip()[:80] if dm else "",
        "colors": cm.group(1).strip()[:30] if cm else "",
    }


async def generate_comic_from_chat(bot: Bot, event: GroupMessageEvent) -> bool:
    """全流程：选段 → 分镜 → 头像参考 → 出图 → 发送。失败静默返回 False。"""
    if not cfg.ai_imagegen_enabled or not cfg.ai_imagegen_api_key:
        return False
    gid = event.group_id
    if gid in _COMIC_INFLIGHT:
        return False
    from . import ambient  # noqa: PLC0415

    cands = _pick_candidates(ambient.recent_lines(gid, limit=40))
    digest_ctx = ""
    if not cands:
        # ambient 是内存缓冲，重启就清空——太薄时退用滚动摘要当素材
        from . import digest as digest_mod  # noqa: PLC0415

        digest_ctx = digest_mod.current(gid)
        if not digest_ctx:
            return False
    _COMIC_INFLIGHT.add(gid)
    try:
        board = await (
            _comic_storyboard(cands) if cands else _comic_storyboard_from_digest(digest_ctx)
        )
        if not board:
            return False
        if not _take_quota():
            logger.info("[ai_chat] 漫画生成：日限已用完，放弃")
            return False

        # 说话人 → QQ 号：分镜里 who 用的是名字，回原候选里对回号码。
        # 三层匹配：精确 → 子串（名片「阿萝酱」vs 分镜 who「阿萝」）→
        # who 自带号码（无名片群友的标记就是「QQ12345」）。
        # 摘要兜底路径没有候选窗口，但摘要里保留了「名字(QQ号)」标记，同样能对
        name2qq: dict[str, str] = {}
        if cands:
            for win in cands:
                for speaker, _ in win:
                    m = re.search(r"^(.*?)\((\d{4,12})\)", speaker)
                    if m:
                        name2qq.setdefault(m.group(1).strip(), m.group(2))
                        name2qq.setdefault(speaker, m.group(2))   # 完整标记也能对上
                    elif (m2 := re.fullmatch(r"QQ(\d{4,12})", speaker.strip())):
                        name2qq.setdefault(speaker.strip(), m2.group(1))
        else:
            for m in re.finditer(
                r"([\w一-龥]{1,16})(?:（[^（）]*）)?\((\d{4,12})\)", digest_ctx or ""
            ):
                name2qq.setdefault(m.group(1), m.group(2))

        def _qq_for(who: str) -> str | None:
            who = who.strip()
            if who in name2qq:
                return name2qq[who]
            best = None
            for name, qq in name2qq.items():
                if name and (name in who or who in name):
                    if best is None or len(name) > len(best[0]):
                        best = (name, qq)
            if best:
                return best[1]
            m = re.search(r"(\d{4,12})", who)   # who 里直接带号码
            return m.group(1) if m else None

        chars: list[tuple[str, str]] = []   # [(名字, qq)]
        # 名单兜底：摘要/分镜里的称呼（「小连」）没带号码时，去群成员名单模糊找
        member_index = None
        try:
            from . import members as _members  # noqa: PLC0415

            member_index = await _members.get_index(bot, event.group_id)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[ai_chat] 群成员索引不可用（漫画角色匹配降级）: {exc!r}")
        for p in board["panels"]:
            if not isinstance(p, dict):
                continue
            who = str(p.get("who") or "").strip()
            if not who or any(who == n for n, _ in chars):
                continue
            qq = _qq_for(who)
            if not qq and member_index is not None:
                qq = _members.resolve_by_name(member_index, who)
                if qq:
                    logger.info(f"[ai_chat] 漫画角色「{who}」经成员名单对上 QQ")
            if qq:
                chars.append((who, qq))
            else:
                logger.info(f"[ai_chat] 漫画角色「{who}」没对上 QQ 号，将按普通配角画")
            if len(chars) >= 3:
                break
        logger.info(
            "[ai_chat] 漫画分镜: "
            + " | ".join(
                f"{p.get('who') or '？'}:{str(p.get('bubble') or '')[:16]}"
                for p in board["panels"]
                if isinstance(p, dict)
            )
        )

        # 头像识别分流：
        # 人物/动物头像 → edits 参考图，角色长成头像那样；
        # 标志/徽章 → 简单衣服印标志、整体取标志配色；
        # 风景/植物等其他 → 只取配色生成角色
        infos = await asyncio.gather(*(_classify_avatar(bot, qq) for _, qq in chars))
        avatar_infos = {name: info for (name, _), info in zip(chars, infos) if info}
        char_desc: dict[str, str] = {}
        for name, _ in chars:
            info = avatar_infos.get(name)
            if not info:
                logger.info(f"[ai_chat] 漫画角色「{name}」头像识别失败，按普通配角画")
                continue
            colors = str(info.get("colors") or "").strip()
            if info.get("type") in ("character", "animal"):
                char_desc[name] = f"Q版化的{info.get('desc', '')}"
            elif info.get("type") == "logo":
                base = (
                    f"穿简单T恤/连帽衫、胸口印着「{info.get('desc', '')}」"
                    "标志的Q版人物"
                )
                char_desc[name] = base + (f"，整体配色：{colors}" if colors else "")
            else:
                char_desc[name] = (
                    f"Q版人物，配色取自头像（{colors or info.get('desc', '')}）"
                )
        # 只有人物/动物头像才能当脸，标志和风景图塞给模型只会画歪
        avatars: list[tuple[str, bytes]] = []
        for name, qq in chars:
            if avatar_infos.get(name, {}).get("type") not in ("character", "animal"):
                continue
            raw = await asyncio.to_thread(_fetch_avatar, qq)
            if raw:
                avatars.append((f"{name}.png", raw))
            else:
                logger.info(f"[ai_chat] 漫画角色「{name}」的头像拉取失败")
        logger.info(
            f"[ai_chat] 漫画角色：{[n for n, _ in chars]}，"
            f"类型 {[avatar_infos.get(n, {}).get('type') for n, _ in chars]}，"
            f"头像参考 {len(avatars)} 张"
        )
        image: bytes | None = None
        from .ai_client import make_client  # noqa: PLC0415

        client = make_client(cfg.ai_imagegen_api_key, cfg.ai_imagegen_api_base, cfg.ai_proxy)
        if avatars:
            prompt_edits = _comic_prompt(
                board, char_desc, avatar_names=[n for n, _ in avatars]
            )
            try:
                resp = await client.images.edit(
                    model=cfg.ai_imagegen_model,
                    image=[(fn, io.BytesIO(raw)) for fn, raw in avatars],
                    prompt=prompt_edits,
                    size="1024x1024",
                    quality=cfg.ai_imagegen_comic_quality,
                    n=1,
                )
                image = _img_bytes(resp.data[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[ai_chat] 漫画头像参考 edits 失败，退回纯文本: {exc!r}")
        if image is None:
            try:
                resp = await client.images.generate(
                    model=cfg.ai_imagegen_model,
                    prompt=_comic_prompt(board, char_desc),
                    size="1024x1024",
                    quality=cfg.ai_imagegen_comic_quality,
                    n=1,
                )
                image = _img_bytes(resp.data[0])
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[ai_chat] 漫画生成失败（静默）: {exc!r}")
                return False

        try:
            await bot.send(
                event,
                Message(MessageSegment.image("base64://" + base64.b64encode(image).decode())),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[ai_chat] 漫画发送失败: {exc!r}")
            return False
        ambient.record(
            gid, "我",
            f"画了一张群聊四格漫画（主题：{str(board.get('topic') or '群聊小剧场')[:50]}）",
        )
        _last_comic[gid] = image
        logger.info(f"[ai_chat] 群聊四格漫画已发出（{len(image) // 1024}KB）")
        return True
    finally:
        _COMIC_INFLIGHT.discard(gid)
