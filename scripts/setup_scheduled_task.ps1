$taskName = 'JobSearch_Daily_Digest'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-ExecutionPolicy Bypass -File C:\Users\Misha\Documents\job-search\scripts\run_job_search_production.ps1' -WorkingDirectory 'C:\Users\Misha\Documents\job-search'
$trigger = New-ScheduledTaskTrigger -Daily -At '10:00AM'
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'Daily Canonical Job Search Telegram Digest Dispatcher (Stage 84)' -Force

