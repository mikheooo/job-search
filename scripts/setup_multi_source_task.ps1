# Registers Windows Scheduled Task for silent multi-source vacancy discovery
$taskName = "JobSearchMultiSourceDiscovery"
$projectRoot = "C:\Users\Misha\Documents\job-search"
$vbsPath = Join-Path $projectRoot "scripts\run_multi_source_silent.vbs"

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 2)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Fetches fresh remote IT vacancies from Telegram, Hirify, and Habr Career quietly in background." `
    -Force

Write-Host "Task '$taskName' registered successfully (every 2 hours, 100% silent)."
