@echo off
cd /d "%~dp0"
echo ==================================================
echo STARTING CYPRUS BUS MONITORING SYSTEM
echo ==================================================
call venv\Scripts\activate.bat
if not exist logs mkdir logs
powershell -NoProfile -Command "python -u src\monitor.py 2>&1 | Tee-Object -FilePath logs\monitor.log"
pause
