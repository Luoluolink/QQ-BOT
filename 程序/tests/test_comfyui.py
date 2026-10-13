"""生图功能测试：纯逻辑 + 对着假 ComfyUI 服务器跑完整 HTTP 链路。

分三段：

  1~9 节   纯逻辑。comfyui.py 是按「可以不依赖 nonebot 单独加载」设计的，
           这里用 importlib 直接把它当独立模块加载，连插件框架都不需要拉起来。
  10 节    起一个假 ComfyUI（stdlib http.server），真实走一遍
           探测 → 拉清单 → 提交 → 轮询 → 取图，并覆盖各条异常分支。
  11 节    静态一致性检查：配置项有没有漏进启动器的 .env 模板、命令有没有注册。
           这里用 AST 读源码而不是 import 插件 —— 导入 plugins.ai_chat 需要
           拉起整个 NoneBot 运行时（driver / 适配器 / .env），在测试里又慢又脆，
           而这一段要验的只是「有没有这个命令、配置齐不齐」，解析源码足够且更稳。

想拿本机真实的 ComfyUI 跑一遍完整生成（会真的画图、占显存），加 --live：

    python tests/test_comfyui.py --live

默认不跑，避免测试依赖外部服务。假服务器不联网、不花钱、不碰你本机的 ComfyUI。

运行： python tests/test_comfyui.py
"""

import asyncio
import base64
import importlib.util
import json
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # Windows 控制台默认可能是 GBK，中文输出容易炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

PROJECT = Path(
    sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else Path(__file__).resolve().parent.parent
)
PORT = 18088
LIVE = "--live" in sys.argv

# 1x1 的合法 PNG，冒充「ComfyUI 返回的图片」
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def load_comfyui():
    """把 plugins/ai_chat/comfyui.py 当独立模块加载。

    必须先塞进 sys.modules 再 exec_module：dataclass 装饰器内部会用
    cls.__module__ 反查 sys.modules，模块不在里面就直接 AttributeError。
    """
    path = PROJECT / "plugins" / "ai_chat" / "comfyui.py"
    spec = importlib.util.spec_from_file_location("comfyui", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["comfyui"] = module
    spec.loader.exec_module(module)
    return module


comfyui = load_comfyui()


# ---------------------------------------------------------------- 断言小工具

_PASS = 0
_FAIL = 0


def check(label, condition, extra=""):
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"    [PASS] {label}")
    else:
        _FAIL += 1
        print(f"    [FAIL] {label}" + (f"  <-- {extra}" if extra else ""))


def eq(label, actual, expected):
    check(label, actual == expected, f"期待 {expected!r}，实际 {actual!r}")


def section(name):
    print(f"\n[{name}]")


# ---------------------------------------------------------------- 1. 清单解析

def section_parse():
    section("1. /object_info 解析")

    sample = {
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["a.safetensors", "sub/b.safetensors"], {}]}}
        },
        "LoraLoader": {
            "input": {
                "required": {
                    "lora_name": [["x.safetensors"], {}],
                    "strength_model": [1.0, {}],
                }
            }
        },
        "KSampler": {
            "input": {
                "required": {
                    "sampler_name": [["euler", "dpmpp_2m"], {}],
                    "scheduler": [["normal", "karras"], {}],
                }
            }
        },
        "VAELoader": {"input": {"optional": {"vae_name": [["v.safetensors"], {}]}}},
        "UNETLoader": {"input": {"required": {"unet_name": [[], {}]}}},
        # 结构异常：choices 不是列表
        "WeirdNode": {"input": {"required": {"ckpt_name": "not-a-list"}}},
    }
    models = comfyui.parse_object_info(sample)
    eq("节点总数", models.node_count, len(sample))
    eq("检查点", models.checkpoints, ["a.safetensors", "sub/b.safetensors"])
    eq("LoRA", models.loras, ["x.safetensors"])
    eq("采样器", models.samplers, ["euler", "dpmpp_2m"])
    eq("调度器", models.schedulers, ["normal", "karras"])
    eq("VAE 也能从 optional 里取到", models.vaes, ["v.safetensors"])
    eq("空清单的 UNET", models.unets, [])
    eq("没装的节点返回空", models.upscalers, [])
    check("has_anything", models.has_anything is True)

    # 各种畸形输入都不该抛异常
    for bad in (None, 123, "x", [], {"KSampler": None}, {"KSampler": {"input": None}},
                {"KSampler": {"input": {"required": {"sampler_name": []}}}},
                {"KSampler": {"input": {"required": {"sampler_name": [[1, 2, ""], {}]}}}}):
        try:
            comfyui.parse_object_info(bad)
        except Exception as exc:  # noqa: BLE001
            check(f"畸形输入不抛异常: {str(bad)[:30]}", False, repr(exc))
            break
    else:
        check("畸形输入全部安全返回", True)

    check("ENUM_SOURCES 覆盖 ComfyModels 的全部字段",
          set(comfyui.ENUM_SOURCES) <= {f for f in comfyui.ComfyModels.__dataclass_fields__})
    check("REPORT_ORDER 与 ENUM_SOURCES 键一致",
          set(comfyui.REPORT_ORDER) == set(comfyui.ENUM_SOURCES))


