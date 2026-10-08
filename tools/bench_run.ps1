# T-487: один прогон парного бенчмарка. Правит модель в dev-профиле, собирает конфиг
# и запускает приложение с SAYTYPE_BENCH=tools/bench_ui.py, дожидается выхода.
#   .\tools\bench_run.ps1 -UI legacy -Model large-v3-turbo
#   .\tools\bench_run.ps1 -UI v5 -Cpu -Model small -Repeats 10
#   .\tools\bench_run.ps1 -UI legacy -Modes streaming -Samples long -Visible true
param(
    [ValidateSet('v5', 'legacy')] [string]$UI = 'v5',
    [switch]$Cpu,
    [switch]$Offscreen,
    [string]$Model = 'large-v3-turbo',
    [int]$Repeats = 10,
    [int]$Warmup = 1,
    [string[]]$Modes = @('batch'),
    [string[]]$Samples = @('short', 'long'),
    [string[]]$Visible = @('true', 'false'),
    [double]$IdleSec = 10,
    [string]$Out = '_dev/bench/results',
    [string]$Profile = "$env:LOCALAPPDATA\saytype-dev"
)
$root = Split-Path -Parent $PSScriptRoot
$ini = Join-Path $Profile 'settings.ini'
$t = [System.IO.File]::ReadAllText($ini, [System.Text.Encoding]::UTF8)
$t = $t -replace '(?m)^model=.*$', "model=$Model"
[System.IO.File]::WriteAllText($ini, $t, (New-Object System.Text.UTF8Encoding($false)))

$sampleMap = @{}
foreach ($s in $Samples) { $sampleMap[$s] = "_dev/bench/$s.wav" }
$vis = @(); foreach ($v in $Visible) { $vis += [bool]::Parse($v) }
$cfg = @{ samples = $sampleMap; repeats = $Repeats; warmup = $Warmup; modes = $Modes; visible = $vis; idle_sec = $IdleSec; out = $Out } | ConvertTo-Json -Compress
$env:SAYTYPE_BENCH_CFG = $cfg
Write-Output "cfg: $cfg"
$devArgs = @{ UI = $UI; Console = $true; Wait = $true; Bench = 'tools/bench_ui.py'; Profile = $Profile }
if ($Cpu) { $devArgs.Cpu = $true }
if ($Offscreen) { $devArgs.Offscreen = $true }
& (Join-Path $root 'tools\dev_run.ps1') @devArgs
Remove-Item Env:SAYTYPE_BENCH_CFG -ErrorAction SilentlyContinue
