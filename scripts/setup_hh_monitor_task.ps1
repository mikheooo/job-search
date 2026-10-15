$taskName = 'JobSearch_HH_Message_Monitor'
$projectRoot = 'C:\Users\Misha\Documents\job-search'
$vbsPath = Join-Path $projectRoot 'scripts\run_hh_monitor_silent.vbs'

# Action: run wscript.exe scripts\run_hh_monitor_silent.vbs (100% silent, no terminal popup)
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot

# Trigger: Start now, repeat every 15 minutes indefinitely
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)

# Settings: Ignore new if already running, don't stop on battery, max execution 10 minutes
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Register or update task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Continuous HeadHunter message monitor with real-time Telegram alerts (Stage 33, silent background execution)' `
    -Force

Write-Host "Task $taskName successfully registered to run silently every 15 minutes!" -ForegroundColor Green
