# T-487: локальная Windows-сборка ветки без упаковки и публикации.
# Повторяет сборочную часть release.ps1: PyInstaller → список лицензий по составу
# сборки → второй проход, если список изменился → self-test frozen-сборки.
# Результат: dist\saytype\saytype.exe (рядом saytype-selftest.exe).
#   .\tools\build_local.ps1
param(
    [string]$Python = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
)
# Не 'Stop': PyInstaller пишет INFO в stderr, и в PowerShell 5.1 каждая такая строка
# при перенаправлении вывода становилась бы ошибкой. Сбой определяем по $LASTEXITCODE.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Как в release.ps1: чужие native tools из PATH не должны попасть в сборку.
$buildPath = $env:PATH
try {
    $env:PATH = (($buildPath -split ';') |
        Where-Object { $_ -and $_ -notmatch '(?i)[\\/]\.cache[\\/]codex-runtimes[\\/]' }) -join ';'
    Write-Output "== PyInstaller (проход 1) =="
    & $Python -m PyInstaller --noconfirm saytype.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал (код $LASTEXITCODE)" }

    $licenseFile = "licenses\THIRD-PARTY-LICENSES.txt"
    $before = if (Test-Path $licenseFile) { (Get-FileHash $licenseFile).Hash } else { "" }
    Write-Output "== список сторонних лицензий по составу сборки =="
    & $Python tools\collect_licenses.py
    if ($LASTEXITCODE -ne 0) { throw "collect_licenses упал (код $LASTEXITCODE)" }
    $after = (Get-FileHash $licenseFile).Hash
    if ($before -ne $after) {
        Write-Output "== список изменился → PyInstaller (проход 2) =="
        & $Python -m PyInstaller --noconfirm saytype.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал (код $LASTEXITCODE)" }
    } else {
        Write-Output "   список не изменился, второй проход не нужен"
    }
} finally {
    $env:PATH = $buildPath
}

if (-not (Test-Path "dist\saytype\saytype.exe")) { throw "Нет dist\saytype\saytype.exe" }
Write-Output "== self-test frozen-сборки =="
& "dist\saytype\saytype-selftest.exe"
if ($LASTEXITCODE -ne 0) { throw "self-test упал (код $LASTEXITCODE)" }

Write-Output "== ассеты интерфейса и лицензии в сборке =="
$need = @(
    "dist\saytype\_internal\saytype\ui\assets\fonts\Unbounded.ttf",
    "dist\saytype\_internal\saytype\ui\assets\fonts\Unbounded-OFL.txt",
    "dist\saytype\_internal\saytype\ui\assets\mark.svg",
    "dist\saytype\_internal\licenses\THIRD-PARTY-LICENSES.txt"
)
$missing = $need | Where-Object { -not (Test-Path $_) }
if ($missing) { throw ("нет в сборке: " + ($missing -join ', ')) }
$need | ForEach-Object { Write-Output "  ok $_" }

Write-Output "== GPL-кода в поставке нет =="
$gpl = Get-ChildItem dist\saytype -Recurse -File | Where-Object { $_.FullName -match 'av\.libs|libx264|libx265' }
if ($gpl) { throw ("найдено: " + ($gpl.FullName -join ', ')) }
Write-Output "  ok"
$size = [math]::Round((Get-ChildItem dist\saytype -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Output "== готово: dist\saytype ($size МБ) =="
