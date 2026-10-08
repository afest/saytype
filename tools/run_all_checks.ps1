# T-487: всё длинное одной цепочкой, без вывода на экран.
# 1) локальная сборка exe + self-test; 2) CUDA обычный режим; 3) CUDA потоковый;
# 4) процессор (small). Состояние — в _dev\checks-status.txt (что идёт, что готово).
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$status = Join-Path $root '_dev\checks-status.txt'
function Mark($text) { "$(Get-Date -Format 'HH:mm:ss')  $text" | Add-Content -Path $status -Encoding UTF8 }
"T-487 · цепочка проверок · старт $(Get-Date -Format 'yyyy-MM-dd HH:mm')" | Set-Content -Path $status -Encoding UTF8

Mark "1/4 сборка exe — идёт"
try {
    & (Join-Path $root 'tools\build_local.ps1') *> (Join-Path $root '_dev\build.log')
    Mark "1/4 сборка exe — готово (лог _dev\build.log)"
} catch {
    Mark "1/4 сборка exe — СБОЙ: $($_.Exception.Message) (лог _dev\build.log)"
}

# на время замеров — без звуков: штатный стоп играет сигнал завершения
$ini = Join-Path $env:LOCALAPPDATA 'saytype-dev\settings.ini'
function Set-Sounds($on) {
    $v = if ($on) { 'true' } else { 'false' }
    $s = [System.IO.File]::ReadAllText($ini, [System.Text.Encoding]::UTF8)
    $s = $s -replace '(?m)^sound_notifications_dictation=.*$', "sound_notifications_dictation=$v"
    $s = $s -replace '(?m)^sound_notifications_call=.*$', "sound_notifications_call=$v"
    [System.IO.File]::WriteAllText($ini, $s, (New-Object System.Text.UTF8Encoding($false)))
}
Set-Sounds $false

$matrix = Join-Path $root 'tools\bench_matrix.ps1'
Mark "2/4 замер CUDA, обычный режим — идёт"
& $matrix -Offscreen -Model large-v3-turbo -Blocks 2 -RepeatsPerBlock 5 -Out _dev/bench/matrix-cuda *> (Join-Path $root '_dev\bench-cuda.log')
Mark "2/4 замер CUDA, обычный режим — готово"

Mark "3/4 замер CUDA, потоковый режим (длинная запись в реальном времени) — идёт"
& $matrix -Offscreen -Model large-v3-turbo -Blocks 2 -RepeatsPerBlock 5 -Modes streaming -Samples long -Visible true -Out _dev/bench/matrix-streaming *> (Join-Path $root '_dev\bench-streaming.log')
Mark "3/4 замер CUDA, потоковый режим — готово"

Mark "4/4 замер на процессоре (small) — идёт"
& $matrix -Offscreen -Cpu -Model small -Blocks 2 -RepeatsPerBlock 5 -Out _dev/bench/matrix-cpu *> (Join-Path $root '_dev\bench-cpu.log')
Mark "4/4 замер на процессоре — готово"

# тестовый профиль — обратно в рабочий вид для ручной проверки
Set-Sounds $true
$t = [System.IO.File]::ReadAllText($ini, [System.Text.Encoding]::UTF8)
$t = $t -replace '(?m)^model=.*$', 'model=large-v3-turbo'
[System.IO.File]::WriteAllText($ini, $t, (New-Object System.Text.UTF8Encoding($false)))
Mark "ВСЁ ГОТОВО"
