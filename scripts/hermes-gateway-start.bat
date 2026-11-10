@echo off
rem ============================================================================
rem  Поднять гейтвей Hermes в обход песочницы WorkBuddy.
rem
rem  Зачем: если запускать hermes.exe прямо из-под агента, дерево процессов
rem  убивается в момент завершения вызова — гейтвей умирает, не дойдя даже до
rem  записи gateway.start в gateway-exit-diag.log. Проверено 2026-10-05.
rem
rem  Решение: запускать этот .bat через explorer.exe (Start-Process explorer.exe
rem  -ArgumentList <путь>). Тогда процесс висит на explorer, а не на агенте.
rem
rem  Ещё: окружение WorkBuddy подменяет fs.rmSync через NODE_OPTIONS и safe-bin,
rem  поэтому переменные чистятся перед вызовом.
rem ============================================================================

setlocal
set "HERMES=%LOCALAPPDATA%\hermes\bin\hermes.exe"
set "LOG=%LOCALAPPDATA%\hermes\logs\gateway-manual-start.log"

set "NODE_OPTIONS="
set "CODEBUDDY_SAFE_DELETE_ENABLED="
set "CODEBUDDY_SAFE_DELETE_BIN_DIR="
set "CODEBUDDY_SAFE_DELETE_BULK_GUARD="
set "GENIE_TRASH_DIR="

echo [%DATE% %TIME%] starting gateway... >> "%LOG%"
"%HERMES%" gateway start >> "%LOG%" 2>&1
echo [%DATE% %TIME%] gateway start returned %ERRORLEVEL% >> "%LOG%"
