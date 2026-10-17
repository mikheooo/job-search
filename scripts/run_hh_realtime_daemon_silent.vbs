Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
ScriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
RootDir = fso.GetParentFolderName(ScriptDir)

pythonwPath = RootDir & "\.venv\Scripts\pythonw.exe"
scriptPath = RootDir & "\scripts\run_hh_realtime_daemon.py"

WshShell.CurrentDirectory = RootDir

' Window style 0 = hidden, bWaitOnReturn True so caller sees exit code
rc = WshShell.Run("""" & pythonwPath & """ -u """ & scriptPath & """", 0, True)
WScript.Quit(rc)
