$t = Get-ScheduledTask -TaskName 'JobSearch_Daily_Digest'
$info = Get-ScheduledTaskInfo -TaskName 'JobSearch_Daily_Digest'
[PSCustomObject]@{
    TaskName = $t.TaskName
    State = $t.State
    Enabled = $t.Settings.Enabled
    ActionExecute = $t.Actions.Execute
    ActionArgument = $t.Actions.Arguments
    WorkingDirectory = $t.Actions.WorkingDirectory
    MultipleInstancesPolicy = $t.Settings.MultipleInstances
    ExecutionTimeLimit = $t.Settings.ExecutionTimeLimit
    Trigger = $t.Triggers.StartBoundary
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $info.NextRunTime
} | Format-List

