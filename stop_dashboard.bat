@echo off
echo Stopping Job Search Web Dashboard...
powershell -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
echo Server successfully stopped.
timeout /t 2 >nul
