@echo off
setlocal
set "HERMES=%LOCALAPPDATA%\hermes\bin\hermes.exe"

rem --- Defensive: strip the WorkBuddy Node shim from the environment.
rem If this window was opened from a WorkBuddy terminal, NODE_OPTIONS points at
rem WorkBuddy's node-language-shim, which breaks Hermes' desktop build with
rem "genie-trash ETIMEDOUT". A plain Explorer double-click is already clean,
rem but this costs nothing.
set "NODE_OPTIONS="
set "CODEBUDDY_SAFE_DELETE_ENABLED="
set "CODEBUDDY_SAFE_DELETE_BIN_DIR="
set "CODEBUDDY_SAFE_DELETE_BULK_GUARD="
set "GENIE_TRASH_DIR="

if not exist "%HERMES%" (
    echo ERROR: hermes.exe not found at %HERMES%
    pause
    exit /b 1
)

echo.
echo ==== STEP 1/2 : restart the Hermes gateway ====
echo.
"%HERMES%" gateway restart
echo.
echo ---- step 1 finished, exit code %ERRORLEVEL% ----
echo.
echo Expected to see: "Gateway started via direct spawn (PID: ...)"
echo That brings Telegram and the cron scheduler back.
echo.

echo ==== STEP 2/2 : finish the interrupted update (rebuilds the desktop app) ====
echo This takes roughly 10-30 minutes. Do not close the window.
echo Press Ctrl+C now if you only wanted step 1.
pause

"%HERMES%" update --yes
echo.
echo ---- step 2 finished, exit code %ERRORLEVEL% ----
echo.
echo Exit code 1 is usually harmless: it means the fleet version probe
echo did not see the gateway within 120 seconds. Look for "Update complete!"
echo in the output above.
echo.
pause