# ---------------------------------------------------------------- 2. 名字匹配

def section_match():
    section("2. 模型名匹配")

    eq("stem_of 去目录", comfyui.stem_of("SDXL/dream.safetensors"), "dream")
    eq("stem_of 反斜杠", comfyui.stem_of("SDXL\\dream.safetensors"), "dream")
    eq("stem_of 无后缀", comfyui.stem_of("dream"), "dream")

    options = ["DreamShaper_8.safetensors", "SDXL/sd_xl_base_1.0.safetensors", "wai_v15.safetensors"]
    eq("完全一致", comfyui.match_choice(options, "wai_v15.safetensors"), "wai_v15.safetensors")
    eq("忽略大小写", comfyui.match_choice(options, "dreamshaper_8.SAFETENSORS"), "DreamShaper_8.safetensors")
    eq("主干匹配（不带后缀）", comfyui.match_choice(options, "wai_v15"), "wai_v15.safetensors")
    eq("唯一子串", comfyui.match_choice(options, "sd_xl"), "SDXL/sd_xl_base_1.0.safetensors")
    eq("空查询返回 None", comfyui.match_choice(options, ""), None)
    eq("列表为空返回 None", comfyui.match_choice([], "a"), None)

    many = ["a_xl.safetensors", "b_xl.safetensors"]
    eq("多解返回 None（不瞎猜）", comfyui.match_choice(many, "xl"), None)
    eq("找不到返回 None", comfyui.match_choice(options, "zzz"), None)

    eq("search_choices 模糊列全部", comfyui.search_choices(many, "xl"), many)
    eq("search_choices 空查询返回全部", len(comfyui.search_choices(many, "")), 2)


# ---------------------------------------------------------------- 3. 参数合成

def _settings(**kw):
    base = {
        "default_model": "", "negative": "", "sampler": "euler", "scheduler": "normal",
        "steps": 20, "cfg": 7.0, "width": 512, "height": 512,
        "max_width": 1024, "max_height": 1024, "batch": 1, "max_prompt": 300,
    }
    base.update(kw)
    return base


def _models(**kw):
    models = comfyui.ComfyModels()
    for key, value in kw.items():
        setattr(models, key, value)
    return models


