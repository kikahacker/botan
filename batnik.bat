@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SCRIPT=get_cookie_playwright.py"
set "REQUIREMENTS=requirements.txt"

where python >nul 2>nul || (
  echo [ERROR] Python not found in PATH
  pause & exit /b 1
)

python -m ensurepip >nul 2>nul
if not exist ".venv" (
  echo [INFO] Creating venv...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

python -m pip install --upgrade pip >nul
if exist "%REQUIREMENTS%" (
  echo [INFO] Installing from requirements.txt...
  pip install -r "%REQUIREMENTS%"
) else (
  echo [INFO] Installing playwright + pyperclip...
  pip install playwright pyperclip
)

echo [INFO] Ensuring chromium is installed for Playwright...
python -m playwright install chromium

if not exist "%SCRIPT%" (
  echo [ERROR] %SCRIPT% not found
  dir /b
  pause & exit /b 1
)

echo [INFO] Launching %SCRIPT%...
python "%SCRIPT%"
echo [DONE]
pause
