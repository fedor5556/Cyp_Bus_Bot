@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat
python src\analysis\predict_eta.py --bot
pause
