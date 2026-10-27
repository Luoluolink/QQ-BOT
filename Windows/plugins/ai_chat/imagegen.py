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
import json
import random
import time
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
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[ai_chat] 图片发送失败: {exc!r}")
        return False
