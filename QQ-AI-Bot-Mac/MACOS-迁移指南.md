# 迁移到 Mac：完整指南

这份文档是给**在新 Mac 上接手的人（或 WorkBuddy）**看的。照着做，大约 15 分钟能跑起来。

---

## 0. 这个包里有什么

```
QQ-AI-Bot-Mac/
├── MACOS-迁移指南.md      ← 你正在看的这份
├── setup_macos.sh         ← 一键搭环境（找 Python、建 venv、装依赖、自检）
├── start_macos.sh         ← 启动 / 后台运行 / 停止 / 查状态
├── README.md              ← 完整功能文档（12 个章节，含所有踩过的坑）
├── .env                   ← ★ 你的真实配置：API Key、人设、所有功能开关，已填好
├── .env.example           ← 配置模板（给别人用的时候拷这份）
├── bot.py                 ← 入口
├── check_api.py           ← 单独检查模型 API 通不通
├── mock_napcat.py         ← 假协议端：不装 QQ 也能验整条链路
├── requirements.txt       ← 依赖（3 个包）
├── plugins/ai_chat/       ← 全部业务代码
│   ├── __init__.py        ←   核心逻辑
│   ├── config.py          ←   ★ 人设提示词在这里
│   ├── members.py         ←   群成员识别
│   ├── welcome.py         ←   入群欢迎
│   ├── websearch.py       ←   联网额度账本
│   ├── resource_index.py  ←   群文件/相册检索
│   ├── essence.py         ←   群精华检索
│   ├── vision.py          ←   识图
│   └── stickers.py        ←   表情包
├── tests/                 ← 154 项离线测试（不联网、不花钱）
└── stickers/              ← 表情包图片目录
```

**已经带过来了**：人设（吉普莉尔）、API Key 与接口地址、模型名、全部功能开关、以及现有的所有代码和测试。

**不用带**：`.venv`（Windows 的虚拟环境在 Mac 上不能用，`setup_macos.sh` 会重新建）、`data/`（联网额度账本，Mac 上从零开始算）、日志。

---

## 1. 三步概览

| 步骤 | 做什么 | 在哪做 |
|---|---|---|
| ① | 把整个文件夹拷到 Mac，跑 `setup_macos.sh` | 终端 |
| ② | 装 NapCat（让 QQ 号上线） | **图形界面，必须手动** |
| ③ | 启动，验证 | 终端 |

第 ② 步是唯一无法自动化的：macOS 上的 NapCat 要往 `QQ.app` 的沙箱里装东西，必须由用户在系统设置里授权，脚本做不了。

---

## 2. 第 ① 步：环境

### 2.1 把文件夹放好

建议放在用户主目录下，路径**不要有中文和空格**（能省掉一堆编码问题）：

```bash
mv ~/Downloads/QQ-AI-Bot-Mac ~/QQ-AI-Bot-Mac
cd ~/QQ-AI-Bot-Mac
```

### 2.2 检查 Python 版本

macOS 自带的 `/usr/bin/python3` 通常是 **3.9，版本不够**（项目要求 3.10+）。

```bash
python3 --version
```

如果显示 3.9.x，装一个新的：

- **方式 A（推荐）**：打开 <https://www.python.org/downloads/macos/>，下载 3.12 的安装包，双击装完。
- **方式 B（有 Homebrew）**：`brew install python@3.12`

> Apple Silicon（M1/M2/M3/M4）和 Intel Mac 都可以，项目是纯 Python，没有平台相关的依赖。

### 2.3 一键搭建

```bash
bash setup_macos.sh
```

它会依次：找 Python → 建 `.venv` → 装依赖（自动走清华/阿里镜像，失败自动换源）→ 跑离线测试 → 检查 API 连通性。

**看到最后那行 `环境好了` 就成了。** 重复运行是安全的，依赖没变过会跳过。

---

## 3. 第 ② 步：装 NapCat（关键，且只能手动）

NapCat 的作用：把你的 QQ 号挂上线，把 QQ 协议翻译成 OneBot v11 协议给机器人用。

