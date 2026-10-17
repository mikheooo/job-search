$taskName = 'JobSearch_Gig_Autopilot'
$projectRoot = 'C:\Users\Misha\Documents\job-search'
$vbsPath = Join-Path $projectRoot 'scripts\run_gig_autopilot_silent.vbs'

# Action: run wscript.exe scripts\run_gig_autopilot_silent.vbs (100% silent, no terminal popup)
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot

# Trigger: Start now, repeat every 2 hours indefinitely
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 2)

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
    -Description 'Autonomous Freelance & Gig Autopilot Engine (Freelancer, Kwork, Telegram, silent background execution every 2 hours)' `
    -Force

Write-Host "Task $taskName successfully registered to run silently every 2 hours!" -ForegroundColor Green
