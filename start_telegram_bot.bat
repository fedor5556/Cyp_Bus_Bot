@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
if not exist logs mkdir logs
powershell -NoProfile -Command ".\venv\Scripts\python.exe -u src\analysis\predict_eta.py --bot 2>&1 | Tee-Object -FilePath logs\telegram_bot.log"
pause
