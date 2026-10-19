# Unattended alert run for Windows Task Scheduler.
#   powershell -ExecutionPolicy Bypass -File scripts\run_alerts.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run_alerts.ps1 -p network_slicing --mode report
# Scheduled runs cannot ask questions, so the default mode is 'auto'.
# Output is appended to logs\alerts-task.log.
Set-Location (Join-Path $PSScriptRoot "..")
if (Test-Path .\.venv\Scripts\Activate.ps1) { & .\.venv\Scripts\Activate.ps1 }
New-Item -ItemType Directory -Force -Path logs | Out-Null
$argsList = if ($args.Count -gt 0) { $args } else { @("--all-projects", "--mode", "auto") }
"=== $(Get-Date -Format o) litsuite alerts $argsList" | Out-File -Append -Encoding utf8 logs\alerts-task.log
litsuite alerts @argsList *>> logs\alerts-task.log
