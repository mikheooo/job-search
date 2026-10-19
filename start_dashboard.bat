@echo off
title Job Search Dashboard Server
cd /d "C:\Users\Misha\Documents\job-search"
set PYTHONPATH=C:\Users\Misha\Documents\job-search\.venv\Lib\site-packages;C:\Users\Misha\Documents\job-search
set PYTHON=C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe
echo ===================================================
echo   Job Search Web Dashboard Server
echo   URL: http://localhost:8000
echo ===================================================
"%PYTHON%" -m ai_assistant.cli ui --port 8000
pause