> ⚠️ **务必用小号。** 第三方协议端有封号风险，这是腾讯侧的判断，不是本项目的问题。详见 `README.md` 第 5 章和第 7 章。

### 3.1 装 QQ

从腾讯官网下载 macOS 版 QQ，安装后确认它在这个位置：

```bash
ls -d /Applications/QQ.app
```

**必须是 `/Applications/QQ.app`。** NapCat 的安装器就认这个路径。

### 3.2 装 NapCat

1. 打开 <https://github.com/NapNeko/NapCat-Mac-Installer/releases/latest>
2. 下载安装器（支持 macOS 12+，Intel 与 Apple Silicon 通用包）
3. 打开它，选择 NapCat 版本和下载方式，点安装
4. 按提示**备份并修改 QQ 入口** → 选「切换程序入口 **NapCat**」→ 启动

如果系统提示权限不足，去：

```
系统设置 → 隐私与安全性 → App 管理 → 允许 NapCat 安装器管理 QQ
```

装好之后，**双击 `/Applications/QQ.app` 启动的就是 NapCat 了**（不再是原版 QQ）。想换回去就在安装器里选「切换程序入口 原版 QQ」。

### 3.3 登录

第一次启动 NapCat 会让你扫码登录 QQ。用手机 QQ 扫，确认。

> 如果报 `serverErrorCode 168`（「账号近期存在安全风险」），这是**腾讯对账号的限制**，不是代码问题。处理办法见 `README.md` 第 7 章那一节，那里有完整的时间线和恢复步骤。

### 3.4 配置文件在哪

macOS 上 NapCat 的配置在 QQ 的沙箱容器里：

```bash
~/Library/Containers/com.tencent.qq/Data/Documents/napcat
```

里面应该有 `config/` 目录。看一眼确认：

```bash
ls -la ~/Library/Containers/com.tencent.qq/Data/Documents/napcat/config/
```

---

## 4. 第 ③ 步：把机器人接到 NapCat 上

机器人用的是**反向 WebSocket**：机器人先起一个 WS 服务（默认 `127.0.0.1:8080`），NapCat 主动连过来。所以要先配 NapCat 的连接参数。

### 方式 A：改配置文件（推荐）

在 `~/Library/Containers/com.tencent.qq/Data/Documents/napcat/config/` 下创建 `onebot11.json`：

```json
{
  "network": {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [
      {
        "name": "nonebot",
        "enable": true,
        "url": "ws://127.0.0.1:8080/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 5000,
        "token": "把 .env 里的 ONEBOT_ACCESS_TOKEN 抄到这里",
        "debug": false,
        "heartInterval": 30000
      }
    ],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false
}
```

`token` 必须和本项目 `.env` 里的 `ONEBOT_ACCESS_TOKEN` **完全一致**。查看：

```bash
grep ONEBOT_ACCESS_TOKEN ~/QQ-AI-Bot-Mac/.env
```

> 如果目录里已经存在 `onebot11_<你的QQ号>.json`，说明 NapCat 已经给这个账号单独存过配置——**它优先于 `onebot11.json`**，改那个才对。

### 方式 B：WebUI 里点

NapCat 启动后有一个网页控制台：

```
http://127.0.0.1:6099/webui?token=napcatwebui
```

在里面「网络配置」新增一个 **WebSocket 客户端（反向）**：

| 字段 | 值 |
|---|---|
| 名称 | nonebot |
| URL | `ws://127.0.0.1:8080/onebot/v11/ws` |
| 消息格式 | array |
| Token | 同 `.env` 里的 `ONEBOT_ACCESS_TOKEN` |
| 心跳间隔 | 30000 |
| 重连间隔 | 5000 |

改完**重启 NapCat**（退出 QQ.app 再打开）。

### 启动顺序很重要

**先起机器人，再起 NapCat。** 机器人是 WS 服务端，它得先在那里听着，NapCat 才连得上。

