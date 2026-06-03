@echo off
cd /d "%~dp0"
TITLE Cyprus Bus Analysis - COMPLETE LAUNCH
echo ==============================================================
echo      Starting Cyprus Bus Analysis Pipeline ^& Bots
echo ==============================================================
echo.

:: Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment...
    call venv\Scripts\activate.bat
) else (
    echo [WARNING] Virtual environment not found. Using system Python.
)
echo.

:: Kill any existing project processes (including zombies from old folders)
echo [INFO] Cleaning up old processes...
python src\stop_processes.py
echo.

:: Auto-install/verify all dependencies
echo [INFO] Checking dependencies...
venv\Scripts\python.exe -m pip install -r requirements.txt
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
start "Admin Deployment Bot" cmd /c "python src\admin_bot.py"

echo.
echo ==============================================================
echo  All systems launched in separate terminal windows.
echo  You can close this main window safely.
echo ==============================================================
pause
