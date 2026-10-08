# T-487: остановить dev-копию, запущенную dev_run.ps1 (по PID из _dev\app.pid).
$root = Split-Path -Parent $PSScriptRoot
$pidFile = "$root\_dev\app.pid"
if (-not (Test-Path $pidFile)) { Write-Output 'no pid file'; exit 0 }
$id = [int](Get-Content $pidFile)
$proc = Get-Process -Id $id -ErrorAction SilentlyContinue
if ($null -eq $proc) { Write-Output "pid $id not running"; exit 0 }
Stop-Process -Id $id
Write-Output "stopped pid=$id"
