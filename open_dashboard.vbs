Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
RootDir = fso.GetParentFolderName(WScript.ScriptFullName)

Function IsServerRunning()
    On Error Resume Next
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 500, 500, 500, 500
    http.open "GET", "http://127.0.0.1:8000/", False
    http.send
    If Err.Number = 0 Then
        If http.Status = 200 Then
            IsServerRunning = True
            Exit Function
        End If
    End If
    IsServerRunning = False
    On Error GoTo 0
End Function

If Not IsServerRunning() Then
    WshShell.Run "schtasks /Run /TN ""JobSearchDashboard""", 0, True
    For i = 1 To 12
        WScript.Sleep 500
        If IsServerRunning() Then Exit For
    Next
End If

If Not IsServerRunning() Then
    Set WshEnv = WshShell.Environment("PROCESS")
    WshEnv("PYTHONPATH") = RootDir & "\.venv\Lib\site-packages;" & RootDir
    pythonPath = "C:\Users\Misha\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\pythonw.exe"
    scriptPath = RootDir & "\scripts\launch_dashboard.py"
    WshShell.Run """" & pythonPath & """ """ & scriptPath & """", 0, False
    For i = 1 To 12
        WScript.Sleep 500
        If IsServerRunning() Then Exit For
    Next
End If

WshShell.Run "http://localhost:8000"
