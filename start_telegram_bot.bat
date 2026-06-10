@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
:: The bot writes its own rotating UTF-8 log (logs\telegram_bot.log) from inside
:: Python (src\log_tee.py). NEVER pipe/Tee/redirect output into that file here -
:: a second writer locks it (see TELEGRAM_BOT_NOTE.md in GEMINI_PROJECTS root).
.\venv\Scripts\python.exe -u src\analysis\predict_eta.py --bot
pause
