$taskName = 'JobSearch_HH_Autopilot'
$projectRoot = 'C:\Users\Misha\Documents\job-search'
$vbsPath = Join-Path $projectRoot 'scripts\run_hh_autopilot_silent.vbs'

# Action: run wscript.exe scripts\run_hh_autopilot_silent.vbs (100% silent, no terminal popup)
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot

# Trigger: Start now, repeat every 4 hours indefinitely
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 4)

# Settings: Ignore new if already running, don't stop on battery, max execution 20 minutes
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Register or update task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Continuous HeadHunter Autonomous Application Engine (Autopilot, silent background execution)' `
    -Force

Write-Host "Task $taskName successfully registered to run silently every 4 hours!" -ForegroundColor Green
