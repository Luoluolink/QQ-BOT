"""本地 ComfyUI 生图：把群里的提示词送进本机 ComfyUI 工作流，出图后发回群里。

为什么单独拆一个模块：本文件里除了 ComfyClient 之外全是「与机器人无关的纯逻辑」——
模型清单解析、工作流拼装、命令参数解析、结果整理，都能脱离 NoneBot 单独跑测试。
接入层（plugins/ai_chat/__init__.py）只负责收消息、限流、把图发出去。

用到的 ComfyUI HTTP 接口（稳定，不需要装任何自定义节点）：

    GET  /system_stats    设备与显存信息（判断服务是否在线、显卡叫什么）
    GET  /object_info     所有节点的定义 —— 本机有哪些模型/采样器就藏在这里
    GET  /queue           当前队列（判断是不是在排队）
    POST /prompt          提交一个 API 格式的工作流，返回 prompt_id
    GET  /history/{id}    取该次生成的结果（里面是输出文件名）
    GET  /view            按文件名把图片二进制取回来

「支持的模型」不是猜的，是从 /object_info 里读出来的真实可选值：
ComfyUI 会把它扫到的 ckpt_name / lora_name / sampler_name 等枚举原样报告出来，
所以界面上列的就是本机实际能用的清单，不会出现「列了但跑不起来」的假清单。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

import httpx

DEFAULT_BASE = "http://127.0.0.1:8188"

# 图片后缀白名单：只有这些才允许落盘/发送，避免把任意文件名带进日志或路径
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# 生成结果里最多保留多少张（多了自动删最旧的，防止磁盘被撑爆）
DEFAULT_KEEP = 200


# ---------------------------------------------------------------- 枚举清单
#
# 每一项 = (ComfyUI 节点类名, 输入名, 中文标签)。这些节点都是 ComfyUI 自带的，
# 用它们的枚举输入就能问出「本机有什么」。节点不存在（比如没装 VAE 相关的东西）
# 就让这一栏空着，不会报错。

ENUM_SOURCES: dict[str, tuple[str, str, str]] = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name", "检查点模型"),
    "unets": ("UNETLoader", "unet_name", "扩散模型（UNET）"),
    "loras": ("LoraLoader", "lora_name", "LoRA"),
    "vaes": ("VAELoader", "vae_name", "VAE"),
    "samplers": ("KSampler", "sampler_name", "采样器"),
    "schedulers": ("KSampler", "scheduler", "调度器"),
    "upscalers": ("UpscaleModelLoader", "model_name", "放大模型"),
    "controlnets": ("ControlNetLoader", "control_net_name", "ControlNet"),
    "clips": ("CLIPLoader", "clip_name", "CLIP 文本编码器"),
    "clip_visions": ("CLIPVisionLoader", "clip_name", "CLIP 视觉编码器"),
    "style_models": ("StyleModelLoader", "style_model_name", "风格模型"),
}

# 报告里的展示顺序（先列最常调的，再列锦上添花的）
REPORT_ORDER: tuple[str, ...] = (
    "checkpoints",
    "unets",
    "loras",
    "vaes",
    "samplers",
    "schedulers",
    "upscalers",
    "controlnets",
    "clips",
    "clip_visions",
    "style_models",
)


@dataclass
class ComfyModels:
    """从 /object_info 里挖出来的可用清单。"""

    checkpoints: list[str] = field(default_factory=list)
    unets: list[str] = field(default_factory=list)
    loras: list[str] = field(default_factory=list)
    vaes: list[str] = field(default_factory=list)
    samplers: list[str] = field(default_factory=list)
    schedulers: list[str] = field(default_factory=list)
    upscalers: list[str] = field(default_factory=list)
    controlnets: list[str] = field(default_factory=list)
    clips: list[str] = field(default_factory=list)
    clip_visions: list[str] = field(default_factory=list)
    style_models: list[str] = field(default_factory=list)
    node_count: int = 0

    def get(self, key: str) -> list[str]:
        return list(getattr(self, key, []) or [])

    @property
    def has_anything(self) -> bool:
        return any(self.get(key) for key in REPORT_ORDER)


def _enum_values(object_info: Any, node_class: str, input_name: str) -> list[str]:
    """从 object_info 里取某个节点某个输入的可选值列表。

    结构长这样：
        {"KSampler": {"input": {"required": {"sampler_name": [["euler", ...], {}]}}}}

    这里全程防御式取值：节点没装、结构变了、值是动态占位符（不是列表）都返回空列表，
    绝不让「读不到」升级成「抛异常」。
    """
    if not isinstance(object_info, dict):
        return []
    node = object_info.get(node_class)
    if not isinstance(node, dict):
        return []
    inputs = node.get("input")
    if not isinstance(inputs, dict):
        return []
    spec = inputs.get("required") or inputs.get("optional") or {}
    if not isinstance(spec, dict):
        return []
    raw = spec.get(input_name)
    if not isinstance(raw, (list, tuple)) or not raw:
        return []
    choices = raw[0]
    if not isinstance(choices, (list, tuple)):
        return []
    return [str(item) for item in choices if isinstance(item, str) and item]


def parse_object_info(object_info: Any) -> ComfyModels:
    """把 /object_info 的原始返回整理成 ComfyModels。"""
    models = ComfyModels()
    if isinstance(object_info, dict):
        models.node_count = len(object_info)
    for key, (node_class, input_name, _label) in ENUM_SOURCES.items():
        setattr(models, key, _enum_values(object_info, node_class, input_name))
    return models


# ---------------------------------------------------------------- 名字匹配


def stem_of(name: str) -> str:
    """取文件名主干（去掉目录与后缀），用于展示和模糊匹配。

    ComfyUI 里的模型名可能带子目录（"SDXL/dreamshaper.safetensors"），
    展示时只留主干更清爽，匹配时两种写法都认。
    """
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    return Path(base).stem or base


def match_choice(options: Iterable[str], query: str) -> Optional[str]:
    """在可选值里找最匹配 query 的那一个；找不到或有多解时返回 None。

    匹配顺序：完全相同 → 忽略大小写相同 → 文件名主干相同 → 唯一子串命中。
    「唯一」这条很关键：写 "xl" 而库里同时有 3 个带 xl 的模型时，
    宁可报「有歧义请写全名」，也不要随机挑一个让用户猜。
    """
    q = (query or "").strip()
    if not q:
        return None
    options = [str(o) for o in options if o]
    if not options:
        return None
    if q in options:
        return q
    low = q.lower()
    for option in options:
        if option.lower() == low:
            return option
    for option in options:
        if stem_of(option).lower() == low:
            return option
    hits = [o for o in options if low in o.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


def search_choices(options: Iterable[str], query: str) -> list[str]:
    """列出所有沾边的可选值，用于「找不到时给点提示」。"""
    q = (query or "").strip().lower()
    options = [str(o) for o in options if o]
    if not q:
        return options
    return [o for o in options if q in o.lower()]


# ---------------------------------------------------------------- 生成参数


@dataclass
class GenOptions:
    """一次生成最终要用的具体参数（默认值与命令行覆盖都已经合过）。"""

    prompt: str = ""
    negative: str = ""
    model: str = ""
    sampler: str = "euler"
    scheduler: str = "normal"
    steps: int = 20
    cfg: float = 7.0
    width: int = 512
    height: int = 512
    seed: int = -1
    batch: int = 1
    notes: list[str] = field(default_factory=list)

    def resolved_seed(self) -> int:
        """seed 传 -1 表示随机；这里落成一个确定的数，方便复现。"""
        if self.seed is not None and self.seed >= 0:
            return int(self.seed)
        return int.from_bytes(os.urandom(4), "big") % (2**32)

    def summary(self) -> str:
        return (
            f"{self.width}x{self.height} · {self.steps} 步 · CFG {self.cfg:g} · "
            f"{self.sampler}/{self.scheduler}"
        )


def settings_from_config(cfg: Any) -> dict:
    """把插件配置对象转成普通 dict —— 纯函数不依赖 pydantic 的具体类型。"""

    def pick(name: str, default: Any) -> Any:
        value = getattr(cfg, name, None)
        return default if value is None else value

    return {
        "default_model": str(pick("ai_image_default_model", "") or "").strip(),
        "negative": str(pick("ai_image_default_negative", "") or "").strip(),
        "sampler": str(pick("ai_image_sampler", "euler") or "euler").strip(),
        "scheduler": str(pick("ai_image_scheduler", "normal") or "normal").strip(),
        "steps": int(pick("ai_image_steps", 20)),
        "cfg": float(pick("ai_image_cfg", 7.0)),
        "width": int(pick("ai_image_width", 512)),
        "height": int(pick("ai_image_height", 512)),
        "max_width": int(pick("ai_image_max_width", 1024)),
        "max_height": int(pick("ai_image_max_height", 1024)),
        "batch": int(pick("ai_image_batch", 1)),
        "max_prompt": int(pick("ai_image_max_prompt", 300)),
    }


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def align_size(value: Any, low: int, high: int, fallback: int) -> int:
    """把宽高对齐到 8 的倍数并夹在允许区间内。

    为什么要对齐：SD 的 VAE 下采样 8 倍，非 8 倍数的宽高会被 ComfyUI 静默改写或直接报错。
    夹上限则是因为 8G 显存跑 2048x2048 必然爆显存，与其等它 OOM 不如提前拦住。
    """
    number = _clamp_int(value, low, high, fallback)
    number = (number // 8) * 8
    return max(low, number)


def build_options(
    prompt: str,
    overrides: dict,
    settings: dict,
    models: Optional[ComfyModels] = None,
) -> GenOptions:
    """把「默认设置 + 命令行覆盖 + 本机实际模型清单」合成最终参数。

    会做的检查与兜底：
      - 模型名在本机清单里对不上 → 退回第一个可用模型，并记一条说明（不静默）
      - 本机一个检查点都没有 → 抛 ValueError（上层给用户看得懂的话）
      - 步数/CFG/宽高越界 → 夹到合理区间
    """
    notes: list[str] = []
    overrides = overrides or {}

    prompt = (prompt or "").strip()
    max_prompt = int(settings.get("max_prompt", 300) or 300)
    if len(prompt) > max_prompt:
        prompt = prompt[:max_prompt]
        notes.append(f"提示词太长，已截到 {max_prompt} 字")

    negative = str(overrides.get("negative") or settings.get("negative") or "").strip()

    checkpoints = models.get("checkpoints") if models else []
    wanted_model = str(overrides.get("model") or settings.get("default_model") or "").strip()
    model = ""
    if wanted_model:
        model = match_choice(checkpoints, wanted_model) or ""
        if not model:
            if checkpoints:
                notes.append(f"没有找到模型「{wanted_model}」，已改用 {stem_of(checkpoints[0])}")
            else:
                notes.append(f"没有找到模型「{wanted_model}」")
    if not model:
        if checkpoints:
            model = checkpoints[0]
            if not wanted_model:
                notes.append(f"用的是本机第一个模型：{stem_of(model)}")
        elif models is not None and models.checkpoints == []:
            # models 传了但清单为空 → 一个检查点都没有，没法生成
            if wanted_model:
                raise ValueError(
                    f"ComfyUI 里没有名为「{wanted_model}」的检查点模型，"
                    "而且本机一个检查点都没有，请先把模型放进 ComfyUI 的 models/checkpoints"
                )
            raise ValueError(
                "ComfyUI 里没有任何检查点模型，请先把模型放进 "
                "ComfyUI/models/checkpoints，再发 /画图模型 refresh"
            )
        else:
            model = wanted_model

    sampler = str(overrides.get("sampler") or settings.get("sampler") or "euler").strip()
    if models is not None and models.samplers:
        if sampler not in models.samplers:
            matched = match_choice(models.samplers, sampler)
            if matched:
                sampler = matched
            else:
                notes.append(f"没有采样器「{sampler}」，已改用 {models.samplers[0]}")
                sampler = models.samplers[0]

    scheduler = str(overrides.get("scheduler") or settings.get("scheduler") or "normal").strip()
    if models is not None and models.schedulers:
        if scheduler not in models.schedulers:
            matched = match_choice(models.schedulers, scheduler)
            if matched:
                scheduler = matched
            else:
                notes.append(f"没有调度器「{scheduler}」，已改用 {models.schedulers[0]}")
                scheduler = models.schedulers[0]

    max_width = int(settings.get("max_width", 1024) or 1024)
    max_height = int(settings.get("max_height", 1024) or 1024)
    width = align_size(
        overrides.get("width", settings.get("width", 512)), 64, max_width, 512
    )
    height = align_size(
        overrides.get("height", settings.get("height", 512)), 64, max_height, 512
    )
    if overrides.get("width") or overrides.get("height"):
        requested_w = overrides.get("width")
        requested_h = overrides.get("height")
        if requested_w and _clamp_int(requested_w, 0, 99999, 0) > max_width:
            notes.append(f"宽度上限 {max_width}（可用 .env 的 AI_IMAGE_MAX_WIDTH 调大）")
        if requested_h and _clamp_int(requested_h, 0, 99999, 0) > max_height:
            notes.append(f"高度上限 {max_height}（可用 .env 的 AI_IMAGE_MAX_HEIGHT 调大）")

    seed = -1
    if overrides.get("seed") not in (None, ""):
        seed = _clamp_int(overrides.get("seed"), 0, 2**32 - 1, -1)

    batch = _clamp_int(overrides.get("batch", settings.get("batch", 1)), 1, 4, 1)

    return GenOptions(
        prompt=prompt,
        negative=negative,
        model=model,
        sampler=sampler,
        scheduler=scheduler,
        steps=_clamp_int(overrides.get("steps", settings.get("steps", 20)), 1, 150, 20),
        cfg=_clamp_float(overrides.get("cfg", settings.get("cfg", 7.0)), 0.0, 30.0, 7.0),
        width=width,
        height=height,
        seed=seed,
        batch=batch,
        notes=notes,
    )


# ---------------------------------------------------------------- 命令解析

# 参数开关 → 内部字段名。中英文都收，写错名字的会当普通文字留在提示词里。
FLAGS: dict[str, str] = {
    "-n": "negative",
    "--负": "negative",
    "--负向": "negative",
    "--负面": "negative",
    "--negative": "negative",
    "--尺寸": "size",
    "--size": "size",
    "--宽": "width",
    "--width": "width",
    "--高": "height",
    "--height": "height",
    "--步数": "steps",
    "--steps": "steps",
    "--cfg": "cfg",
    "--模型": "model",
    "--model": "model",
    "--采样器": "sampler",
    "--sampler": "sampler",
    "--调度器": "scheduler",
    "--scheduler": "scheduler",
    "--种子": "seed",
    "--seed": "seed",
    "--数量": "batch",
    "--batch": "batch",
}

SIZE_PATTERN = re.compile(r"^(\d{2,5})\s*[x×*,，]\s*(\d{2,5})$")


def parse_draw_command(text: str) -> tuple[str, dict, list[str]]:
    """拆解「提示词 + --参数」，返回 (提示词, 覆盖项, 提示信息)。

    设计成尽量宽容：不认识的 --xx 不会被吞掉，而是原样留在提示词里
    —— 免得用户写了 --风格 动漫，结果「风格 动漫」凭空消失。
    """
    overrides: dict = {}
    warnings: list[str] = []
    tokens = str(text or "").split()
    prompt_parts: list[str] = []

    index = 0
    while index < len(tokens):
        token = tokens[index]
        field_name = FLAGS.get(token.lower()) or FLAGS.get(token)
        if field_name is None:
            prompt_parts.append(token)
            index += 1
            continue

        if index + 1 >= len(tokens):
            warnings.append(f"{token} 后面少了值，已忽略")
            index += 1
            continue

        value = tokens[index + 1]
        index += 2

        if field_name == "size":
            matched = SIZE_PATTERN.match(value)
            if not matched:
                warnings.append(f"尺寸「{value}」看不懂，请写成 512x768 这样")
                continue
            overrides["width"] = int(matched.group(1))
            overrides["height"] = int(matched.group(2))
        elif field_name in ("width", "height", "steps", "seed", "batch"):
            overrides[field_name] = value
        elif field_name == "cfg":
            overrides["cfg"] = value
        else:  # negative / model / sampler / scheduler 都是原样字符串
            overrides[field_name] = value

    prompt = " ".join(prompt_parts).strip()
    return prompt, overrides, warnings


# 这些说法会被当成「想让它画一张」——只列明确的动词短语，
# 避免把「这张图不错」这种闲聊误判成生图请求。
DRAW_TRIGGERS: tuple[str, ...] = (
    "画一张", "画一幅", "画个", "画只", "画张", "画一个", "画一条", "画朵",
    "帮我画", "给我画", "帮忙画", "请画", "来画", "能不能画", "可以画",
    "生成一张", "生成图片", "生成图像", "生成一张图", "帮我生成图", "帮我生成一张",
    "绘制一张", "绘制一幅", "绘制一个",
    "文生图", "生图", "出图", "ai画", "AI画",
)

# 开头的客气话/动词，拼进提示词会影响出图效果，所以剥掉。
# 这些词经常连着出现（「帮我画一张…」），所以动词部分允许重复 1~3 次。
# 量词写成 `一?[张幅个条朵只匹头]`：这样「一张」「一只」「张」「只」都覆盖，
# 不会因为「一…」和去掉「一」是两种写法而漏掉一个。
_LEADING_NOISE = re.compile(
    r"^\s*(?:(?:请|帮我|给我|帮忙|麻烦|能不能|可以|可不可以|我要|我想|想要|来|"
    r"画|绘制|生成|做|出)\s*){1,3}"
    r"(?:一?[张幅个条朵只匹头])?\s*"
)


def looks_like_draw_query(text: str) -> bool:
    """判断这句话是不是在要求生图（用于被 @ 时的自然语言触发）。"""
    raw = str(text or "").strip()
    if len(raw) < 2:
        return False
    low = raw.lower()
    return any(trigger.lower() in low for trigger in DRAW_TRIGGERS)


def clean_prompt(text: str) -> str:
    """剥掉开头的客气话与动词，留下真正该画的东西。"""
    cleaned = _LEADING_NOISE.sub("", str(text or "").strip(), count=1)
    return cleaned.strip(" ，,。.、:：!！?？~～")


# ---------------------------------------------------------------- 工作流


def build_txt2img_workflow(opts: GenOptions) -> dict:
    """拼一个最标准的「文生图」工作流（ComfyUI 的 API 格式）。

    节点连线与 ComfyUI 默认的文生图模板一致，只用内置节点，所以不挑环境：

        4 CheckpointLoaderSimple ──┬─► 6 CLIPTextEncode(正向) ─┐
                                   ├─► 7 CLIPTextEncode(负向) ─┼─► 3 KSampler ─► 8 VAEDecode ─► 9 SaveImage
                                   └─► 8（vae）               │
        5 EmptyLatentImage ────────────────────────────────────┘

    注意 KSampler 的 seed 必须落成具体数值：留 -1 会让某些版本给出随机结果，
    而我们希望「同一条命令 + 同一个种子」能复现同一张图。
    """
    seed = opts.resolved_seed()
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": int(opts.steps),
                "cfg": float(opts.cfg),
                "sampler_name": opts.sampler,
                "scheduler": opts.scheduler,
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": opts.model},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": int(opts.width),
                "height": int(opts.height),
                "batch_size": int(max(1, opts.batch)),
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": opts.prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": opts.negative, "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "qqbot", "images": ["8", 0]},
        },
    }


def workflow_seed(workflow: dict) -> int:
    """从工作流里读回这一次用的种子（回执里要告诉用户，方便复现）。"""
    try:
        return int(workflow["3"]["inputs"]["seed"])
    except (KeyError, TypeError, ValueError):
        return -1


# ---------------------------------------------------------------- 返回解析


def _node_errors_text(node_errors: Any) -> str:
    """把 node_errors 收拾成一句人话（最多取前三个节点）。"""
    if not isinstance(node_errors, dict) or not node_errors:
        return ""
    chunks: list[str] = []
    for node_id, info in list(node_errors.items())[:3]:
        reason = ""
        if isinstance(info, dict):
            errors = info.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict):
                    reason = str(
                        first.get("message") or first.get("details") or ""
                    ).strip()
                else:
                    reason = str(first).strip()
            if not reason and info.get("message"):
                reason = str(info["message"]).strip()
        elif isinstance(info, str):
            reason = info.strip()
        chunks.append(f"节点 {node_id}：{reason}" if reason else f"节点 {node_id} 校验不通过")
    return "；".join(chunks)


def find_prompt_error(payload: Any) -> str:
    """从 /prompt 的返回体里挖出人能看懂的错误原因。

    ComfyUI 校验失败时返回 400，结构是：
        {"error": {"type": "...", "message": "...", "details": "..."},
         "node_errors": {"4": {"errors": [{"message": "..."}]}}}

    注意只取 error.message，不拿 error.type 顶替 —— type 是
    prompt_outputs_failed_validation 这种机器码，对用户毫无帮助；
    真正有用的细节往往在 node_errors 里（「哪个节点的哪个值不在范围内」）。
    """
    if not isinstance(payload, dict):
        return ""

    error = payload.get("error")
    message = ""
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        details = str(error.get("details") or "").strip()
        if details:
            message = f"{message}（{details}）" if message else details
    elif isinstance(error, str):
        message = error.strip()

    node_text = _node_errors_text(payload.get("node_errors"))
    if message and node_text:
        return f"{message}；{node_text}"
    return message or node_text or ""


def extract_images(history_entry: Any) -> list[dict]:
    """从 /history 的某次记录里取出所有生成的图片引用。

    一并收 SaveImage 之外节点产出的图（比如 PreviewImage），去重后返回。
    """
    if not isinstance(history_entry, dict):
        return []
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        return []
    images: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        raw_list = node_output.get("images")
        if not isinstance(raw_list, list):
            continue
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "").strip()
            if not filename:
                continue
            subfolder = str(item.get("subfolder") or "").strip()
            kind = str(item.get("type") or "output").strip() or "output"
            key = (filename, subfolder, kind)
            if key in seen:
                continue
            seen.add(key)
            images.append(
                {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": kind,
                    "node": str(node_id),
                }
            )
    return images


def history_error(history_entry: Any) -> str:
    """判断这次生成是不是失败了，返回错误描述（正常则返回空串）。"""
    if not isinstance(history_entry, dict):
        return ""
    status = history_entry.get("status")
    if not isinstance(status, dict):
        return ""
    if str(status.get("status_str") or "").lower() != "error":
        return ""
    messages = status.get("messages")
    if isinstance(messages, list):
        for item in messages:
            text = str(item)
            if "error" in text.lower() or "exception" in text.lower():
                return text[:300]
        if messages:
            return str(messages[-1])[:300]
    return "生成过程报错（ComfyUI 没有给出细节，去它的控制台看日志）"


# ---------------------------------------------------------------- 结果落盘


def safe_stem(name: str, fallback: str = "image") -> str:
    """把任意文件名收敛成安全的短主干，专治路径穿越与奇怪字符。"""
    base = Path(str(name).replace("\\", "/").split("/")[-1]).stem
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", base).strip("-")
    return (cleaned[:40] or fallback)


def save_image(
    directory: Path, data: bytes, ref: dict, now: Optional[float] = None
) -> Path:
    """把图片二进制落到本地目录，返回落盘路径。

    用「主干-时间戳」命名而不是直接用 ComfyUI 给的名字，
    这样并发生成、同名覆盖都不会互相踩。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(str(ref.get("filename") or "")).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    base = f"{safe_stem(ref.get('filename') or '')}-{stamp}-{os.getpid()}"
    path = directory / f"{base}{suffix}"
    counter = 1
    while path.exists():
        path = directory / f"{base}-{counter}{suffix}"
        counter += 1
    path.write_bytes(data)
    return path