```bash
cd ~/QQ-AI-Bot-Mac
bash start_macos.sh --bg     # 后台起机器人，会自动尝试拉起 QQ.app
```

看日志确认连上了：

```bash
tail -f ~/QQ-AI-Bot-Mac/logs/bot.log
```

出现类似 `OneBot V11 | Bot 123456789 connected` 就通了。

然后**在群里 @ 一下机器人**，比如「@机器人 你好」。有回复就全部搞定。

---

## 5. 日常使用

```bash
cd ~/QQ-AI-Bot-Mac

bash start_macos.sh            # 前台跑，能看日志，Ctrl-C 停
bash start_macos.sh --bg       # 后台跑，关终端也不停
bash start_macos.sh --status   # 看跑没跑
bash start_macos.sh --stop     # 停
tail -f logs/bot.log           # 看日志
```

群里可用的指令（详见 `README.md`）：

| 指令 | 作用 |
|---|---|
| `@机器人 你的问题` | 聊天 |
| `@机器人 @某人 他怎么样` | 会认出被 @ 的是谁 |
| `@机器人 小a是管理员吗` | 正文里打名字也能认出来 |
| `/files 关键词` | 检索群文件与群相册 |
| `/essence 关键词` | 检索群精华消息 |
| `/成员 关键词` | 查群成员 |
| `/联网` | 看今天联网额度用了多少 |
| `/欢迎` | 看入群欢迎的开关与用量 |
| `/reset` | 清空本群上下文（限群主/管理员） |

---

## 6. 让它一直挂着（Mac 会睡觉，这是重点）

Mac 默认会在闲置后睡眠，睡了机器人就掉线。三件事：

### 6.1 别让 Mac 睡

```bash
caffeinate -disu &
```

或者：`系统设置 → 锁定屏幕 → 显示器关闭时…` 调成「永不」，并把「节能」里的睡眠关掉。

> 笔记本合上盖子通常还是会睡。想真正 7×24，建议插电 + 设置里关掉睡眠；实在不行考虑放在一台 Linux 小服务器上跑（`README.md` 第 6 章有说明）。

### 6.2 开机自启（launchd）

创建 `~/Library/LaunchAgents/cn.qqbot.nonebot.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>cn.qqbot.nonebot</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/你的用户名/QQ-AI-Bot-Mac/.venv/bin/python</string>
        <string>/Users/你的用户名/QQ-AI-Bot-Mac/bot.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/你的用户名/QQ-AI-Bot-Mac</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>NO_PROXY</key>
        <string>127.0.0.1,localhost</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/Users/你的用户名/QQ-AI-Bot-Mac/logs/bot.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/你的用户名/QQ-AI-Bot-Mac/logs/bot.err.log</string>
</dict>
</plist>
```

把里面的 `你的用户名` 换成实际用户名（`whoami` 能看到），然后：

```bash
mkdir -p ~/QQ-AI-Bot-Mac/logs
launchctl load  ~/Library/LaunchAgents/cn.qqbot.nonebot.plist   # 启用
launchctl list | grep qqbot                                     # 看状态
launchctl unload ~/Library/LaunchAgents/cn.qqbot.nonebot.plist  # 停用
```

NapCat 那半边想自启，就把 `/Applications/QQ.app` 加进
`系统设置 → 通用 → 登录项`。

> 用了 launchd 之后，就别再用 `start_macos.sh --bg` 起了，两个实例抢同一个端口。

---

## 7. 和 Windows 版的差异（迁移时容易踩）

| 项 | Windows | macOS |
|---|---|---|
| NapCat 安装 | 解压 zip，跑 `launcher.bat` | 跑图形安装器，改 `QQ.app` 入口 |
| NapCat 配置目录 | NapCat 安装目录下的 `config/` | `~/Library/Containers/com.tencent.qq/Data/Documents/napcat/config/` |
| 启动 QQ/NapCat | `napcat.bat` | 双击 `/Applications/QQ.app` |
| 虚拟环境 | `.venv/Scripts/python.exe` | `.venv/bin/python` |
| 路径分隔符 | 反斜杠 | 正斜杠 |
| 代码本身 | —— | **零改动**，同一份代码两边都能跑 |

