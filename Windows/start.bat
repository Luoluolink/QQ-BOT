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
  echo   [X] Python not found.
  echo.
  echo   Install Python 3.10 or newer from:
  echo       https://www.python.org/downloads/
  echo   During setup, check "Add Python to PATH".
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