def section_options():
    section("3. 参数合成 build_options")

    lib = _models(checkpoints=["A.safetensors", "B.safetensors"],
                  samplers=["euler", "dpmpp_2m"], schedulers=["normal", "karras"])

    opts = comfyui.build_options("一只猫", {}, _settings(default_model="A"), lib)
    eq("提示词", opts.prompt, "一只猫")
    eq("默认模型", opts.model, "A.safetensors")
    eq("尺寸", (opts.width, opts.height), (512, 512))
    check("默认模型命中时不产生 notes", opts.notes == [], opts.notes)

    # 模糊名
    opts = comfyui.build_options("猫", {}, _settings(default_model="b"), lib)
    eq("模糊模型名匹配到 B", opts.model, "B.safetensors")

    # 匹配不到 → 退回第一个 + 记一条说明
    opts = comfyui.build_options("猫", {}, _settings(default_model="zzz"), lib)
    eq("未知模型退回第一个", opts.model, "A.safetensors")
    check("未知模型有说明", any("没有找到模型" in n for n in opts.notes), opts.notes)

    # 一个检查点都没有 → 抛 ValueError
    empty = _models(checkpoints=[], samplers=[], schedulers=[])
    try:
        comfyui.build_options("猫", {}, _settings(), empty)
        check("无检查点时抛 ValueError", False, "没有抛异常")
    except ValueError as exc:
        check("无检查点时抛 ValueError", "检查点" in str(exc), str(exc))

    # 不带清单（None）→ 用户说啥用啥，不报错
    opts = comfyui.build_options("猫", {"model": "whatever"}, _settings(), None)
    eq("无清单时照用户说的用", opts.model, "whatever")

    # 未知采样器/调度器 → 换成清单里的第一个 + 说明
    opts = comfyui.build_options("猫", {"sampler": "nope", "scheduler": "nope"},
                                 _settings(default_model="A"), lib)
    eq("未知采样器被替换", opts.sampler, "euler")
    eq("未知调度器被替换", opts.scheduler, "normal")
    check("替换有说明", len(opts.notes) >= 2, opts.notes)

    # 越界夹取
    opts = comfyui.build_options("猫", {"steps": "999", "cfg": "99", "width": "99999"},
                                 _settings(default_model="A"), lib)
    eq("步数夹到 150", opts.steps, 150)
    eq("CFG 夹到 30", opts.cfg, 30.0)
    eq("宽度夹到上限并 8 对齐", opts.width, 1024)
    check("超上限有说明", any("上限" in n for n in opts.notes), opts.notes)

    opts = comfyui.build_options("猫", {"width": "100", "height": "101"},
                                 _settings(default_model="A"), lib)
    eq("宽度 100 → 96（8 的倍数）", opts.width, 96)
    eq("高度 101 → 96（8 的倍数）", opts.height, 96)

    # 超长提示词截断
    opts = comfyui.build_options("猫" * 400, {}, _settings(default_model="A", max_prompt=50), lib)
    eq("提示词截到 50 字", len(opts.prompt), 50)
    check("截断有说明", any("太长" in n for n in opts.notes), opts.notes)

    # 种子：-1 / 缺省 → 随机；指定 → 原样
    opts = comfyui.build_options("猫", {"seed": "12345"}, _settings(default_model="A"), lib)
    eq("指定种子", opts.resolved_seed(), 12345)
    opts = comfyui.build_options("猫", {}, _settings(default_model="A"), lib)
    check("缺省种子落在 uint32 范围", 0 <= opts.resolved_seed() < 2**32)

    # 批次数夹取
    opts = comfyui.build_options("猫", {"batch": "99"}, _settings(default_model="A"), lib)
    eq("批次夹到 4", opts.batch, 4)

    eq("summary 文案", comfyui.build_options(
        "猫", {"steps": "30", "cfg": "7.5", "width": "768", "height": "768"},
        _settings(default_model="A"), lib).summary(),
        "768x768 · 30 步 · CFG 7.5 · euler/normal")


# ---------------------------------------------------------------- 4. 命令解析

def section_command():
    section("4. /画 命令参数解析")

    prompt, over, warn = comfyui.parse_draw_command("一只橘猫坐在窗台上")
    eq("纯提示词", prompt, "一只橘猫坐在窗台上")
    eq("无覆盖项", over, {})
    eq("无警告", warn, [])

    prompt, over, warn = comfyui.parse_draw_command("一只猫 --尺寸 768x768 --步数 30 -n 模糊")
    eq("剥掉参数后的提示词", prompt, "一只猫")
    eq("尺寸解析", (over["width"], over["height"]), (768, 768))
    eq("步数解析", over["steps"], "30")
    eq("负向解析", over["negative"], "模糊")

    for sep in ("x", "×", "*", ",", "，"):
        _, over, _ = comfyui.parse_draw_command(f"猫 --尺寸 512{sep}640")
        eq(f"尺寸分隔符 {sep}", (over["width"], over["height"]), (512, 640))

    # 不认识的 --xx 原样留在提示词里，不能被吞掉
    prompt, over, warn = comfyui.parse_draw_command("一只猫 --风格 动漫")
    check("未知参数保留在提示词里", "--风格" in prompt and "动漫" in prompt, prompt)
    eq("未知参数不进覆盖项", "风格" in over, False)

    # 缺值
    prompt, over, warn = comfyui.parse_draw_command("一只猫 --步数")
    eq("缺值时提示词仍是干净的一只猫", prompt, "一只猫")
    check("缺值有警告", any("少了值" in w for w in warn), warn)

    # 尺寸写错
    _, over, warn = comfyui.parse_draw_command("猫 --尺寸 很大")
    check("尺寸看不懂有警告", any("看不懂" in w for w in warn), warn)
    check("尺寸写错时不写入覆盖项", "width" not in over)

    # 英文开关
    _, over, _ = comfyui.parse_draw_command("cat --model A --sampler dpmpp_2m --seed 42")
    eq("英文 --model", over["model"], "A")
    eq("英文 --sampler", over["sampler"], "dpmpp_2m")
    eq("英文 --seed", over["seed"], "42")


