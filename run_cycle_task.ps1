# Wrapper invoked by Windows Task Scheduler task "AHSnipePipeline" (hourly).
# Runs run_cycle.py and appends its output to data/logs/run_cycle_task.log,
# since a scheduled task has no console to print to.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "run_cycle_task.log"

$output = & python (Join-Path $root "run_cycle.py") --sell 1403 --names 2>&1 | Out-String
"=== $(Get-Date -Format o) ===`n$output" | Out-File -FilePath $log -Append -Encoding utf8
