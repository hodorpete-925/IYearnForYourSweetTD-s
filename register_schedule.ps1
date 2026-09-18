# register_schedule.ps1 — registers the two refresh jobs in Windows Task Scheduler.
# Run once, from the repo folder, in PowerShell. Re-running replaces the tasks.
# Remove with:  Unregister-ScheduledTask -TaskName "IYearn*" -Confirm:$false
#
#   IYearn daily refresh : every day 7:00 AM   -> refresh_all.py --yahoo --unattended
#   IYearn game scores   : hourly in game windows -> publish_scores.py --pull
#       Thu 8 PM-midnight, Sun noon-midnight, Mon 8 PM-midnight (Eastern)
#
# The PC must be on (wake timers are requested, sleep usually honors them;
# a powered-off PC runs the missed job at next start).

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$py   = Join-Path $repo "venv\Scripts\pythonw.exe"
$set  = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew

$daily = New-ScheduledTaskAction -Execute $py -Argument 'refresh_all.py --yahoo --unattended "Scheduled morning refresh"' -WorkingDirectory $repo
Register-ScheduledTask -TaskName "IYearn daily refresh" -Action $daily -Settings $set -Force `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 7:00AM) | Out-Null

$scores = New-ScheduledTaskAction -Execute $py -Argument 'publish_scores.py --pull' -WorkingDirectory $repo
function HourlyWindow($day, $start, $hours) {
  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $day -At $start
  $r = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Hours $hours)
  $t.Repetition = $r.Repetition
  return $t
}
$triggers = @(
  (HourlyWindow Thursday "8:00PM" 4),
  (HourlyWindow Sunday   "12:00PM" 12),
  (HourlyWindow Monday   "8:00PM" 4)
)
Register-ScheduledTask -TaskName "IYearn game scores" -Action $scores -Settings $set -Trigger $triggers -Force | Out-Null

Get-ScheduledTask -TaskName "IYearn*" | Get-ScheduledTaskInfo | Format-Table TaskName, NextRunTime
