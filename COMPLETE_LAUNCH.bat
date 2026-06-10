@echo off
cd /d "%~dp0"
TITLE Cyprus Bus Analysis - COMPLETE LAUNCH
echo ==============================================================
echo      Starting Cyprus Bus Analysis Pipeline ^& Bots
echo ==============================================================
echo.

if not exist logs mkdir logs
if exist logs\runner.stop del logs\runner.stop

:: If the central runner (Admin_hub\runner.py) is alive, hand off to it: it
:: starts this project's processes hidden and keeps them alive. Otherwise
:: fall back to the legacy visible-window launch below.
powershell -NoProfile -Command "$r = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'runner\.py' }; if ($r) { exit 0 } else { exit 1 }"
if %ERRORLEVEL%==0 goto :runner

echo [WARN] Central runner not detected - legacy visible-window launch.
echo.

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
    echo [INFO] Virtual environment found.
) else (
    echo [WARNING] Virtual environment not found! Using system Python.
)
echo.

:: Kill any existing project processes (including zombies from old folders)
echo [INFO] Cleaning up old processes...
"%PYTHON_CMD%" src\stop_processes.py
echo.

:: Auto-install/verify all dependencies
echo [INFO] Checking dependencies...
"%PYTHON_CMD%" -m pip install -r requirements.txt
echo.

:: 1. Launch the Main Data Monitor
echo [LAUNCH] Starting Data Orchestrator (run_monitor.bat)...
start "Bus Monitor Orchestrator" cmd /c "call run_monitor.bat"

:: 2. Launch the Public/Main ETA Telegram Bot
echo [LAUNCH] Starting Main ETA Telegram Bot (start_telegram_bot.bat)...
start "Public ETA Bot" cmd /c "call start_telegram_bot.bat"
echo.
echo All systems launched in separate terminal windows.
:: plain "exit" (not /b): the Hub starts this bat via `start`, which keeps the
:: console open after the script ends - exit closes the window too.
exit 0

:runner
echo [INFO] Central runner detected - requesting hidden (re)start.
echo start > logs\runner.start
exit 0
