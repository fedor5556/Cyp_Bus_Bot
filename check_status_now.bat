@echo off
echo ==================================================
echo FETCHING LIVE DATA AND CHECKING GEOFENCE (ONCE)
echo ==================================================
call venv\Scripts\activate.bat
python src\ingestion\fetch_rt.py
python src\analysis\geofence.py
pause
