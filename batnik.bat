@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM --------------- Конфигурация ---------------
set "PYTHON_VERSION=3.11.6"
set "PYTHON_INSTALLER=python-%PYTHON_VERSION%-amd64.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/%PYTHON_VERSION%/%PYTHON_INSTALLER%"
set "TMP_INSTALLER=%TEMP%\%PYTHON_INSTALLER%"
set "SCRIPT=script.py"

echo ====================================================
echo  Инсталлятор автоматической установки Python + Playwright
echo ====================================================
echo Текущая папка: %CD%
echo.

REM ---------------- Проверка наличия python ----------------
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [OK] Python найден в системе.
    goto :INSTALL_DEPS
)

echo [INFO] Python не найден в PATH. Устанавливаем Python %PYTHON_VERSION%...
echo [INFO] Скачивание из: %PYTHON_URL%

REM Скачиваем через PowerShell
powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%TMP_INSTALLER%' -UseBasicParsing"
if not exist "%TMP_INSTALLER%" (
    echo [ERROR] Не удалось скачать установщик
    echo Откройте в браузере: %PYTHON_URL%
    echo И установите Python вручную
    pause
    exit /b 1
)

echo [INFO] Запускаю установщик Python...
echo Это может занять несколько минут...
"%TMP_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_pip=1
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Установка завершена с ошибкой. Попробуйте запустить вручную: %TMP_INSTALLER%
) else (
    echo [OK] Установка Python завершена
)

REM Очистка
if exist "%TMP_INSTALLER%" del /f /q "%TMP_INSTALLER%"

REM Обновляем PATH для текущей сессии
for /f "skip=2 tokens=1,2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYSTEM_PATH=%%c"
set "PATH=%SYSTEM_PATH%;%PATH%"

:INSTALL_DEPS
echo.
echo [INFO] Проверяем и обновляем pip...
python -m pip install --upgrade pip

echo [INFO] Устанавливаем playwright и pyperclip...
python -m pip install playwright pyperclip
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Ошибка установки пакетов
    pause
    exit /b 1
)

echo [INFO] Устанавливаем браузер Chromium...
python -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo [WARN] Ошибка установки Chromium, но продолжаем...
)

REM ---------------- Запуск скрипта ----------------
echo.
if not exist "%SCRIPT%" (
    echo [ERROR] Файл %SCRIPT% не найден!
    echo.
    echo Содержимое папки:
    dir /b
    echo.
    echo Убедитесь, что файлы batnik.bat и %SCRIPT% находятся в одной папке
    pause
    exit /b 1
)

echo [INFO] Запускаю %SCRIPT%...
echo ОТКРОЕТСЯ БРАУЗЕР - войдите в аккаунт Roblox, затем вернитесь сюда и нажмите Enter
echo.
python "%SCRIPT%"

echo.
echo [DONE] Работа завершена
pause