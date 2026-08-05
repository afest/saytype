# Проверка собранного saytype на чистой Windows (Windows Sandbox).
#
# Запускается автоматически как LogonCommand из saytype.wsb. Внутри песочницы
# нет ни Python, ни CUDA, ни Visual C++ Redistributable — ровно то, что нужно
# проверить. Отчёт и логи складываются в проброшенную папку C:\out.
#
# Что проверяется:
#   1. exe стартует и не умирает за первые полторы минуты;
#   2. окно создаётся (значит Qt-плагины на месте);
#   3. модель скачивается и грузится на CPU — в логе есть `cpu/int8`;
#   4. в логе нет жалоб на CUDA (на машине без NVIDIA их быть не должно);
#   5. _crash.log пуст.
#
# Диктовку голосом отсюда не проверить: в песочнице микрофон отдаёт тишину.
# Здесь проверяется, что тракт поднимается, а живой прогон делает человек.

$ErrorActionPreference = "Continue"
$out = "C:\out"
$app = "C:\saytype\saytype.exe"
$report = "$out\report.txt"
$stderr = "$out\stderr.log"

function Say($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -Path $report -Value $line -Encoding utf8
}

Set-Content -Path $report -Value "=== saytype: проверка на чистой Windows ===" -Encoding utf8
Say "ОС: $((Get-CimInstance Win32_OperatingSystem).Caption) $((Get-CimInstance Win32_OperatingSystem).Version)"

$py = Get-Command python.exe -ErrorAction SilentlyContinue
Say ("Python в PATH: " + $(if ($py) { "ЕСТЬ — $($py.Source) (песочница не чистая!)" } else { "нет (ожидаемо)" }))
Say ("nvidia-smi: " + $(if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { "есть" } else { "нет (ожидаемо)" }))
$vcr = Test-Path "C:\Windows\System32\vcruntime140.dll"
Say "vcruntime140.dll в System32: $vcr"

Say "Запускаю $app"
$proc = Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/c", "`"$app`" 2> `"$stderr`"" `
    -PassThru -WindowStyle Hidden

# Первый запуск качает модель (base ~145 МБ) — даём время сети.
$deadline = (Get-Date).AddMinutes(6)
$loaded = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    $alive = Get-Process -Name "saytype" -ErrorAction SilentlyContinue
    if (-not $alive) { Say "ПРОЦЕСС УМЕР"; break }
    if (Test-Path $stderr) {
        $log = Get-Content $stderr -Raw -ErrorAction SilentlyContinue
        if ($log -match "model loaded: .*(cpu|cuda)/") {
            Say "Модель загрузилась: $($Matches[0])"
            $loaded = $true
            break
        }
    }
}

$alive = Get-Process -Name "saytype" -ErrorAction SilentlyContinue
Say ("Процесс жив: " + [bool]$alive)
if ($alive) {
    $wins = @($alive | Where-Object { $_.MainWindowHandle -ne 0 })
    Say ("Окно создано: " + [bool]$wins.Count)
}
Say "Модель загрузилась: $loaded"

if (Test-Path $stderr) {
    $log = Get-Content $stderr -Raw
    $cudaComplaints = ([regex]::Matches($log, "(?i)cuda|cublas|cudnn")).Count
    Say "Упоминаний CUDA в логе: $cudaComplaints (ожидаем строку «CUDA-рантайм не найден», не ошибки)"
}

$crash = "$env:LOCALAPPDATA\saytype\runtime\_crash.log"
if (Test-Path $crash) {
    Say "ЕСТЬ _crash.log:"
    Copy-Item $crash "$out\_crash.log" -Force
    Add-Content -Path $report -Value (Get-Content $crash -Raw) -Encoding utf8
} else {
    Say "_crash.log отсутствует — аварий не было"
}

$profileDir = "$env:LOCALAPPDATA\saytype"
if (Test-Path $profileDir) {
    Say "Профиль создан: $profileDir"
    Get-ChildItem $profileDir | ForEach-Object { Say "  $($_.Name)" }
}

Say "=== Готово. Отчёт: $report ==="
Start-Process notepad.exe $report
