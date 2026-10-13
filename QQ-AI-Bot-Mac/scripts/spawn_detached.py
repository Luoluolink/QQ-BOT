#!/usr/bin/env python3
"""守护式启动器：双重 fork + setsid，把目标进程彻底脱离父进程组。

为什么要双重 fork：WorkBuddy 结束一次工具调用时会清理自己派生的整个
进程组/session，nohup 挡不住（nohup 只忽略 SIGHUP）。双重 fork 让孙进程
失去控制终端、父进程变为 launchd，会话清理就摸不到它了。

用法：
    python3 spawn_detached.py <stdout日志> <stderr日志> <工作目录或-> <环境变量K=V,...或-> <命令> [参数...]

例：
    python3 spawn_detached.py bot.log bot.err.log /path/to/proj - ./.venv/bin/python bot.py
    python3 spawn_detached.py napcat.log napcat.err.log - NAPCAT_LOAD=1 /Applications/QQ.app/Contents/MacOS/QQ --no-sandbox
"""

import os
import sys


def _fork_and_exit_parent() -> None:
    pid = os.fork()
    if pid > 0:
        # 父进程直接退出，让子进程被 launchd 收养
        os._exit(0)


def main() -> None:
    if len(sys.argv) < 6:
        sys.stderr.write(__doc__)
        sys.exit(2)

    out_path, err_path, cwd, env_spec = sys.argv[1:5]
    cmd = sys.argv[5:]

    _fork_and_exit_parent()
    os.setsid()
    _fork_and_exit_parent()  # 再 fork 一次，确保永远不会重新获得控制终端

    devnull = os.open(os.devnull, os.O_RDONLY)
    out = open(out_path, "ab", buffering=0)
    err = open(err_path, "ab", buffering=0)
    os.dup2(devnull, 0)
    os.dup2(out.fileno(), 1)
    os.dup2(err.fileno(), 2)

    if cwd != "-":
        os.chdir(cwd)
    if env_spec != "-":
        for item in env_spec.split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                os.environ[key] = value

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
