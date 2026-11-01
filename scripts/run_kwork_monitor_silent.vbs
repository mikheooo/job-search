Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
RootDir = fso.GetParentFolderName(ScriptDir)

Set WshEnv = WshShell.Environment("PROCESS")
WshEnv("PYTHONPATH") = RootDir & "\.venv\Lib\site-packages;" & RootDir

pythonwPath = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
scriptPath = RootDir & "\scripts\run_kwork_inbox_monitor_cycle.py"

WshShell.CurrentDirectory = RootDir

' Run in live mode so new Kwork inbox messages are stored in kwork_inbox.db and sent to Telegram
rc = WshShell.Run("""" & pythonwPath & """ """ & scriptPath & """ --live", 0, True)
WScript.Quit(rc)
