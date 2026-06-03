@echo off
cd /d "%~dp0"
TITLE Cyprus Bus Bot - STOP ALL
echo ==============================================================
echo      Stopping ONLY Bus Bot processes...
echo ==============================================================
echo.

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python src\stop_processes.py

echo.
echo ==============================================================
echo  Done. To restart, double-click COMPLETE_LAUNCH.bat
echo ==============================================================
pause