# ---------------------------------------------------------------- 5. 自然语言触发

def section_trigger():
    section("5. 自然语言触发与提示词清洗")

    for text in ("画一张猫", "帮我画只猫", "生成一张夕阳", "文生图：赛博朋克城市", "AI画一个女孩"):
        check(f"识别为生图请求: {text}", comfyui.looks_like_draw_query(text) is True)
    for text in ("这张图不错", "今天天气怎么样", "你好", ""):
        check(f"不误判: {text or '(空)'}", comfyui.looks_like_draw_query(text) is False)

    eq("剥客气话（连用）", comfyui.clean_prompt("帮我画一张 猫"), "猫")
    eq("剥动词+量词", comfyui.clean_prompt("画一只橘猫"), "橘猫")
    eq("本来就没有前缀时不动", comfyui.clean_prompt("一只在雨里的猫"), "一只在雨里的猫")
    eq("剥掉首尾标点", comfyui.clean_prompt("生成一张，夕阳。"), "夕阳")


# ---------------------------------------------------------------- 6. 工作流

def section_workflow():
    section("6. 工作流拼装")

    opts = comfyui.GenOptions(prompt="猫", negative="模糊", model="A.safetensors",
                              steps=20, cfg=7.0, width=512, height=512, seed=999)
    wf = comfyui.build_txt2img_workflow(opts)

    eq("节点数", len(wf), 7)
    for node in ("3", "4", "5", "6", "7", "8", "9"):
        check(f"节点 {node} 存在", node in wf)
    eq("KSampler 类型", wf["3"]["class_type"], "KSampler")
    eq("CheckpointLoaderSimple 类型", wf["4"]["class_type"], "CheckpointLoaderSimple")
    eq("SaveImage 前缀", wf["9"]["inputs"]["filename_prefix"], "qqbot")
    eq("正向提示词接线", wf["6"]["inputs"]["text"], "猫")
    eq("负向提示词接线", wf["7"]["inputs"]["text"], "模糊")
    eq("模型接到 KSampler", wf["3"]["inputs"]["model"], ["4", 0])
    eq("VAE 接到 VAEDecode", wf["8"]["inputs"]["vae"], ["4", 2])
    eq("batch_size", wf["5"]["inputs"]["batch_size"], 1)
    eq("seed 落成具体数值（可复现）", wf["3"]["inputs"]["seed"], 999)
    eq("workflow_seed 读回", comfyui.workflow_seed(wf), 999)
    eq("workflow_seed 对坏数据安全", comfyui.workflow_seed({}), -1)

    # seed = -1 时必须落成具体数值，不能把 -1 传下去
    random_wf = comfyui.build_txt2img_workflow(
        comfyui.GenOptions(prompt="猫", model="A", seed=-1))
    check("随机种子被落成具体值", random_wf["3"]["inputs"]["seed"] >= 0)

    # batch 至少 1
    b = comfyui.build_txt2img_workflow(comfyui.GenOptions(prompt="猫", model="A", batch=0))
    eq("batch 下限为 1", b["5"]["inputs"]["batch_size"], 1)


# ---------------------------------------------------------------- 7. 返回解析

