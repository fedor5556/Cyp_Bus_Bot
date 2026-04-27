@echo off
echo.
echo Starting Schedule Viewer...
echo.
call venv\Scripts\activate.bat
python src\analysis\show_schedule_limassol.py
echo.
pause
