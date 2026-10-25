@echo off
rem QQ AI Bot - Windows launcher
rem First run: creates venv and installs dependencies, then starts the UI.
rem NOTE: run "chcp 65001" so UTF-8 text from Python shows correctly.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---------- 第一步：找一个真能用的 Python（3.10+）----------
rem Windows 上 "python" 有时会解析到 Microsoft Store 的 0 字节存根
rem （没装 Python 时系统自带的假 python，where 能找到，但一跑就弹商店）。
rem 所以优先用 py 启动器（官网安装包默认带），再退回 python。
set "PYCMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] 没有找到可用的 Python。请到 python.org 下载 3.10 或更新版本，
    echo         安装时务必勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

echo Using Python: %PYCMD%
%PYCMD% --version
if errorlevel 1 (
    echo.
    echo [ERROR] 这个 Python 跑不起来 —— 大概率是 Microsoft Store 的占位符
    echo         （没装真 Python 时系统自带的假 python，执行只会弹商店）。
    echo         解决：到 python.org 下载安装 3.10+，勾选 "Add python.exe to PATH"，
    echo         装完重开本窗口再跑。
    pause
    exit /b 1
)

rem ---------- 第二步：建虚拟环境 + 装依赖（仅首次）----------
if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment ...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] venv 创建失败，请按以下方向排查：
        echo   1. 没装 Python：python.org 下载 3.10+，勾选 Add python.exe to PATH
        echo   2. 装的是 Microsoft Store 版：开始菜单搜 "Python" 看图标是不是商店样式，
        echo      是则卸载它，改装官网版
        echo   3. 杀毒软件/Defender 拦截：暂时关掉或把本文件夹加白名单
        echo   4. 放在同步盘/权限特殊目录：把项目挪到普通本地目录（如 D:\QQBot）再跑
        echo.
        %PYCMD% --version
        pause
        exit /b 1
    )
    echo Installing dependencies, this may take a few minutes ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] 依赖安装失败。国内网络慢的话，可改镜像源重试：
        echo   ".venv\Scripts\python.exe" -m pip install -r requirements.txt ^
        -i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

rem ---------- 第二步半：客户端窗口组件（pywebview，每次启动都检查）----------
rem 不放「仅首次」块里：之前装失败/没装的人，重跑也能补上。
rem 检测用 import（已装则秒过）；装不上只警告不阻塞 —— 退回浏览器模式照样能用。
".venv\Scripts\python.exe" -c "import webview, clr" >nul 2>nul
if errorlevel 1 (
    echo Installing client window component ^(pywebview^) ...
    ".venv\Scripts\python.exe" -m pip install -q "pywebview[edgechromium]"
    if errorlevel 1 (
        echo [WARN] pywebview 装不上，将退回浏览器模式打开配置页（功能不受影响）
    )
)

rem ---------- 第三步：拉起启动器 ----------
".venv\Scripts\python.exe" launcher.py
if errorlevel 1 pause
