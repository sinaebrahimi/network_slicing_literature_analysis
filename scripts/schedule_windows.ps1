# Register a daily Windows scheduled task that runs scripts\run_alerts.ps1.
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Time 07:40
# Remove it again with:  Unregister-ScheduledTask -TaskName litsuite-alerts
param([string]$Time = "07:40", [string]$TaskName = "litsuite-alerts")
$script = Join-Path $PSScriptRoot "run_alerts.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "litsuite: check email alerts and download relevant papers" -Force | Out-Null
Write-Host "Scheduled '$TaskName' daily at $Time. Logs: logs\alerts-task.log"
