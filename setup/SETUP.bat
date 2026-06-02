@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
TITLE Cyprus Bus Server - Setup

echo.
echo  ============================================================
echo       Cyprus Bus Analysis Server - Automated Setup
echo  ============================================================
echo.

:: ================================================================
:: STEP 1: Verify Python and Git
:: ================================================================
echo [STEP 1/7] Checking prerequisites...
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Python is not installed or not in PATH!
    echo   Please install Python 3.10+ from https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   [OK] %%i found

git --version >nul 2>&1
if errorlevel 1 (
    echo   [ERROR] Git is not installed or not in PATH!
    echo   Please install Git from https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('git --version 2^>^&1') do echo   [OK] %%i found
echo.

:: ================================================================
:: STEP 2: Choose install location
:: ================================================================
echo [STEP 2/7] Choose installation directory.
echo.
set "DEFAULT_DIR=C:\CypBusBot"
set /p INSTALL_DIR="Install location (press Enter for %DEFAULT_DIR%): "
if "%INSTALL_DIR%"=="" set "INSTALL_DIR=%DEFAULT_DIR%"

:: Check if directory already exists
if exist "%INSTALL_DIR%\.git" (
    echo.
    echo   [WARNING] %INSTALL_DIR% already contains a git repository!
    echo   If you continue, it will be deleted and re-cloned.
    set /p CONFIRM="Continue? (y/n): "
    if /i not "!CONFIRM!"=="y" (
        echo   Setup cancelled.
        pause
        exit /b 0
    )
    echo   Removing existing directory...
    rmdir /s /q "%INSTALL_DIR%" 2>nul
)
echo.

:: ================================================================
:: STEP 3: Clone the repository
:: ================================================================
echo [STEP 3/7] Cloning repository from GitHub...
echo.
git clone https://github.com/fedor5556/Cyp_Bus_Bot.git "%INSTALL_DIR%"
if errorlevel 1 (
    echo.
    echo   [ERROR] Git clone failed! Check your internet connection.
    pause
    exit /b 1
)
echo   [OK] Repository cloned to %INSTALL_DIR%
echo.

:: ================================================================
:: STEP 4: Copy .env configuration
:: ================================================================
echo [STEP 4/7] Copying configuration files...
echo.

set "DATA_SRC=%~dp0server_data"

if not exist "%DATA_SRC%\.env" (
    echo   [ERROR] server_data\.env not found!
    echo   Make sure the server_data folder is next to this script with the .env file inside.
    pause
    exit /b 1
)

copy /y "%DATA_SRC%\.env" "%INSTALL_DIR%\.env" >nul
echo   [OK] .env configuration copied

:: ================================================================
:: STEP 5: Copy database files
:: ================================================================
echo.
echo [STEP 5/7] Copying database files...
echo.

:: Create data directory if it doesn't exist
if not exist "%INSTALL_DIR%\data" mkdir "%INSTALL_DIR%\data"

set "DB_COPIED=0"
if exist "%DATA_SRC%\bus_data.db" (
    copy /y "%DATA_SRC%\bus_data.db" "%INSTALL_DIR%\data\bus_data.db" >nul
    echo   [OK] bus_data.db copied
    set "DB_COPIED=1"
)
if exist "%DATA_SRC%\bus_data.db-wal" (
    copy /y "%DATA_SRC%\bus_data.db-wal" "%INSTALL_DIR%\data\bus_data.db-wal" >nul
    echo   [OK] bus_data.db-wal copied
)
if exist "%DATA_SRC%\bus_data.db-shm" (
    copy /y "%DATA_SRC%\bus_data.db-shm" "%INSTALL_DIR%\data\bus_data.db-shm" >nul
    echo   [OK] bus_data.db-shm copied
)

if "%DB_COPIED%"=="0" (
    echo   [WARNING] No database file found in server_data\
    echo   The system will start with an empty database.
)
echo.

:: ================================================================
:: STEP 6: Create virtual environment and install dependencies
:: ================================================================
echo [STEP 6/7] Setting up Python virtual environment...
echo   This may take 1-2 minutes...
echo.

cd /d "%INSTALL_DIR%"

python -m venv venv
if errorlevel 1 (
    echo   [ERROR] Failed to create virtual environment!
    pause
    exit /b 1
)
echo   [OK] Virtual environment created

:: Activate venv and install
call venv\Scripts\activate.bat

echo.
echo   Installing Python packages...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo   [WARNING] Some packages may have failed to install.
    echo   The system might still work. Check the output above.
) else (
    echo   [OK] All dependencies installed successfully
)
echo.

:: ================================================================
:: STEP 7: Launch everything
:: ================================================================
echo [STEP 7/7] Launching the Cyprus Bus Analysis Pipeline...
echo.

if exist "%INSTALL_DIR%\COMPLETE_LAUNCH.bat" (
    start "" cmd /c "cd /d "%INSTALL_DIR%" && COMPLETE_LAUNCH.bat"
    echo   [OK] Pipeline launched!
) else (
    echo   [ERROR] COMPLETE_LAUNCH.bat not found in %INSTALL_DIR%!
    echo   Please run it manually.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   SETUP COMPLETE!
echo  ============================================================
echo.
echo   Install location: %INSTALL_DIR%
echo.
echo   Three windows should have opened:
echo     1. Bus Monitor Orchestrator - collects live bus data
echo     2. Public ETA Bot - Telegram ETA bot
echo     3. Admin Deployment Bot - handles remote updates
echo.
echo   DO NOT close any of these windows!
echo   They need to run 24/7 for the system to work.
echo.
echo   To update the code remotely, send /update to the
echo   Admin Bot on Telegram.
echo.
echo  ============================================================
echo.
pause
