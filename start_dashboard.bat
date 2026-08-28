@echo off
title Job Search Dashboard Server
cd /d "C:\Users\Misha\Documents\job-search"
echo ===================================================
echo   Job Search Web Dashboard Server
echo   URL: http://localhost:8000
echo ===================================================
".venv\Scripts\python.exe" -m ai_assistant.cli ui --port 8000
pause
