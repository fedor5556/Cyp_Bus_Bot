@echo off
cd /d "%~dp0"
echo ==================================================
echo STARTING CYPRUS BUS MONITORING SYSTEM
echo ==================================================
call venv\Scripts\activate.bat
python src\monitor.py
pause
