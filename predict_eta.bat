@echo off
echo.
echo Starting ETA Prediction Script...
echo.
call venv\Scripts\activate.bat
python src\analysis\predict_eta.py
pause
