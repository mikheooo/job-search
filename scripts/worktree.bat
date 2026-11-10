@echo off
rem Job-Search Worktree Wrapper for Windows Command Prompt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0worktree.ps1" %*