def section_parse_result():
    section("7. 返回体解析")

    payload = {"error": {"type": "prompt_outputs_failed_validation",
                         "message": "Prompt outputs failed validation",
                         "details": "bad value"},
               "node_errors": {"4": {"errors": [{"message": "Value not in list: ckpt_name"}]}}}
    text = comfyui.find_prompt_error(payload)
    check("错误里含 message", "Prompt outputs failed validation" in text, text)
    check("错误里含节点细节", "ckpt_name" in text, text)
    check("不把机器码 type 当正文", "prompt_outputs_failed_validation" not in text, text)

    eq("纯字符串错误", comfyui.find_prompt_error({"error": "炸了"}), "炸了")
    eq("无错误返回空", comfyui.find_prompt_error({}), "")
    eq("非字典安全返回空", comfyui.find_prompt_error(None), "")
    check("只有 node_errors 也能给出说明",
          "ckpt_name" in comfyui.find_prompt_error(
              {"node_errors": {"4": {"errors": [{"message": "ckpt_name missing"}]}}}))

    entry = {"outputs": {"9": {"images": [
        {"filename": "a.png", "subfolder": "", "type": "output"},
        {"filename": "a.png", "subfolder": "", "type": "output"},      # 重复
        {"filename": "b.png", "subfolder": "sub", "type": "temp"},
    ]}, "10": {"images": [{"filename": "c.png"}]}, "11": {"text": "无图"}}}
    refs = comfyui.extract_images(entry)
    eq("去重后张数", len(refs), 3)
    eq("缺省 type 补成 output", refs[2]["type"], "output")
    eq("带上了节点号", refs[0]["node"], "9")
    eq("非字典输入安全", comfyui.extract_images(None), [])
    eq("没有 outputs 安全", comfyui.extract_images({"outputs": None}), [])

    eq("成功状态无错误", comfyui.history_error({"status": {"status_str": "success"}}), "")
    oom = {"status": {"status_str": "error", "messages": [
        ["execution_error", {"exception_message": "CUDA out of memory"}]]}}
    check("失败状态给出细节", "CUDA out of memory" in comfyui.history_error(oom),
          comfyui.history_error(oom))
    check("失败但无细节时有兜底文案",
          comfyui.history_error({"status": {"status_str": "error"}}) != "")


# ---------------------------------------------------------------- 8. 落盘

def section_files():
    section("8. 结果落盘与清理")

    eq("safe_stem 去路径", comfyui.safe_stem("a/b/c.png"), "c")
    eq("safe_stem 防路径穿越", comfyui.safe_stem("../../etc/passwd"), "passwd")
    check("safe_stem 清奇怪字符", "/" not in comfyui.safe_stem("a<>b|c.png"), comfyui.safe_stem("a<>b|c.png"))
    eq("safe_stem 全空时给兜底", comfyui.safe_stem("///"), "image")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        ref = {"filename": "qqbot_00001_.png", "subfolder": "", "type": "output"}
        first = comfyui.save_image(directory, b"x" * 10, ref, now=1_700_000_000)
        check("文件名带时间戳", "202311" in first.name, first.name)
        eq("后缀保留", first.suffix, ".png")
        eq("内容写入正确", first.read_bytes(), b"x" * 10)

        same = comfyui.save_image(directory, b"y", ref, now=1_700_000_000)
        check("同一秒重名不覆盖", same != first and same.exists() and first.exists(), str(same))

        weird = comfyui.save_image(directory, b"z", {"filename": "noext"}, now=1_700_000_100)
        eq("无后缀默认补 .png", weird.suffix, ".png")

        # 清理：留最新 keep 张
        for i in range(5):
            comfyui.save_image(directory, b"1", {"filename": f"k{i}.png"}, now=1_700_001_000 + i)
        before = len(comfyui.list_saved_images(directory))
        removed = comfyui.prune_images(directory, 3)
        after = len(comfyui.list_saved_images(directory))
        check("prune 后只剩 3 张", after == 3, f"{before} -> {after}")
        eq("删掉的数量", removed, before - 3)
        eq("keep=0 不清理", comfyui.prune_images(directory, 0), 0)
        eq("目录不存在时列空", comfyui.list_saved_images(directory / "nope"), [])


# ---------------------------------------------------------------- 9. 报告文案

