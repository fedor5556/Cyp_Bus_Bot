@echo off
cd /d "%~dp0"
TITLE Cyprus Bus - Git Push

echo ==================================================
echo          Cyprus Bus Analysis - Quick Push
echo ==================================================
echo.

:: Show current status
echo [INFO] Changed files:
git status --short
echo.

:: Check if there are any changes
git diff --quiet --cached 2>nul
git diff --quiet 2>nul
git status --porcelain | findstr /r "." >nul 2>nul
if errorlevel 1 (
    echo [INFO] No changes to commit. Everything is up to date.
    echo.
    pause
    exit /b 0
)

:: Stage all changes
git add .
echo [OK] All changes staged.
echo.

:: Prompt for commit message
set /p MSG="Commit message (or press Enter for 'Update'): "
if "%MSG%"=="" set MSG=Update

:: Commit
git commit -m "%MSG%"
if errorlevel 1 (
    echo.
    echo [ERROR] Commit failed! See above for details.
    pause
    exit /b 1
)

echo.
echo [INFO] Pushing to origin/main...
git push origin main
if errorlevel 1 (
    echo.
    echo [ERROR] Push failed! Check your internet connection.
    pause
    exit /b 1
)

echo.
echo ==================================================
echo  [SUCCESS] Code pushed to GitHub!
echo  Now send /update to the Admin Bot on Telegram
echo  to deploy it to the server.
echo ==================================================
echo.
pause
