$taskName = 'JobSearch_Weekly_Metrics'
$projectRoot = 'C:\Users\Misha\Documents\job-search'
$scriptPath = Join-Path $projectRoot 'scripts\run_weekly_metrics.ps1'

# Action: run powershell.exe with executionpolicy bypass to invoke run_weekly_metrics.ps1
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`"" `
    -WorkingDirectory $projectRoot

# Trigger: Every Sunday morning at 10:00 AM
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 10:00AM

# Settings: Ignore new if already running, don't stop on battery, max execution 15 minutes
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

# Register or update task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Job Search Weekly Operational Metrics Digest (@remotejobd, every Sunday 10:00 AM)' `
    -Force

Write-Host "Task $taskName successfully registered to run every Sunday at 10:00 AM!" -ForegroundColor Green
