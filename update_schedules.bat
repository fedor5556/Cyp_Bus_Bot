@echo off
echo ==================================================
echo DOWNLOADING LATEST STATIC BUS SCHEDULES
echo ==================================================
call venv\Scripts\activate.bat
python src\ingestion\fetch_static.py
echo.
echo Schedules updated successfully!
pause
