@echo off
rem QQ AI Bot - Windows launcher
rem First run: creates venv and installs dependencies, then starts the UI.
rem NOTE: run "chcp 65001" so UTF-8 text from Python shows correctly.
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from python.org
    echo         and check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo First run: creating virtual environment ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv. Is Python 3.10+ installed?
        pause
        exit /b 1
    )
    echo Installing dependencies, this may take a few minutes ...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check your network and retry.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" launcher.py
if errorlevel 1 pause
