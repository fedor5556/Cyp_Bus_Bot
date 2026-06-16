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

:: Stage everything first so we can tell if there is anything new to commit.
git add .

:: If there are staged changes, commit them. If not, SKIP the commit but still
:: push - there may be commits made earlier (e.g. from a tool) that were never
:: pushed. The old version exited here on a clean tree and never pushed those.
git diff --cached --quiet
if errorlevel 1 goto commit
echo [INFO] Nothing new to commit - checking for unpushed commits...
goto push

:commit
set /p MSG="Commit message (or press Enter for 'Update'): "
if "%MSG%"=="" set MSG=Update
git commit -m "%MSG%"
if errorlevel 1 (
    echo.
    echo [ERROR] Commit failed! See above for details.
    pause
    exit /b 1
)
echo [OK] Changes committed.

:push
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
echo  [SUCCESS] Repo is up to date on GitHub!
echo  Now send /update to the Admin Bot on Telegram
echo  to deploy it to the server.
echo ==================================================
echo.
pause
