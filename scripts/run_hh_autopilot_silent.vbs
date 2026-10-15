Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Misha\Documents\job-search"
WshShell.Run """C:\Users\Misha\Documents\job-search\.venv\Scripts\python.exe"" ""C:\Users\Misha\Documents\job-search\scripts\run_hh_autopilot_cycle.py""", 0, True
