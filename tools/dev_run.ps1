# T-487: запуск SayType из этого worktree рядом с рабочим приложением.
# Свой mutex, свой профиль (%LOCALAPPDATA%\saytype-dev), свой hotkey из его settings.ini.
#   .\tools\dev_run.ps1                 # новый интерфейс V5
#   .\tools\dev_run.ps1 -UI legacy      # прежнее окно
#   .\tools\dev_run.ps1 -Cpu            # без CUDA (CUDA_VISIBLE_DEVICES пустой)
#   .\tools\dev_run.ps1 -Bench <py>     # исполнить сценарий автоматизации после старта
#   .\tools\dev_run.ps1 -Console -Wait  # консольный python, дождаться выхода
#   .\tools\dev_run.ps1 -Offscreen      # окно рисуется в offscreen-платформе Qt: на экране ничего
#   .\tools\dev_run.ps1 -Exe            # собранный dist\saytype\saytype.exe с тем же тестовым профилем
param(
    [ValidateSet('v5', 'legacy')] [string]$UI = 'v5',
    [switch]$Cpu,
    [string]$Bench = '',
    [switch]$Console,
    [switch]$Wait,
    [switch]$Offscreen,
    [switch]$Exe,
    [string]$Profile = "$env:LOCALAPPDATA\saytype-dev"
)
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $env:LOCALAPPDATA ('Programs\Python\Python312\' + $(if ($Console) { 'python.exe' } else { 'pythonw.exe' }))
$env:PYTHONPATH = "$root\src"
$env:SAYTYPE_PROFILE_DIR = $Profile
$env:SAYTYPE_INSTANCE = 'dev'
$env:SAYTYPE_UI = $UI
# '-1', а не '': присваивание пустой строки в PowerShell удаляет переменную, и CUDA оставалась видна
if ($Cpu) { $env:CUDA_VISIBLE_DEVICES = '-1' } else { Remove-Item Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue }
if ($Offscreen) { $env:QT_QPA_PLATFORM = 'offscreen'; $env:QT_QPA_FONTDIR = 'C:/Windows/Fonts' } else { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue; Remove-Item Env:QT_QPA_FONTDIR -ErrorAction SilentlyContinue }
if ($Bench) { $env:SAYTYPE_BENCH = (Resolve-Path $Bench).Path } else { Remove-Item Env:SAYTYPE_BENCH -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force "$root\_dev" | Out-Null
$log = "$root\_dev\app-$UI.log"
# Dev-копия уже работает: новый процесс только попросит её показать окно и выйдет
# (src\saytype\single_instance.py). Логи и app.pid не трогаем — перенаправление
# обнулило бы лог живой копии, а dev_stop перестал бы её находить (05.10.2026).
$held = $null
if ([System.Threading.Mutex]::TryOpenExisting("faster-whisper-ui-singleton-2026-05-$($env:SAYTYPE_INSTANCE)", [ref]$held)) {
    $held.Dispose()
    $sp = @{ FilePath = $py; ArgumentList = @('-m', 'saytype'); WorkingDirectory = $root; PassThru = $true }
    if ($Exe) { $sp.FilePath = Join-Path $root 'dist\saytype\saytype.exe'; $sp.Remove('ArgumentList') }
    $p = Start-Process @sp
    Write-Output "dev-копия уже запущена — попросил её показать окно (pid=$($p.Id), app.pid не тронут)"
    if ($Wait) { $p.WaitForExit(); Write-Output "exited code=$($p.ExitCode)" }
    exit 0
}
if ($Exe) {
    $exePath = Join-Path $root 'dist\saytype\saytype.exe'
    if (-not (Test-Path $exePath)) { throw "нет сборки: $exePath (сначала tools\build_local.ps1)" }
    $p = Start-Process -FilePath $exePath -WorkingDirectory (Split-Path $exePath) -PassThru
} else {
    $p = Start-Process -FilePath $py -ArgumentList '-m', 'saytype' -WorkingDirectory $root -PassThru -RedirectStandardOutput $log -RedirectStandardError "$root\_dev\app-$UI.err.log"
}
$p.Id | Set-Content "$root\_dev\app.pid"
Write-Output "started pid=$($p.Id) ui=$UI cpu=$($Cpu.IsPresent) offscreen=$($Offscreen.IsPresent) log=$log"
if ($Wait) { $p.WaitForExit(); Write-Output "exited code=$($p.ExitCode)" }
