# Registers Windows Scheduled Task for silent HeadHunter Realtime Message Watcher Daemon
$taskName = "JobSearch_HH_Realtime_Daemon"
$projectRoot = "C:\Users\Misha\Documents\job-search"
$vbsPath = Join-Path $projectRoot "scripts\run_hh_realtime_daemon_silent.vbs"

# 1. Disable legacy 15-minute monitor task if present (to avoid collisions)
$legacyTaskName = "JobSearch_HH_Message_Monitor"
$legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
if ($legacyTask) {
    Disable-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Legacy task '$legacyTaskName' has been disabled." -ForegroundColor Yellow
}

# 2. Configure Realtime Daemon task
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot
# Hourly trigger with IgnoreNew acts as watchdog (restarts daemon if killed or after reboot)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Continuous HeadHunter Real-Time Message Watcher & Auto-Reply Daemon (30s polling cycle)." `
    -Force | Out-Null

# 3. Start the daemon process right now via wscript
Start-Process "wscript.exe" -ArgumentList "`"$vbsPath`"" -WorkingDirectory $projectRoot
Write-Host "Task '$taskName' successfully registered and background daemon started!" -ForegroundColor Green
