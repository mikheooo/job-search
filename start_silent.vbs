Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Misha\Documents\job-search"
WshShell.Run """C:\Users\Misha\Documents\job-search\.venv\Scripts\pythonw.exe"" -m ai_assistant.cli ui --port 8000", 0, False
