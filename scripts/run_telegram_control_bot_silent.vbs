Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
RootDir = fso.GetParentFolderName(ScriptDir)

' Phase 5 windowless: pythonw.exe has NO console subsystem at all, so a
' scheduled start never flashes a cmd/conhost window. The log is written by
' the script itself (FileHandler in telegram_control_bot.py), so the old
' `cmd /S /c ... >> log 2>&1` wrapper, which spawned the visible window, is
' gone for good.
Set WshEnv = WshShell.Environment("PROCESS")
WshEnv("PYTHONPATH") = RootDir & "\.venv\Lib\site-packages;" & RootDir

pythonwPath = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
scriptPath = RootDir & "\ai_assistant\telegram_control_bot.py"

WshShell.CurrentDirectory = RootDir

' -u keeps stdout/stderr unbuffered for the StreamHandler, harmless under
' pythonw. Window style 0 = hidden, bWaitOnReturn True so the scheduler sees
' the real exit code: exit 3 means "duplicate poller holds the lock" (healthy
' skip, not a crash); the hourly trigger + IgnoreNew restarts only dead bots.
rc = WshShell.Run("""" & pythonwPath & """ -u """ & scriptPath & """", 0, True)
WScript.Quit(rc)
