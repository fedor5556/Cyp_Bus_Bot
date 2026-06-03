@echo off
cd /d "%~dp0"
TITLE Cyprus Bus Analysis - COMPLETE LAUNCH
echo ==============================================================
echo      Starting Cyprus Bus Analysis Pipeline ^& Bots
echo ==============================================================
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
if %ERRORLEVEL% == 0 (
    echo [OK] All dependencies verified.
) else (
    echo [WARNING] Some dependency issues detected. Continuing anyway...
)
echo.

:: 1. Launch the Main Data Monitor
echo [LAUNCH] Starting Data Orchestrator (run_monitor.bat)...
start "Bus Monitor Orchestrator" cmd /c "call run_monitor.bat"

:: 2. Launch the Public/Main ETA Telegram Bot
echo [LAUNCH] Starting Main ETA Telegram Bot (start_telegram_bot.bat)...
start "Public ETA Bot" cmd /c "call start_telegram_bot.bat"

:: 3. Launch the Admin Deployment/Maintenance Bot
echo [LAUNCH] Starting Admin Maintenance Bot (src/admin_bot.py)...
:: Using powershell to Tee the output to a log file, keeping the window open
if not exist logs mkdir logs
start "Admin Deployment Bot" powershell -NoProfile -Command "& '%PYTHON_CMD%' -u src\admin_bot.py 2>&1 | Tee-Object -FilePath logs\admin_bot.log; Read-Host 'Press Enter to exit'"

echo.
echo ==============================================================
echo  All systems launched in separate terminal windows.
echo  You can close this main window safely.
echo ==============================================================
pause