def section_reports():
    section("9. 报告文案")

    models = _models(checkpoints=["A.safetensors", "sub/B.safetensors"], loras=["l1", "l2"],
                     samplers=["euler", "dpmpp_2m"], schedulers=["normal"], node_count=1805)
    settings = _settings(default_model="A.safetensors", width=512, height=512, steps=20)

    offline = comfyui.format_models_report(comfyui.ComfyModels(), settings, online=False)
    check("离线报告提示启动 ComfyUI", "启动 ComfyUI" in offline, offline)
    check("离线报告给出地址", "8188" in offline)

    report = comfyui.format_models_report(models, settings, base_url="http://127.0.0.1:8188")
    check("报告列出检查点", "A.safetensors" in report, report)
    check("报告列出 LoRA", "l1" in report)
    check("报告含默认模型", "当前默认模型" in report)
    check("报告含用法", "/画" in report)

    empty_report = comfyui.format_models_report(comfyui.ComfyModels(), settings)
    check("空清单时给提示", "没能读到任何模型" in empty_report, empty_report)

    status = comfyui.format_status_report(
        base_url="http://127.0.0.1:8188", models=models,
        stats={"devices": [{"name": "RTX 5060"}]},
        queue={"queue_running": [], "queue_pending": [1, 2]},
        settings=settings, used=3, limit=20, image_count=7, image_dir="data/images")
    check("状态含设备名", "RTX 5060" in status, status)
    check("状态含排队数", "排队 2 个" in status, status)
    check("状态含额度", "3 / 20" in status, status)

    opts = comfyui.GenOptions(prompt="猫", negative="模糊", model="sub/A.safetensors",
                              notes=["提示词太长，已截到 300 字"])
    text = comfyui.describe_options(opts, 12345)
    check("回执含提示词", "猫" in text)
    check("回执含种子", "12345" in text)
    check("回执含负向", "模糊" in text)
    check("回执含说明", "太长" in text)
    check("回执里模型只显示主干", "sub/" not in text, text)


def section_config_map():
    section("9b. settings_from_config")

    class FakeCfg:
        ai_image_default_model = "A.safetensors"
        ai_image_steps = 30

    got = comfyui.settings_from_config(FakeCfg())
    eq("读到显式字段", got["default_model"], "A.safetensors")
    eq("读到显式步数", got["steps"], 30)
    eq("缺失字段走默认", got["sampler"], "euler")
    eq("缺失字段走默认(宽)", got["width"], 512)
    eq("空对象也不炸", comfyui.settings_from_config(object())["max_prompt"], 300)

    eq("normalize_base 补协议", comfyui.normalize_base("127.0.0.1:8188"), "http://127.0.0.1:8188")
    eq("normalize_base 去尾斜杠", comfyui.normalize_base("http://a.b:88/"), "http://a.b:88")
    eq("normalize_base 空值回落默认", comfyui.normalize_base(""), comfyui.DEFAULT_BASE)


# ---------------------------------------------------------------- 10. 假服务器

CHECKPOINTS = ["dreamshaper_8.safetensors", "SDXL/sd_xl_base_1.0.safetensors"]
OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [CHECKPOINTS, {}]}}},
    "LoraLoader": {"input": {"required": {"lora_name": [["anime.safetensors"], {}]}}},
    "KSampler": {"input": {"required": {
        "sampler_name": [["euler", "dpmpp_2m"], {}],
        "scheduler": [["normal", "karras"], {}]}}},
    "VAELoader": {"input": {"required": {"vae_name": [["v.safetensors"], {}]}}},
}

STATE = {"polls": 0, "payload": None, "prompt_id": "pid-0001"}
MODE = {
    "delay_polls": 2,       # /history 前几次返回空，模拟「还在画」
    "no_output": False,     # 跑完但没有图片
    "error_status": False,  # 跑完但状态是 error
    "omit_prompt_id": False,
    "http_error": False,    # /prompt 直接 400
    "never": False,         # /history 永远返回空，用来测超时
}


def reset_state(**mode):
    STATE.update({"polls": 0, "payload": None, "prompt_id": "pid-0001"})
    MODE.update({"delay_polls": 2, "no_output": False, "error_status": False,
                 "omit_prompt_id": False, "http_error": False, "never": False})
    MODE.update(mode)