def list_saved_images(directory: Path) -> list[Path]:
    """列出已保存的生成结果，最新的排前面。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def prune_images(directory: Path, keep: int) -> int:
    """只保留最新 keep 张，返回删掉的数量。keep <= 0 表示不清理。"""
    if keep <= 0:
        return 0
    files = list_saved_images(directory)
    removed = 0
    for path in files[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------- 报告文案


def _bullet_lines(items: Iterable[str], limit: int) -> list[str]:
    """把清单排成带序号的短行；太长就截断并说明总数。"""
    values = list(items)
    lines = []
    for index, value in enumerate(values[:limit], 1):
        lines.append(f"　{index}. {value}")
    if len(values) > limit:
        lines.append(f"　…还有 {len(values) - limit} 个（共 {len(values)} 个）")
    return lines


def _inline_line(items: Iterable[str], limit: int) -> str:
    values = list(items)
    head = "、".join(values[:limit])
    if len(values) > limit:
        head += f" …（共 {len(values)} 个）"
    return head


def format_models_report(
    models: ComfyModels,
    settings: dict,
    *,
    base_url: str = "",
    online: bool = True,
    detail_limit: int = 15,
) -> str:
    """「当前支持哪些模型」的清单 —— /画图模型 命令的输出。"""
    if not online:
        return (
            f"连不上本地 ComfyUI（{base_url or DEFAULT_BASE}）。\n"
            "请先启动 ComfyUI（它默认监听 8188 端口），然后重发 /画图模型。\n"
            "如果 ComfyUI 装在另一台机器或改了端口，改 .env 里的 AI_IMAGE_API_BASE。"
        )

    lines = [f"本地生图清单（ComfyUI @ {base_url or DEFAULT_BASE}）"]
    lines.append(f"　共读到 {models.node_count} 个节点定义")

    if not models.has_anything:
        lines.append("　没能读到任何模型清单，请确认 ComfyUI 已完全启动")
        return "\n".join(lines)

    for key in REPORT_ORDER:
        items = models.get(key)
        if not items:
            continue
        label = ENUM_SOURCES[key][2]
        count = len(items)
        # 检查点/LoRA 会直接决定出图效果，逐条列出来；采样器这类只列名字就够
        if key in ("checkpoints", "unets", "loras", "vaes", "upscalers", "controlnets"):
            lines.append(f"\n{label}（{count} 个）：")
            lines.extend(_bullet_lines(items, detail_limit))
        else:
            lines.append(f"\n{label}（{count} 个）：")
            lines.append(f"　{_inline_line(items, 12)}")

    default_model = settings.get("default_model") or ""
    if default_model:
        picked = match_choice(models.checkpoints, default_model) or ""
        if picked:
            lines.append(f"\n当前默认模型：{picked}")
        else:
            lines.append(
                f"\n当前默认模型：{default_model}（不在本机清单里，生成时会自动改用 "
                f"{stem_of(models.checkpoints[0]) if models.checkpoints else '无'}）"
            )
    elif models.checkpoints:
        lines.append(f"\n当前默认模型：{models.checkpoints[0]}（本机第一个）")

    lines.append(
        "默认参数："
        f"{settings.get('width')}x{settings.get('height')} · {settings.get('steps')} 步 · "
        f"CFG {float(settings.get('cfg', 7.0)):g} · "
        f"{settings.get('sampler')} / {settings.get('scheduler')}"
    )
    lines.append(
        "\n用法：/画 一只在雨里的猫　（可加 --模型 xxx --尺寸 768x768 --步数 30 -n 负向词）"
    )
    lines.append("刷新本清单：/画图模型 refresh（管理员）")
    return "\n".join(lines)


def format_status_report(
    *,
    base_url: str,
    models: ComfyModels,
    stats: dict,
    queue: dict,
    settings: dict,
    used: int = 0,
    limit: int = 0,
    image_count: int = 0,
    image_dir: str = "",
) -> str:
    """生图功能的整体状态 —— /comfy 命令的输出。"""
    lines = ["本地生图状态（ComfyUI）", f"　接口：{base_url}"]

    devices = []
    if isinstance(stats, dict):
        for device in stats.get("devices") or []:
            if isinstance(device, dict):
                name = str(device.get("name") or "未知设备")
                devices.append(name)
    if devices:
        lines.append("　服务：在线")
        lines.append("　设备：" + "、".join(devices))
    else:
        lines.append("　服务：在线（没读到设备信息）")

    pending = 0
    running = 0
    if isinstance(queue, dict):
        pending = len(queue.get("queue_pending") or [])
        running = len(queue.get("queue_running") or [])
    lines.append(f"　队列：运行中 {running} 个，排队 {pending} 个")

    if models.checkpoints:
        lines.append(f"　检查点模型：{len(models.checkpoints)} 个")
    else:
        lines.append("　检查点模型：0 个（生成前请先放模型进 models/checkpoints）")
    lines.append(f"　LoRA：{len(models.loras)} 个 · 采样器：{len(models.samplers)} 个")
    lines.append("　默认参数：" + f"{settings.get('width')}x{settings.get('height')} · "
                 f"{settings.get('steps')} 步 · CFG {float(settings.get('cfg', 7.0)):g} · "
                 f"{settings.get('sampler')} / {settings.get('scheduler')}")
    lines.append(f"　图片目录：{image_dir}（现有 {image_count} 张）")
    if limit:
        lines.append(f"　本群今日已生成 {used} / {limit} 张")
    lines.append("\n看支持的模型：/画图模型　　生成：/画 你的提示词")
    return "\n".join(lines)


def describe_options(opts: GenOptions, seed: int) -> str:
    """生成前回执：告诉群里这次到底用什么参数在画。"""
    text = (
        f"正在生成：{opts.prompt or '（没写提示词，按默认来）'}\n"
        f"{opts.summary()} · 种子 {seed} · 模型 {stem_of(opts.model) or '未指定'}"
    )
    if opts.negative:
        text += f"\n负向：{opts.negative}"
    if opts.notes:
        text += "\n" + "\n".join(opts.notes)
    return text


# ---------------------------------------------------------------- 客户端


class ComfyError(RuntimeError):
    """生图链路上可以预期的错误：连不上、超时、工作流被拒、取图失败。

    单独一个类型是为了让接入层能干净地区分「ComfyUI 的问题」（回一句人话）
    与「代码的 bug」（抛上去打日志）。
    """


def normalize_base(raw: str) -> str:
    """补全协议、去掉结尾斜杠；空值回落到默认的本地地址。"""
    base = str(raw or "").strip().rstrip("/")
    if not base:
        return DEFAULT_BASE
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    return base


class ComfyClient:
    """对 ComfyUI HTTP 接口的一层薄封装。

    只做「发请求 + 把错误翻译成人话」，不做业务判断（那是纯函数的活）。
    超时给了两层：单次请求用 timeout，整轮生成用 generate(deadline)。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        *,
        timeout: float = 30.0,
        client_id: str = "",
    ) -> None:
        self.base = normalize_base(base_url)
        self.timeout = float(timeout)
        self.client_id = client_id or f"qqbot-{uuid4().hex[:12]}"

    # ---- 底层请求 ----

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = self.base + path
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                response = await client.request(
                    method, url, params=params, json=payload
                )
        except httpx.HTTPError as exc:
            raise ComfyError(
                f"连不上 ComfyUI（{self.base}）：{type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            detail = ""
            try:
                detail = find_prompt_error(response.json())
            except ValueError:
                detail = response.text[:200]
            raise ComfyError(
                detail or f"ComfyUI 返回 HTTP {response.status_code}"
            )

        try:
            return response.json()
        except ValueError:
            return response.content

    # ---- 探测 ----

    async def object_info(self) -> dict:
        data = await self._request("GET", "/object_info")
        if not isinstance(data, dict):
            raise ComfyError("ComfyUI 的 /object_info 返回了非预期的格式")
        return data

    async def system_stats(self) -> dict:
        data = await self._request("GET", "/system_stats")
        return data if isinstance(data, dict) else {}

    async def queue_info(self) -> dict:
        data = await self._request("GET", "/queue")
        return data if isinstance(data, dict) else {}

    async def online(self) -> bool:
        """探活：能拿到 system_stats 就算在线。"""
        try:
            await self.system_stats()
            return True
        except ComfyError:
            return False

    # ---- 提交与取图 ----

    async def submit(self, workflow: dict) -> str:
        data = await self._request(
            "POST",
            "/prompt",
            payload={"prompt": workflow, "client_id": self.client_id},
        )
        if not isinstance(data, dict):
            raise ComfyError("ComfyUI 的 /prompt 返回了非预期的格式")
        error = find_prompt_error(data)
        if error:
            raise ComfyError(error)
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyError("ComfyUI 没有返回 prompt_id，可能工作流被拒了")
        return str(prompt_id)

    async def history(self, prompt_id: str) -> dict:
        data = await self._request("GET", f"/history/{prompt_id}")
        return data if isinstance(data, dict) else {}

    async def fetch_image(self, ref: dict, *, timeout: Optional[float] = None) -> bytes:
        params = {
            "filename": str(ref.get("filename") or ""),
            "subfolder": str(ref.get("subfolder") or ""),
            "type": str(ref.get("type") or "output"),
        }
        data = await self._request(
            "GET",
            "/view",
            params=params,
            timeout=timeout or max(self.timeout, 60.0),
        )
        if isinstance(data, bytes):
            if not data:
                raise ComfyError("ComfyUI 返回了空图片")
            return data
        raise ComfyError("ComfyUI 的 /view 没有返回图片数据")

    async def wait_result(
        self, prompt_id: str, *, deadline: float, poll: float = 0.8
    ) -> dict:
        """轮询 /history 直到这次生成出图（或超时/报错）。

        为什么不走 WebSocket：WS 只在「实时进度条」上有优势，代价是要多一个
        长连接和断线重连逻辑；群里生图只需要「成了没有」这一个结论，轮询更稳、
        更好测。轮询间隔默认 0.8 秒，一个 20 步的 512 图通常 3~10 秒出结果。
        """
        poll = max(0.2, float(poll))
        while True:
            entry = await self.history(prompt_id)
            item = entry.get(prompt_id)
            if isinstance(item, dict):
                failed = history_error(item)
                if failed:
                    raise ComfyError(f"生成失败：{failed}")
                if extract_images(item):
                    return item
                if item.get("outputs"):
                    # 跑完了但没有任何图片输出 —— 多半是工作流里没有 SaveImage
                    raise ComfyError(
                        "生成跑完了但没有图片输出，请检查工作流里是否有 SaveImage 节点"
                    )
            if time.monotonic() >= deadline:
                raise ComfyError(
                    "生成超时（还没画完）。可以调大 .env 里的 AI_IMAGE_TIMEOUT，"
                    "或把步数/尺寸调小一点"
                )
            await _sleep(poll)

    async def generate(
        self, workflow: dict, *, timeout: float = 180.0, poll: float = 0.8
    ) -> dict:
        """提交 + 等结果，返回 /history 里那次生成的记录。"""
        prompt_id = await self.submit(workflow)
        deadline = time.monotonic() + max(5.0, float(timeout))
        return await self.wait_result(prompt_id, deadline=deadline, poll=poll)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def load_models(client: ComfyClient) -> tuple[ComfyModels, str]:
    """拉一次模型清单，返回 (清单, 错误说明)。错误不抛出，交给调用方决定怎么展示。"""
    try:
        info = await client.object_info()
    except ComfyError as exc:
        return ComfyModels(), str(exc)
    return parse_object_info(info), ""
