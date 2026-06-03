@echo off
TITLE Cyprus Bus Bot - STOP ALL
echo ==============================================================
echo      Stopping ONLY Bus Bot processes...
echo ==============================================================
echo.

:: Get the directory this script is in (= project root)
set "PROJECT_DIR=%~dp0"

:: Use PowerShell to find and kill ONLY python.exe processes
:: whose executable path is inside THIS project's venv folder
echo [INFO] Scanning for Bus Bot python processes...
echo [INFO] Project: %PROJECT_DIR%
echo.

powershell -NoProfile -Command ^
  "$venvPath = '%PROJECT_DIR%venv'; " ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue; " ^
  "$killed = 0; " ^
  "foreach ($p in $procs) { " ^
  "  if ($p.ExecutablePath -and $p.ExecutablePath.ToLower().Contains($venvPath.ToLower())) { " ^
  "    Write-Host \"  [KILL] PID $($p.ProcessId): $($p.CommandLine)\"; " ^
  "    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; " ^
  "    $killed++; " ^
  "  } " ^
  "} " ^
  "if ($killed -eq 0) { Write-Host '  No Bus Bot processes found.' } " ^
  "else { Write-Host \"  Stopped $killed process(es).\" }"

echo.
echo ==============================================================
echo  Done. Your other programs are untouched.
echo  To restart the Bus Bot, double-click COMPLETE_LAUNCH.bat
echo ==============================================================
pause
