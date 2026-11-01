@echo off
chcp 65001 >nul
title HH Headless — Проверка авторизации и вход
cd /d "C:\Users\Misha\Documents\job-search"
set "PYTHONPATH=C:\Users\Misha\Documents\job-search\.venv\Lib\site-packages;C:\Users\Misha\Documents\job-search"
set "PYTHONIOENCODING=utf-8"

"C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe" -u "C:\Users\Misha\Documents\job-search\scripts\hh_open_login_window.py" %*

echo.
pause
