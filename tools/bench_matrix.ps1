# T-487: парная матрица legacy ↔ v5 чередующимися блоками (ABAB), чтобы порядок
# прогонов, прогрев и чужая нагрузка не ложились на одну сторону сравнения.
# Во время матрицы на машине не должно идти ничего тяжёлого (сборка, рендер, игры).
#   .\tools\bench_matrix.ps1                       # CUDA, large-v3-turbo, batch short+long, окно видно/скрыто, 2×5 повторов на сторону
#   .\tools\bench_matrix.ps1 -Streaming            # + streaming long (CUDA, окно видно), 2×5
#   .\tools\bench_matrix.ps1 -Cpu -Model small     # CPU на small
#   .\tools\bench_matrix.ps1 -Modes streaming -Samples long -Visible true   # только потоковый режим
param(
    [switch]$Cpu,
    [switch]$Offscreen,
    [string]$Model = 'large-v3-turbo',
    [int]$Blocks = 2,
    [int]$RepeatsPerBlock = 5,
    [switch]$Streaming,
    [string[]]$Modes = @('batch'),
    [string[]]$Samples = @('short', 'long'),
    [string[]]$Visible = @('true', 'false'),
    [string]$Out = '_dev/bench/results'
)
$root = Split-Path -Parent $PSScriptRoot
$run = Join-Path $root 'tools\bench_run.ps1'
for ($b = 1; $b -le $Blocks; $b++) {
    foreach ($ui in @('legacy', 'v5')) {
        Write-Output "=== блок $b/$Blocks · $ui · $($Modes -join ',') ==="
        $common = @{ UI = $ui; Model = $Model; Repeats = $RepeatsPerBlock; Warmup = 1; Out = $Out; IdleSec = 10 }
        if ($Cpu) { $common.Cpu = $true }
        if ($Offscreen) { $common.Offscreen = $true }
        & $run @common -Samples $Samples -Visible $Visible -Modes $Modes
        if ($Streaming) {
            Write-Output "=== блок $b/$Blocks · $ui · streaming long ==="
            & $run @common -Samples long -Visible true -Modes streaming
        }
    }
}
Write-Output "=== сводка ==="
& (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe') (Join-Path $root 'tools\bench_report.py') $Out --md (Join-Path $Out 'summary.md')