def _entry(prompt_id):
    outputs = {"9": {}} if MODE["no_output"] else {
        "9": {"images": [{"filename": "qqbot_00001_.png", "subfolder": "", "type": "output"}]}}
    if MODE["error_status"]:
        status = {"status_str": "error", "completed": False, "messages": [
            ["execution_start", {"prompt_id": prompt_id}],
            ["execution_error", {"exception_message": "CUDA out of memory"}]]}
    else:
        status = {"status_str": "success", "completed": True, "messages": []}
    return {"prompt": [], "outputs": outputs, "status": status}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 别刷屏
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, blob, ctype="image/png"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/object_info":
            self._json(OBJECT_INFO)
        elif path == "/system_stats":
            self._json({"system": {"os": "nt", "comfyui_version": "0.39.2"},
                        "devices": [{"name": "Fake GPU", "type": "cuda",
                                     "vram_total": 8 * 1024**3, "vram_free": 6 * 1024**3}]})
        elif path == "/queue":
            self._json({"queue_running": [], "queue_pending": []})
        elif path.startswith("/history/"):
            prompt_id = path[len("/history/"):]
            STATE["polls"] += 1
            if MODE["never"] or STATE["polls"] <= MODE["delay_polls"]:
                self._json({})
            else:
                self._json({prompt_id: _entry(prompt_id)})
        elif path == "/view":
            self._bytes(PNG_BYTES)
        else:
            self.send_error(404)

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/prompt":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        STATE["payload"] = json.loads(self.rfile.read(length) or b"{}")
        if MODE["http_error"]:
            self._json({"error": {"type": "prompt_outputs_failed_validation",
                                  "message": "Prompt outputs failed validation"},
                        "node_errors": {"4": {"errors": [
                            {"message": "Value not in list: ckpt_name"}]}}}, 400)
            return
        response = {"number": 1, "node_errors": {}}
        if not MODE["omit_prompt_id"]:
            response["prompt_id"] = STATE["prompt_id"]
        self._json(response)


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def section_server(base):
    section("10. 假 ComfyUI 端到端")

    client = comfyui.ComfyClient(base, timeout=5.0)

    reset_state()
    check("探活 online()", await client.online() is True)

    models, error = await comfyui.load_models(client)
    eq("load_models 无错误", error, "")
    eq("清单解析出检查点", models.checkpoints, CHECKPOINTS)
    eq("清单解析出 LoRA", models.loras, ["anime.safetensors"])

    # 完整链路：提交 → 轮询 2 次空 → 出结果 → 取图
    reset_state(delay_polls=2)
    settings = _settings(default_model="dreamshaper", sampler="euler", scheduler="normal")
    opts = comfyui.build_options("a cat", {"steps": "8"}, settings, models)
    eq("模糊名命中", opts.model, "dreamshaper_8.safetensors")
    workflow = comfyui.build_txt2img_workflow(opts)

    entry = await client.generate(workflow, timeout=20, poll=0.1)
    eq("轮询次数（前 2 次空）", STATE["polls"], 3)
    refs = comfyui.extract_images(entry)
    eq("拿到 1 张图", len(refs), 1)
    data = await client.fetch_image(refs[0])
    eq("取到的图片字节数", len(data), len(PNG_BYTES))
    eq("提交体里带了工作流", STATE["payload"]["prompt"]["4"]["inputs"]["ckpt_name"],
       "dreamshaper_8.safetensors")
    check("提交体里带了 client_id", bool(STATE["payload"].get("client_id")))

    # 跑完但没图
    reset_state(delay_polls=0, no_output=True)
    try:
        await client.generate(workflow, timeout=10, poll=0.1)
        check("无图时抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("无图时抛 ComfyError", "没有图片输出" in str(exc), str(exc))

    # 状态 error
    reset_state(delay_polls=0, error_status=True)
    try:
        await client.generate(workflow, timeout=10, poll=0.1)
        check("error 状态抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("error 状态抛 ComfyError", "生成失败" in str(exc) and "CUDA" in str(exc), str(exc))

    # /prompt 返回 400，细节在 node_errors
    reset_state(http_error=True)
    try:
        await client.generate(workflow, timeout=10, poll=0.1)
        check("400 时抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("400 时抛 ComfyError", "ckpt_name" in str(exc), str(exc))

    # 没返回 prompt_id
    reset_state(omit_prompt_id=True)
    try:
        await client.submit(workflow)
        check("缺 prompt_id 抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("缺 prompt_id 抛 ComfyError", "prompt_id" in str(exc), str(exc))

    # 超时（直接给一个马上到期的 deadline，避免等 5 秒）
    reset_state(never=True)
    try:
        await client.wait_result("pid-x", deadline=time.monotonic() + 0.5, poll=0.15)
        check("超时抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("超时抛 ComfyError", "超时" in str(exc), str(exc))

    # 连不上
    dead = comfyui.ComfyClient("http://127.0.0.1:1", timeout=2.0)
    check("连不上时 online() 为 False", await dead.online() is False)
    try:
        await dead.object_info()
        check("连不上时抛 ComfyError", False, "没有抛异常")
    except comfyui.ComfyError as exc:
        check("连不上时抛 ComfyError", "连不上" in str(exc), str(exc))

    _, error = await comfyui.load_models(dead)
    check("load_models 把错误当返回值而不是抛出", bool(error), error)


# ---------------------------------------------------------------- 11. 静态检查

def section_static():
    section("11. 配置与命令的一致性")

    config_src = (PROJECT / "plugins" / "ai_chat" / "config.py").read_text(encoding="utf-8")
    cfg_keys = set(re.findall(r"^\s{4}(ai_image_[a-z_]+)\s*:", config_src, re.M))
    check("config.py 有生图配置项", len(cfg_keys) >= 20, f"实际 {len(cfg_keys)} 个")

    launcher_src = (PROJECT / "launcher.py").read_text(encoding="utf-8")
    env_keys = set(re.findall(r'"(AI_IMAGE_[A-Z_]+)"', launcher_src))
    expected_env = {"AI_" + key[3:].upper() for key in cfg_keys}
    missing = expected_env - env_keys
    check("生图配置项全部进了启动器 .env 模板（重跑启动器不丢配置）",
          not missing, f"缺: {sorted(missing)}")

    example = (PROJECT / ".env.example").read_text(encoding="utf-8")
    check(".env.example 说明了生图开关", "AI_IMAGE_ENABLED" in example)
    check(".env.example 说明了接口地址", "AI_IMAGE_API_BASE" in example)

    import ast

    tree = ast.parse((PROJECT / "plugins" / "ai_chat" / "__init__.py").read_text(encoding="utf-8"))
    commands = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "on_command" and node.args and isinstance(node.args[0], ast.Constant):
                commands.append(str(node.args[0].value))
    check("注册了 /画 命令", "画" in commands, f"已注册: {commands}")
    check("注册了 /画图模型 命令", "画图模型" in commands, f"已注册: {commands}")

    init_src = (PROJECT / "plugins" / "ai_chat" / "__init__.py").read_text(encoding="utf-8")
    check("__init__ 里接上了 comfyui 模块", "comfyui" in init_src)
    check("__init__ 里有生图流程函数 _image_flow", "_image_flow" in init_src)


# ---------------------------------------------------------------- 可选：真实 ComfyUI

async def section_live():
    section("12. 真实 ComfyUI（--live）")

    base = "http://127.0.0.1:8188"
    client = comfyui.ComfyClient(base, timeout=30.0)
    if not await client.online():
        print(f"    [SKIP] 连不上 {base}，跳过实时测试")
        return

    models, error = await comfyui.load_models(client)
    eq("真实环境读到检查点", error, "", )
    print(f"    实际：检查点 {len(models.checkpoints)} 个 / LoRA {len(models.loras)} 个 / "
          f"采样器 {len(models.samplers)} 个")
    if not models.checkpoints:
        print("    [SKIP] 本机没有检查点模型，无法出图")
        return

    settings = _settings(default_model=models.checkpoints[0], steps=8, cfg=7.0,
                         width=512, height=512, max_width=1024, max_height=1024)
    options = comfyui.build_options("a cat on a windowsill, soft light", {}, settings, models)
    workflow = comfyui.build_txt2img_workflow(options)
    started = time.time()
    try:
        entry = await client.generate(workflow, timeout=180, poll=0.8)
    except comfyui.ComfyError as exc:
        check("真实生成", False, str(exc))
        return
    refs = comfyui.extract_images(entry)
    check("真实生成出图", bool(refs), f"耗时 {time.time() - started:.1f}s")
    if refs:
        data = await client.fetch_image(refs[0])
        check("真实取图非空", len(data) > 1000, f"{len(data)} 字节")
        print(f"    耗时 {time.time() - started:.1f}s，模型 {options.model}，"
              f"种子 {comfyui.workflow_seed(workflow)}")


# ---------------------------------------------------------------- 主流程

def main():
    print("=" * 72)
    print("生图功能测试（ComfyUI 接入）")
    print(f"项目目录：{PROJECT}")
    print("=" * 72)

    section_parse()
    section_match()
    section_options()
    section_command()
    section_trigger()
    section_workflow()
    section_parse_result()
    section_files()
    section_reports()
    section_config_map()

    server = start_server()
    base = f"http://127.0.0.1:{PORT}"
    try:
        asyncio.run(section_server(base))
    finally:
        server.shutdown()

    section_static()

    if LIVE:
        asyncio.run(section_live())
    else:
        print("\n[12. 真实 ComfyUI] 默认跳过（加 --live 才连本机 8188 真出图）")

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项，失败 {_FAIL} 项")
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
