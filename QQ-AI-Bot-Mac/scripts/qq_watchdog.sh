#!/bin/bash
# QQ 机器人看门狗：由 cron 每分钟调用一次。
# 谁没在跑就拉谁，已经在跑就什么都不做 —— 幂等，可以随便重复执行。
# 覆盖三种场景：开机后自动启动、进程崩溃自动重启、与任何终端/对话会话无关。
#
# 想手动停某个服务（让看门狗别再拉起）：
#   touch /Users/luoluolink/WorkBuddy/2026-10-11-20-57-49/qq-ai-bot/QQ-AI-Bot-Mac/.paused_napcat
#   touch /Users/luoluolink/WorkBuddy/2026-10-11-20-57-49/qq-ai-bot/QQ-AI-Bot-Mac/.paused_bot
# 想恢复：删掉对应的 .paused_* 文件，下一分钟内自动拉起。

ROOT="/Users/luoluolink/WorkBuddy/2026-10-11-20-57-49/qq-ai-bot/QQ-AI-Bot-Mac"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

log() { echo "$(date '+%F %T') [watchdog] $*" >> "$LOG_DIR/watchdog.log"; }

# ---------------- NapCat（QQ 协议端，无头运行） ----------------
if [ ! -f "$ROOT/.paused_napcat" ]; then
    # 只认带 --no-sandbox 的 QQ 主进程（那是 NapCat 模式），
    # 双击打开的原版 QQ（不带参数）不算数，互不干扰
    if ! pgrep -f "MacOS/QQ --no-sandbox" >/dev/null 2>&1; then
        # 必须经由 .app 外壳启动：直接从工具/脚本上下文 exec QQ 会让它的
        # 沙盒容器初始化崩掉（libsecinit SIGTRAP），外壳经 LaunchServices
        # 解析后责任进程干净，能正常起。详见 scripts/NapCatStarter.app。
        "$ROOT/.venv/bin/python" "$ROOT/scripts/spawn_detached.py" \
            "$LOG_DIR/napcat.log" "$LOG_DIR/napcat.err.log" "-" "-" \
            "$ROOT/scripts/NapCatStarter.app/Contents/MacOS/napcat_starter"
        log "拉起 NapCat（经外壳，守护式）"
    fi
fi

# ---------------- NoneBot（机器人本体） ----------------
if [ ! -f "$ROOT/.paused_bot" ]; then
    if ! pgrep -f "python.*bot\.py" >/dev/null 2>&1; then
        "$ROOT/.venv/bin/python" "$ROOT/scripts/spawn_detached.py" \
            "$LOG_DIR/bot.log" "$LOG_DIR/bot.err.log" "$ROOT" "-" \
            "$ROOT/.venv/bin/python" "$ROOT/bot.py"
        log "拉起机器人（守护式）"
    fi
fi
