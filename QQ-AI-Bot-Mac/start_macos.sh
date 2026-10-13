#!/usr/bin/env bash
# ============================================================================
#  QQ 群 AI 机器人 · macOS 启动器
#
#  用法：
#    bash start_macos.sh            前台运行（Ctrl-C 停止，能看到日志）
#    bash start_macos.sh --bg       后台运行（关掉终端也不停，日志写到 logs/bot.log）
#    bash start_macos.sh --stop     停止后台运行的实例
#    bash start_macos.sh --status   看看现在跑没跑
#
#  说明：macOS 上 NapCat 是挂在 QQ.app 里跑的，本脚本只负责启动 NoneBot 这一半。
#        NapCat 那半边用 open -a /Applications/QQ.app 启动（--bg 会自动尝试）。
# ============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
LOGDIR="$ROOT/logs"
LOGFILE="$LOGDIR/bot.log"
PIDFILE="$LOGDIR/bot.pid"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
  YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

ok()   { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
err()  { printf '%s✗%s %s\n' "$RED" "$RESET" "$1" >&2; }
info() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
die()  { err "$1"; exit 1; }

# 关掉代理对回环的影响：开着系统代理时，有些库会把 127.0.0.1 的请求也发给代理，
# 表现就是莫名其妙的 502。这个坑在 Windows 上踩过一次。
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

read_port() {
  if [ -f "$ROOT/.env" ]; then
    P="$(grep -E '^PORT=' "$ROOT/.env" | head -1 | cut -d= -f2- | tr -d ' \r')"
  fi
  echo "${P:-8080}"
}

running_pid() {
  [ -f "$PIDFILE" ] || return 1
  PID="$(cat "$PIDFILE" 2>/dev/null || echo)"
  [ -n "$PID" ] || return 1
  kill -0 "$PID" 2>/dev/null || return 1
  echo "$PID"
}

ACTION="foreground"
case "${1:-}" in
  --bg|--background|-b) ACTION="background" ;;
  --stop|-s)            ACTION="stop" ;;
  --status)             ACTION="status" ;;
  "" )                  ACTION="foreground" ;;
  -h|--help)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  *) die "不认识的参数：$1（试试 --help）" ;;
esac

PORT="$(read_port)"

# ---------------------------------------------------------------- status / stop
if [ "$ACTION" = "status" ]; then
  if PID="$(running_pid)"; then
    ok "后台运行中，PID $PID"
    info "日志：$LOGFILE"
  else
    warn "没有在后台运行"
  fi
  exit 0
fi

if [ "$ACTION" = "stop" ]; then
  if PID="$(running_pid)"; then
    kill "$PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$PID" 2>/dev/null; then
      warn "还在退出，强制结束"
      kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    ok "已停止（PID $PID）"
  else
    warn "没有在后台运行的实例"
  fi
  exit 0
fi

# ---------------------------------------------------------------- 前置检查
[ -x "$PY" ] || die "还没搭好环境。先跑：bash setup_macos.sh"
[ -f "$ROOT/.env" ] || die "缺少 .env。先跑：bash setup_macos.sh"

if grep -qE '^AI_API_KEY=\s*$' "$ROOT/.env"; then
  warn ".env 里的 AI_API_KEY 是空的，机器人会起不来。"
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  warn "端口 $PORT 已被占用，可能已经有一个实例在跑。"
  info "查是谁占着：lsof -nP -iTCP:$PORT -sTCP:LISTEN"
fi

mkdir -p "$LOGDIR"

# ---------------------------------------------------------------- 启动
if [ "$ACTION" = "background" ]; then
  if PID="$(running_pid)"; then
    warn "已经在后台跑了（PID $PID），不重复启动"
    info "日志：$LOGFILE"
    exit 0
  fi

  printf '\n%s后台启动%s\n' "$BOLD" "$RESET"
  nohup "$PY" "$ROOT/bot.py" >>"$LOGFILE" 2>&1 &
  PID=$!
  echo "$PID" > "$PIDFILE"

  sleep 3
  if kill -0 "$PID" 2>/dev/null; then
    ok "已启动，PID $PID"
    info "日志：$LOGFILE"
    info "看日志：tail -f $LOGFILE"
    info "停止：  bash start_macos.sh --stop"
  else
    rm -f "$PIDFILE"
    err "启动就退出了。最后 20 行日志："
    tail -20 "$LOGFILE" 2>/dev/null || true
    exit 1
  fi

  # 顺手把 NapCat 拉起来（macOS 上 NapCat 跑在 QQ.app 里）
  if [ -d "/Applications/QQ.app" ]; then
    if pgrep -f "QQ.app/Contents/MacOS" >/dev/null 2>&1; then
      ok "NapCat（QQ.app）已在运行"
    else
      printf '  %s尝试启动 NapCat（open -a /Applications/QQ.app）…%s\n' "$DIM" "$RESET"
      open -a /Applications/QQ.app 2>/dev/null && ok "已拉起 QQ.app" \
        || warn "没拉起来，手动双击 /Applications/QQ.app 试试"
    fi
  else
    warn "没找到 /Applications/QQ.app —— NapCat 还没装，见 MACOS-迁移指南.md"
  fi

  printf '\n'
  exit 0
fi

# 前台
printf '\n%s前台运行%s  %s(Ctrl-C 停止)%s\n\n' "$BOLD" "$RESET" "$DIM" "$RESET"
exec "$PY" "$ROOT/bot.py"
