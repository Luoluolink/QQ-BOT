#!/bin/bash
# ============================================================================
# QQ 群 AI 机器人 · macOS 启动器（对应 Windows 版的「启动.bat」）
#
# 用法：在访达里双击本文件（第一次可能需要在
#       「系统设置 → 隐私与安全性」里点「仍要打开」）。
# 效果：弹出终端跑环境检查 → 自动开浏览器进图形化配置页 →
#       填完点「保存并启动」自动拉起机器人核心和 NapCat（QQ.app）。
#
# 服务进程已做会话隔离：配置完关掉这个终端窗口，机器人照常运行。
# ============================================================================

cd "$(dirname "$0")" || exit 1

# ---- 选 Python：venv 优先 → WorkBuddy 管理的 3.13 → 系统 python3 ----
PY=""
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" ]; then
  PY="$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
fi

if [ -z "$PY" ]; then
  echo ""
  echo "  [X] 没有找到 Python。请安装 Python 3.10 或更新版本："
  echo "      https://www.python.org/downloads/"
  echo ""
  read -r -p "按回车键关闭..." _
  exit 1
fi

# 回环地址不走系统代理（开着 Clash 时有些库会把 127.0.0.1 也发给代理）
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

"$PY" launcher.py "$@"
RC=$?

if [ "$RC" -ne 0 ]; then
  echo ""
  echo "  [X] 启动器退出码 $RC"
fi

# 双击运行时窗口别一闪而过，让用户看清最后的输出
if [ -t 0 ]; then
  read -r -p "按回车键关闭本窗口（机器人核心与 NapCat 会继续运行）..." _
fi
exit $RC
