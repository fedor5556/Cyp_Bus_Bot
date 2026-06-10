@echo off
cd /d "%~dp0"
TITLE Cyprus Bus Bot - STOP ALL
echo ==============================================================
echo      Stopping ONLY Bus Bot processes...
echo ==============================================================
echo.

:: Tell the central runner this stop is intentional (it would otherwise
:: auto-restart the processes). Written BEFORE killing.
if not exist logs mkdir logs
if exist logs\runner.start del logs\runner.start
echo stop > logs\runner.stop

:: Determine the correct Python executable
set "PYTHON_CMD=python"
if exist "venv\Scripts\python.exe" (
    set "PYTHON_CMD=%~dp0venv\Scripts\python.exe"
)

"%PYTHON_CMD%" src\stop_processes.py

echo.
echo ==============================================================
echo  Done. To restart, double-click COMPLETE_LAUNCH.bat
echo ==============================================================
pause
