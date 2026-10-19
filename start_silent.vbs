Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
RootDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set WshEnv = WshShell.Environment("PROCESS")
WshEnv("PYTHONPATH") = RootDir & "\.venv\Lib\site-packages;" & RootDir

pythonwPath = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"

WshShell.CurrentDirectory = RootDir
WshShell.Run """" & pythonwPath & """ -m ai_assistant.cli ui --port 8000", 0, False
