@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title QQ AI Bot

echo.
echo   QQ AI Bot - launcher
echo   --------------------

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python  >nul 2>nul && set "PY=python"
if not defined PY where python3 >nul 2>nul && set "PY=python3"

if not defined PY (
  echo.
  echo   [X] 这台电脑上没有检测到 Python，本程序需要 Python 3.10 或更新版本。
  echo.
  echo   正在为你打开 Python 官方下载页面……
  echo.
  echo   安装时请注意两点：
  echo     1. 勾选「Add python.exe to PATH」（默认可能没勾，必须勾上）
  echo     2. 装完后重新双击本程序即可，依赖会自动安装，不用手动操作
  echo.
  start "" "https://www.python.org/downloads/"
  echo   如果浏览器没有自动打开，请手动访问：
  echo       https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY% "%~dp0launcher.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [X] Launcher exited with code %RC%.
  pause
)
endlocal
