#!/usr/bin/env bash
# ============================================================================
#  QQ 群 AI 机器人 · macOS 一键环境搭建
#
#  用法：  bash setup_macos.sh
#
#  它做四件事：找 Python → 建独立虚拟环境 → 装依赖 → 检查 API 能不能通。
#  重复运行是安全的：依赖没变过会自动跳过。
#
#  故意用 bash 3.2 语法（macOS 自带的就是 3.2），不要用 bash 4+ 的特性。
# ============================================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
DEPS_MARKER="$VENV/.deps-ok"
REQUIREMENTS="$ROOT/requirements.txt"

# 国内网络优先走镜像，第一个失败会自动换下一个
MIRRORS="https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple https://pypi.org/simple"

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
step() { printf '\n%s[%s]%s %s%s%s\n' "$CYAN" "$1" "$RESET" "$BOLD" "$2" "$RESET"; }
die()  { err "$1"; exit 1; }

printf '\n%sQQ 群 AI 机器人 · macOS 环境搭建%s\n' "$BOLD" "$RESET"
printf '%s%s%s\n' "$DIM" "目录：$ROOT" "$RESET"

# ---------------------------------------------------------------- 1. 系统检查
step 1/4 "检查系统"

if [ "$(uname -s)" != "Darwin" ]; then
  warn "当前不是 macOS（$(uname -s)）。脚本仍会尝试继续，但 NapCat 的安装方式不同。"
else
  ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?')  $(uname -m)"
fi

# Homebrew 的 python 不在默认 PATH 里（尤其是 Apple Silicon）
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export PATH

find_python() {
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

if ! BASE_PY="$(find_python)"; then
  err "没找到 Python 3.10 或更高版本。"
  printf '\n%s装一个（任选其一）：%s\n' "$BOLD" "$RESET"
  printf '  %s方式 A（推荐，图形界面）%s\n' "$CYAN" "$RESET"
  printf '    打开 https://www.python.org/downloads/macos/ 下载 3.12 的安装包，双击装完再跑本脚本。\n'
  printf '  %s方式 B（命令行，需要已装 Homebrew）%s\n' "$CYAN" "$RESET"
  printf '    brew install python@3.12\n'
  printf '\n%s注意%s：macOS 自带的 /usr/bin/python3 通常是 3.9，版本不够。\n' "$YELLOW" "$RESET"
  printf '      如果 which python3 指向 /usr/bin/python3，请务必另装一个。\n\n'
  printf '  查当前版本：python3 --version\n'
  exit 1
fi

ok "Python：$BASE_PY  ($("$BASE_PY" --version 2>&1))"

# ---------------------------------------------------------------- 2. 虚拟环境
step 2/4 "创建独立虚拟环境（不影响系统 Python）"

if [ -x "$VENV/bin/python" ]; then
  ok "已存在：.venv"
else
  "$BASE_PY" -m venv "$VENV" || die "创建虚拟环境失败。"
  ok "已创建：.venv"
fi

PY="$VENV/bin/python"
"$PY" -m pip install --upgrade pip --disable-pip-version-check -q >/dev/null 2>&1 || true

# ---------------------------------------------------------------- 3. 依赖
step 3/4 "安装依赖（首次约 1~3 分钟）"

if [ ! -f "$REQUIREMENTS" ]; then
  die "找不到 requirements.txt，目录是不是不完整？"
fi

if command -v shasum >/dev/null 2>&1; then
  WANT="$(shasum -a 256 "$REQUIREMENTS" | awk '{print $1}')"
else
  WANT="$(md5 -q "$REQUIREMENTS" 2>/dev/null || echo unknown)"
fi

if [ -f "$DEPS_MARKER" ] && [ "$(cat "$DEPS_MARKER" 2>/dev/null)" = "$WANT" ]; then
  ok "依赖已是最新，跳过安装"
else
  INSTALLED=0
  for MIRROR in $MIRRORS; do
    info "镜像源：$MIRROR"
    if "$PY" -m pip install -r "$REQUIREMENTS" -i "$MIRROR" \
         --disable-pip-version-check -q; then
      printf '%s' "$WANT" > "$DEPS_MARKER"
      ok "依赖安装完成"
      INSTALLED=1
      break
    fi
    warn "这个源失败了，换下一个"
  done
  [ "$INSTALLED" = "1" ] || die "依赖安装失败。手动试试：$PY -m pip install -r requirements.txt"
fi

# ---------------------------------------------------------------- 4. 自检
step 4/4 "自检"

if [ ! -f "$ROOT/.env" ]; then
  warn "没有 .env，从模板复制一份（AI_API_KEY 还是空的，记得填）"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

printf '  %s跑一遍离线测试（不联网、不花钱）…%s\n' "$DIM" "$RESET"
if "$PY" "$ROOT/tests/test_members.py" >/dev/null 2>&1; then
  ok "离线测试通过"
else
  warn "离线测试没全过，但通常不影响启动。想看细节：$PY tests/test_members.py"
fi

printf '  %s检查大模型 API 连通性…%s\n' "$DIM" "$RESET"
if "$PY" "$ROOT/check_api.py"; then
  ok "API 连通"
else
  warn "API 检查没通过。先确认 .env 里的 AI_API_KEY / AI_API_BASE / AI_MODEL 对不对。"
fi

# ---------------------------------------------------------------- 收尾
printf '\n%s────────────────────────────────────────────%s\n' "$CYAN" "$RESET"
printf '%s环境好了。接下来：%s\n' "$BOLD" "$RESET"
printf '\n  %s1.%s 装 NapCat（让 QQ 号上线）—— 这一步必须在图形界面里手动做\n' "$CYAN" "$RESET"
printf '     详细步骤见同目录的 %sMACOS-迁移指南.md%s\n' "$BOLD" "$RESET"
printf '\n  %s2.%s 启动机器人\n' "$CYAN" "$RESET"
printf '     bash start_macos.sh\n'
printf '\n  %s先不装 NapCat 也能验：%s\n' "$CYAN" "$RESET"
printf '     终端 1：  %s .venv/bin/python bot.py\n' "$DIM"
printf '     终端 2：  %s .venv/bin/python mock_napcat.py\n' "$RESET"
printf '%s────────────────────────────────────────────%s\n\n' "$CYAN" "$RESET"
