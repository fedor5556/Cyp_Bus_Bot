@echo off
cd /d "%~dp0"
echo ==================================================
echo STARTING CYPRUS BUS MONITORING SYSTEM
echo ==================================================
call venv\Scripts\activate.bat
:: The monitor writes its own rotating UTF-8 log (logs\monitor.log) from inside
:: Python (src\log_tee.py). NEVER pipe/Tee/redirect output into that file here -
:: a second writer locks it (see TELEGRAM_BOT_NOTE.md in GEMINI_PROJECTS root).
.\venv\Scripts\python.exe -u src\monitor.py
pause
