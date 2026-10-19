Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
RootDir = fso.GetParentFolderName(ScriptDir)

' Phase 5 windowless: pythonw.exe has NO console subsystem, so a scheduled run
' never flashes a cmd/conhost window. Logging moved into the script itself
' (FileHandler in run_multi_source_discovery.py), replacing the old
' `cmd /S /c ... >> log 2>&1` wrapper that created the window.
Set WshEnv = WshShell.Environment("PROCESS")
WshEnv("PYTHONPATH") = RootDir & "\.venv\Lib\site-packages;" & RootDir

pythonwPath = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
scriptPath = RootDir & "\scripts\run_multi_source_discovery.py"

WshShell.CurrentDirectory = RootDir

' Window style 0 = hidden, bWaitOnReturn True so the scheduler gets python's
' real exit code (WshShell.Run's return used to be discarded, leaving wscript
' always at 0 and the scheduler blind to failures). Short task + 15-min
' ExecutionTimeLimit: hangs surface as failures.
rc = WshShell.Run("""" & pythonwPath & """ """ & scriptPath & """", 0, True)
WScript.Quit(rc)
