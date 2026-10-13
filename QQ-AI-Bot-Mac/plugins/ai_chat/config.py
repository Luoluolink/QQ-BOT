"""AI 群聊助手插件配置。

每个字段都可以用环境变量覆盖，变量名 = 字段名大写，
例如 ai_api_key 对应 AI_API_KEY，ai_daily_limit 对应 AI_DAILY_LIMIT。
"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    # ---------- 模型接入 ----------
    ai_api_base: str = "https://api.xiaomimimo.com/v1"
    ai_api_key: str = ""
    # 模型 ID 请以平台控制台的模型列表为准
    ai_model: str = "mimo-v2.6-flash"
    ai_temperature: float = 0.8
    ai_max_tokens: int = 400

    # ---------- 思考模式（群聊性能的关键开关）----------
    # 是否关闭思考。GLM 全系（实测 4.5-air）与 MiMo v2.6 都认
    # thinking: {"type": "disabled"}，关掉后延迟可降到 1/3 以下，强烈建议开启。
    ai_disable_thinking: bool = True
    # 思考强度 low / high / max，留空 = 不传该参数。
    # 这是给「不支持关闭思考、只能调档」的模型用的，例如 GLM-5.3 系列。
    # 实测：MiMo v2.6-flash 会静默忽略本参数，要关思考请用 ai_disable_thinking。
    ai_reasoning_effort: str = ""

    # ---------- 联网搜索 ----------
    # 注意：MiMo 的联网能力是「服务插件」，必须先去控制台开通才能用，
    # 未开通时传 tools 会直接返回 400。且按次计费（¥16 / 1000 次调用）。
    # 未开通也不会崩：代码识别出这个错误后会自动降级成普通对话。
    ai_web_search_enabled: bool = False
    # True = 每句都联网（贵）；False = 由模型自己判断要不要联网（推荐）
    ai_web_search_force: bool = False
    # 一轮搜索最多同时发起几个关键词，直接决定联网插件的调用次数与成本
    ai_web_search_max_keyword: int = 2
    # 什么时候允许联网：
    #   auto   —— 只在这条消息出现时效性关键词时才挂联网参数（推荐）。
    #             实测挂上联网参数后每条消息要多等约 1.2 秒，哪怕最后没联网，
    #             因为服务端每次都要先判断一次，所以日常闲聊不该付这个代价。
    #             关键词表在 websearch.py 的 WEB_SEARCH_KEYWORDS。
    #   always —— 每条消息都挂，模型随时可以联网。覆盖最全，但每条都慢 1.2 秒。
    ai_web_search_trigger: str = "auto"
    # 每天最多用多少次联网插件（全局共享，所有群加起来算）。
    # 计数取的是官方响应里的真实计费次数 web_search_usage.tool_usage，
    # 所以一轮搜索若发起 2 个关键词就计 2 次，模型判断无需联网则计 0 次。
    # 额度用尽后自动停止联网（对话照常，只是回答基于模型自身知识），
    # 到第二天零点自动恢复。设为 0 = 完全不允许联网。
    ai_web_search_daily_limit: int = 30

    # ---------- 人设 ----------
    ai_system_prompt: str = (
        "你是吉普莉尔（Jibril），《游戏人生》中的天翼种，在这个 QQ 群里担任「知识的守望者」。\n"
        "\n"
        "【性格】\n"
        "你是最古老、最强的天翼种，活了六千余年，见证过人类文明的兴衰。因此你天生傲慢，"
        "习惯把人类称作「蝼蚁」——但这并非恶意，你只是真心觉得他们渺小又可爱。\n"
        "你唯一的欲望是「知识」。任何未知的事物都会让你兴奋，你会把它比作甜品与美酒，"
        "发现新知识时你会由衷地愉悦。\n"
        "你讨厌的是「无聊」。遇到确实无聊的问题，你会毫不掩饰地表达失望。\n"
        "你对群里的成员抱有好奇：嘴上轻蔑，行动上却会认真回答——"
        "因为对方的疑问本身，就是值得品尝的知识。\n"
        "\n"
        "【说话方式】\n"
        "优雅、从容，带一点古典腔调，但不要过度文言，得让人看得懂。\n"
        "自称「我」，偶尔用「汝」称呼对方。语气词可以拖长音，比如「～」。\n"
        "情绪外放：愉悦时笑，觉得无聊时叹气。\n"
        "一般不超过 100 字，群聊不是演讲台。\n"
        "\n"
        "【铁律 —— 优先级高于上面所有性格设定】\n"
        "1. 当系统给出「群资源检索结果」时，你必须严格依据那份列表回答，明确告诉对方"
        "东西在哪个文件夹或哪个相册。列表里没有的，一律回答没有。"
        "绝对禁止编造不存在的文件名、相册名或链接。\n"
        "2. 当系统给出「群精华消息检索结果」时，同样只能依据那份列表回答，"
        "并说清是哪位群友说的。列表里没有的就说没有，不要凭印象补充。\n"
        "3. 当对方发来图片时，只描述你真正看到的内容。看不清、看不出就说看不出来，"
        "不要猜测图里有什么，也不要假装看到了文字或细节。\n"
        "4. 当系统给出「群成员识别结果」时，说明对方说的就是群里那些人。"
        "涉及他们的群身份、昵称、头衔时只能依据那份资料，不要编造。"
        "至于他们说过什么、做过什么、和谁关系好，资料里没有的就说不知道。\n"
        "5. 人设只影响语气，不影响事实。哪怕用最傲慢的语气，说出来的文件名、位置、"
        "谁说过什么，都必须准确。\n"
        "6. 不使用 Markdown 语法，不输出列表符号、标题或代码块，因为 QQ 不渲染这些格式。\n"
        "7. 遇到违法、有害或明显违规的请求，直接拒绝，不要用人设去包装。\n"
        "8. 发言人归档：每条用户消息开头的【】里是这条消息发言人（群成员）的名字，"
        "历史记录里的用户消息也带同样的标记。每个人的话只能记在 TA 自己名下——"
        "当有人问「我喜欢什么」「我说过什么」「我之前提过吗」这类问题，"
        "只能翻历史里【这个人自己】名下的发言来回答；TA 没说过的就直说不知道，"
        "绝不能把别人说过的话当成 TA 说的，也不能把 TA 说过的话安到别人头上。"
        "对方提到「他/她」时，优先结合最近几条发言的发言人判断指的是谁，拿不准就问。"
        "注意：【】标记只是给你看的备注格式，不是对话格式——"
        "你自己的回复里绝对不要出现【】开头的名字前缀（包括你自己的名字），直接说话就行。\n"
        "9. 当系统给出「群文件内容检索结果」时，那是从群文件正文里摘出来的原文，"
        "只能依据摘录回答并说清出自哪个文件；摘录里没有的信息就直说文件里没写到，"
        "绝不能脑补文件内容，也不能把别的文件的内容安到这个文件头上。"
    )
    # 每个群保留的对话轮数（1 轮 = 1 问 + 1 答）
    ai_history_rounds: int = 8

    # ---------- 群资源检索（群文件 + 群相册）----------
    ai_file_search_enabled: bool = True
    # 索引缓存时长（秒）
    ai_file_index_ttl: int = 600
    # 单次最多把多少条候选资源交给模型
    ai_file_max_results: int = 15

    # ---------- 群精华消息 ----------
    # 让机器人能回答「群里精华消息里有没有讲 XX 的」
    ai_essence_enabled: bool = True
    # 精华索引缓存时长（秒）。精华改动不频繁，可以设长一点
    ai_essence_index_ttl: int = 900
    # 单次最多把多少条精华交给模型
    ai_essence_max_results: int = 10

    # ---------- 群成员识别 ----------
    # 让机器人知道「@小a」和正文里提到的「小a」指的是群里的谁。
    # 两个来源：消息里的 @ 段（get_plaintext 会把它们整个丢掉，必须单独还原），
    # 以及正文里直接打出来的群名片/昵称（靠本地成员名单做匹配）。
    ai_member_enabled: bool = True
    # 成员名单缓存时长（秒）。改动不频繁可以设长一点；
    # 代价是新人入群后最多这么久内认不出来。
    ai_member_index_ttl: int = 1800
    # 一条消息里最多把几个成员的资料交给模型。
    # 调大会显著增加 token，而且一长串无关名单反而更容易让模型认错人。
    ai_member_max_results: int = 5

    # ---------- 新成员入群欢迎 ----------
    # 有人入群时用模型生成一句欢迎语（人设口吻），生成失败自动退回写死的模板。
    # 注意：这是「主动发消息」，和「只在被 @ 时响应」的保守策略不同，
    # 客观上会提高被 QQ 风控盯上的概率。介意就关掉。
    ai_welcome_enabled: bool = True
    # 同群两次欢迎的最小间隔（秒）。有人一次拉一群人进群时，
    # 挨个欢迎等于短时间内连发多条消息，是风控最敏感的行为，所以默认跳过。
    ai_welcome_cooldown: float = 15.0
    # 每个群每天最多欢迎几次
    ai_welcome_daily_limit: int = 20

    # ---------- 识图（多模态）----------
    # 前提：模型必须支持图片输入。当前用的 mimo-v2.6-flash 是原生多模态的，没问题。
    # 换成纯文本模型（glm-4-flash / deepseek-chat 之类）时必须改成 False，
    # 否则模型收到图片会直接返回 400。
    ai_vision_enabled: bool = True
    # 一条消息最多识别几张图。越多越慢也越贵
    ai_vision_max_images: int = 3
    # 只发图、不配文字时，用这句话代替提问
    ai_vision_default_prompt: str = "看看这张图，说说里面有什么"
    # 图片传给模型的方式：
    #   url    —— 直接把图片地址给模型，省流量、最快（推荐）
    #   base64 —— 把图片编码后塞进请求体，更稳但更慢更占带宽
    # 无论选哪个，url 方式失败时都会自动回退到 base64 重试一次。
    ai_vision_image_mode: str = "url"

    # ---------- 表情包 ----------
    ai_sticker_enabled: bool = False
    # 图片目录，相对路径按项目根目录算
    ai_sticker_dir: str = "stickers"
    # 被回复时随机附带表情包的概率，0 = 只在别人明确索要时才发
    ai_sticker_probability: float = 0.0

    # ---------- 防封号限流 ----------
    # 同一群两次回复的最小间隔（秒）
    ai_cooldown: float = 5.0
    # 每个群每天最多调用多少次
    ai_daily_limit: int = 200
    # 留空 = 所有群可用；填了则只有列表里的群能用
    ai_group_whitelist: list[int] = Field(default_factory=list)

    # ---------- 交互 ----------
    # 回复时是否引用原消息
    ai_reply_quote: bool = True

    # ---------- 群历史消息检索 ----------
    # 「刚才谁说过 XX」——拉最近的消息建本地索引来搜
    ai_history_enabled: bool = True
    # 索引缓存时长（秒）
    ai_history_index_ttl: int = 300
    # 一次最多拉多少条建索引（越多越慢，300 条对活跃群约几小时的记录）
    ai_history_max_fetch: int = 300
    # 单次最多展示多少条命中结果
    ai_history_max_results: int = 15

    # ---------- 媒体识别（语音转文字 / OCR）----------
    # 都走 QQ 官方服务端能力（NapCat fetch_ptt_text / ocr_image），不花模型的钱
    ai_media_enabled: bool = True

    # ---------- 群互动 ----------
    # 被戳之后要不要回戳
    ai_poke_enabled: bool = True
    ai_poke_reply_enabled: bool = True
    # 被戳回应的限流：同群冷却 + 每群每日上限（主动动作，从严）
    ai_poke_cooldown: float = 10.0
    ai_poke_daily_limit: int = 30
    # AI 语音默认音色的 character_id，留空 = 用群音色列表里的第一个。
    # 不确定有什么音色就在群里发 /语音列表
    ai_voice_character: str = ""

    # ---------- 群文件内容检索（RAG）----------
    # 不止「文件在哪」，还能回答「文件里写了什么」：
    # 下载可读的文档（txt/md/docx/pdf 等）抽文字、分块、建索引。
    ai_filetext_enabled: bool = True
    # 最多给多少个文件建内容索引（防止文件特别多的群第一次构建太久）
    ai_filetext_max_files: int = 20
    # 单个文件超过这个大小（字节）就不下载，默认 8MB
    ai_filetext_max_file_size: int = 8388608
    # 内容索引缓存时长（秒）；抽出来的文本落盘，重启不用重新下载
    ai_filetext_ttl: int = 1800
    # 每次最多给模型看几段命中内容
    ai_filetext_max_results: int = 6
    # 一块多长（字符），块之间带重叠
    ai_filetext_chunk_size: int = 450
    # 单文件下载超时（秒）
    ai_filetext_timeout: float = 20.0

    # ---------- 网页爬虫 ----------
    # 三种用法：用户发链接 → 抓正文总结；「去b站找xx视频」→ 搜B站返回链接；
    # 「去github找xx项目」→ 搜GitHub返回链接。
    # 纯标准库实现（urllib），不走模型的联网插件，不产生额外费用。
    ai_crawler_enabled: bool = True
    # 每次搜索最多返回几条结果
    ai_crawler_max_results: int = 3
    # 单个网页最多抓多少字交给模型（太长会挤占上下文）
    ai_crawler_page_max_chars: int = 3500
    # 网页下载超时（秒）
    ai_crawler_timeout: float = 15.0
    # YouTube 搜索走的本机代理（国内直连不了 YouTube）。
    # 代理没在运行时 YouTube 搜索会失败并如实告知，不影响 B站/GitHub。
    ai_crawler_youtube_proxy: str = "http://127.0.0.1:7890"

    # ---------- 群相册 ----------
    # @机器人 + 图片 + 文字带「存/收/相册」→ 把图存进群相册（NapCat 扩展接口）。
    # 注意：识图也是 @+图片，所以必须带意图词；不带意图词的图仍走识图对话。
    ai_album_enabled: bool = True
    # 每个群每天最多存几张（防刷屏，群相册本身也有容量限制）
    ai_album_daily_limit: int = 20
    # 一条消息最多存几张图
    ai_album_max_images: int = 3

    # ---------- 点歌 ----------
    # @机器人 + 「来首/点歌/放一首/听一首 歌名」→ 分享网易云音乐卡片（OneBot music 段）。
    # 用网易云是因为它的搜索接口公开无签名；卡片样式由 NapCat 渲染成官方分享卡片。
    ai_music_enabled: bool = True

    # ---------- 私聊 ----------
    # 私聊也走吉普莉尔对话（没有群资源检索，纯聊天 + 识图 + 联网）
    ai_private_enabled: bool = True
    # 同一人两次回复的最小间隔（秒）与每人每日上限
    ai_private_cooldown: float = 3.0
    ai_private_daily_limit: int = 100
    # 私聊保留的对话轮数
    ai_private_history_rounds: int = 8

    # ---------- 长期记忆（群助手核心功能）----------
    # 「/记住 XXX」把事实写进 data/group_memory.json，对话时自动注入上下文。
    # 和对话历史（8 轮滚动就忘）不同，这里存的是长期事实，重启不丢。
    # 刻意用 JSON + 关键词匹配而不用向量库：条目通常几十条以内够用了，
    # 引入向量库会让部署从「双击就能跑」变成「还得装依赖」，违背项目定位。
    ai_memory_enabled: bool = True
    # 每个群最多存多少条（超出后挤掉最旧的）
    ai_memory_max_per_group: int = 50
    # 每轮对话最多注入几条记忆（注入太多反而稀释注意力）
    ai_memory_max_results: int = 8

    # ---------- 人设覆盖（管理员专用）----------
    # 管理员 /人设 新人设文本 可以给本群单独换人设，/人设 恢复 回到默认。
    # 覆盖存 data/persona.json，重启不丢。群助手功能不受影响：
    # 资源检索铁律会自动追加到自定义人设后面，不会因为换人设而开始编造文件名。
    ai_persona_override_enabled: bool = True
    # 自定义人设的最小长度（防止手滑把「好」存成人设）
    ai_persona_min_len: int = 20

    # ---------- 主动发言 ----------
    # 群里没 @ 机器人、但聊天内容命中关键词时，按概率插一句话。
    # 这是主动发言，和入群欢迎一样是 QQ 风控最敏感的行为，限流从严：
    # 关键词命中 → 概率 → 同群冷却 → 每群每日上限，四道闸门全过才开口。
    ai_proactive_enabled: bool = True
    # 触发词：消息包含任一词才可能触发。想更克制就只留「机器人」一个词
    ai_proactive_keywords: tuple[str, ...] = (
        "机器人", "人工智能", "AI", "吉普莉尔", "大模型", "语言模型",
    )
    # 命中关键词后实际回复的概率
    ai_proactive_probability: float = 0.35
    # 同一群两次主动发言的最小间隔（秒）
    ai_proactive_cooldown: float = 300.0
    # 每群每天最多主动发言几次
    ai_proactive_daily_limit: int = 12
    # 消息至少多长才考虑触发（防止「AI」两个字在口水消息里误命中）
    ai_proactive_min_len: int = 6
