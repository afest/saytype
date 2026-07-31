# Выпуск версии одной командой: PyInstaller → Velopack → фид обновлений.
#
#     powershell -ExecutionPolicy Bypass -File release.ps1 -Version 0.1.0
#     powershell -ExecutionPolicy Bypass -File release.ps1 -Version 0.1.1 -Github
#
# Без -Github пакеты складываются в локальную папку `Releases` — это полноценный
# фид: приложение умеет брать обновления из папки так же, как из сети. Так цикл
# обновления проверяется целиком до того, как заведён публичный репозиторий.
#
# Что нужно один раз:
#     pip install --user -r requirements.txt pyinstaller velopack
#     dotnet tool install -g vpk        (SDK — per-user, см. docs/build.md)
#
# Версия пакета velopack и версия CLI vpk обязаны совпадать: расхождение даёт
# невнятные ошибки не на сборке, а на этапе применения обновления.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    # Куда складывать пакеты и откуда приложение будет их брать
    [string]$OutputDir = "Releases",

    # Публиковать в GitHub Releases (нужен gh/токен и заведённый remote)
    [switch]$Github,

    [string]$RepoUrl = "https://github.com/afest/iwhisper",

    # Пропустить сборку PyInstaller, если dist уже актуален
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if ($env:IWHISPER_PYTHON) { $env:IWHISPER_PYTHON } else { "python" }
$vpk = Join-Path $env:USERPROFILE ".dotnet\tools\vpk.exe"
if (-not (Test-Path $vpk)) {
    throw "vpk не найден ($vpk). Поставьте: dotnet tool install -g vpk"
}

# --- Версия в трёх местах должна быть одна ---
# `__version__` в пакете — то, что приложение покажет в настройках; --packVersion —
# то, по чему апдейтер сравнивает релизы. Разъедутся — пользователь увидит одно,
# а обновляться будет по другому.
$initPath = "src\iwhisper\__init__.py"
$init = Get-Content $initPath -Raw -Encoding utf8
$patched = [regex]::Replace($init, '__version__ = "[^"]*"', "__version__ = `"$Version`"")
if ($patched -ne $init) {
    [System.IO.File]::WriteAllText((Resolve-Path $initPath), $patched, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "версия в $initPath → $Version"
}

# --- Сборка ---
if (-not $SkipBuild) {
    Write-Output "== PyInstaller =="
    & $python -m PyInstaller --noconfirm iwhisper.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал (код $LASTEXITCODE)" }
}
if (-not (Test-Path "dist\iwhisper\iwhisper.exe")) {
    throw "Нет dist\iwhisper\iwhisper.exe — сборка не состоялась"
}

# --- Пакет Velopack ---
# Setup.exe ставит приложение в %LocalAppData%\<packId> и не спрашивает прав
# администратора.
#
# packId = `iwhisper-app`, а не `iwhisper`, хотя напрашивается второе. Velopack
# жёстко ставит приложение в папку по имени packId, а `%LocalAppData%\iwhisper`
# уже занят профилем пользователя (настройки, словарь, скачанные модели,
# CUDA-слой — см. profile.py). Совпади имена, установщик у любого, кто уже
# пользовался приложением, упирается в «папка существует, перезаписать?», а
# согласие стирает гигабайты скачанного. Плюс деинсталляция сносила бы папку
# установки вместе с данными. Отображаемое имя задаёт --packTitle, так что в
# меню и в «Установке и удалении программ» видно нормальное «iWhisper».
Write-Output "== vpk pack $Version =="
& $vpk pack `
    --packId iwhisper-app `
    --packVersion $Version `
    --packDir "dist\iwhisper" `
    --mainExe "iwhisper.exe" `
    --packTitle "iWhisper" `
    --packAuthors "iwhisper contributors" `
    --icon "assets\iwhisper.ico" `
    --outputDir $OutputDir
if ($LASTEXITCODE -ne 0) { throw "vpk pack упал (код $LASTEXITCODE)" }

# --- Публикация ---
if ($Github) {
    Write-Output "== vpk upload github =="
    & $vpk upload github --repoUrl $RepoUrl --publish --releaseName "iWhisper $Version" --tag "v$Version" --outputDir $OutputDir
    if ($LASTEXITCODE -ne 0) { throw "vpk upload упал (код $LASTEXITCODE)" }
} else {
    Write-Output "== локальный фид: $((Resolve-Path $OutputDir).Path) =="
    Write-Output "   приложение возьмёт обновления отсюда, если задать переменную:"
    Write-Output "   `$env:IWHISPER_UPDATE_FEED = '$((Resolve-Path $OutputDir).Path)'"
}

Get-ChildItem $OutputDir -File | Sort-Object Length -Descending |
    ForEach-Object { "{0,9:N1} MB  {1}" -f ($_.Length / 1MB), $_.Name }
