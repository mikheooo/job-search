# Registers Windows Scheduled Task for silent Telegram Control Bot
$taskName = "JobSearchTelegramControlBot"
$projectRoot = "C:\Users\Misha\Documents\job-search"
$vbsPath = Join-Path $projectRoot "scripts\run_telegram_control_bot_silent.vbs"

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbsPath`"" -WorkingDirectory $projectRoot
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
    -Description "Interactive Telegram mobile control room for JobSearch (@jobsassistbot)." `
    -Force

# Also start the process right now via wscript
Start-Process "wscript.exe" -ArgumentList "`"$vbsPath`"" -WorkingDirectory $projectRoot
Write-Host "Task '$taskName' registered and background daemon started."