**代码没有任何 Windows 专属逻辑**，`.env` 也是全平台通用的（里面没有绝对路径）。所以这个包直接就是 Mac 版，不是「移植版」。

---

## 8. 常见问题

**`setup_macos.sh` 说找不到 Python 3.10+**
装了新 Python 后重开一个终端窗口再跑。如果 `which python3` 还指向 `/usr/bin/python3`，说明装的版本没进 PATH —— 用 python.org 的安装包通常会自动处理。

**依赖装不上 / 超时**
脚本会自动换三个源。全失败的话多半是网络问题，挂上代理再跑：
```bash
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
bash setup_macos.sh
```

**日志里出现 502 / 连接被重置**
检查系统代理是不是把 `127.0.0.1` 也代理走了。脚本已经设了 `NO_PROXY`，但如果是 GUI 方式启动的，要自己确认一遍。

**端口 8080 被占用**
```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
```
改端口的话，`.env` 里的 `PORT` 和 NapCat 配置里的 `url` **两处都要改**。

**NapCat 装完双击 QQ 还是原版**
回安装器里重新执行「切换程序入口 NapCat」。权限被拒就去 `系统设置 → 隐私与安全性 → App 管理` 放行。

**机器人不回复，但日志里能看到消息**
99% 是没 @ 到机器人。它只在被 @ 时说话（刻意的设计，减少风控暴露）。

**报 `serverErrorCode 168`**
腾讯侧的账号限制，不是代码问题。见 `README.md` 第 7 章。

**想换模型 / 改人设**
- 模型与接口：`.env` 里的 `AI_API_BASE` / `AI_API_KEY` / `AI_MODEL`
- 人设（吉普莉尔的语气）：`plugins/ai_chat/config.py` 里的 `ai_system_prompt`
- 改完重启生效

**改完代码想验证**
```bash
.venv/bin/python tests/test_members.py   # 71 项：群成员识别 + 入群欢迎（含端到端）
.venv/bin/python tests/test_gate.py      # 49 项：联网额度闸门
.venv/bin/python tests/test_websearch.py # 34 项：额度账本
```
全部离线，不花钱。

---

## 9. 如果你想让 WorkBuddy 接手

在新 Mac 的 WorkBuddy 里，把下面这段整个贴进去，它就能自己接着干：

```
我要在 Mac 上跑一个 QQ 群 AI 机器人，项目在 ~/QQ-AI-Bot-Mac。

请先完整读一遍 ~/QQ-AI-Bot-Mac/MACOS-迁移指南.md 和 README.md，
然后按指南执行：
1. 跑 bash setup_macos.sh 搭好环境，把输出贴给我看；
2. 检查 /Applications/QQ.app 是否存在，告诉我 NapCat 装没装；
3. 如果装好了，核对 NapCat 的 onebot11 配置（在
   ~/Library/Containers/com.tencent.qq/Data/Documents/napcat/config/）
   里的 token 是否和 .env 里的 ONEBOT_ACCESS_TOKEN 一致，
   不一致就帮我改对；
4. 用 bash start_macos.sh --bg 启动，然后 tail 日志确认
   OneBot 连接成功；
5. 遇到问题先读 README.md 的「常见问题」和「防封号策略」两章，
   不要自己瞎猜。

注意：不要改动 .env 里的 AI_API_KEY；不要把 data/ 目录删掉；
不要把 QQ 号主号拿来用。
```

---

## 10. 安全提醒

- 这个包里的 `.env` **包含你的真实 API Key 和 OneBot Token**。别把它传到公开仓库、别丢进公共网盘。要分享给别人时，用 `.env.example` 那份模板。
- 用的是 QQ **小号**，别用主号。
- 机器人的联网额度默认全局每天 30 次（`AI_WEB_SEARCH_DAILY_LIMIT`），超过会自动停止联网但对话照常。群里发 `/联网` 随时查。
