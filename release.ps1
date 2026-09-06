# Выпуск версии одной командой: PyInstaller → Velopack → фид обновлений.
#
#     powershell -ExecutionPolicy Bypass -File release.ps1 -Version 0.1.0
#     powershell -ExecutionPolicy Bypass -File release.ps1 -Version 0.1.1 -Github
#
# Без -Github пакеты складываются в локальную папку `Releases` — это полноценный
# фид: приложение умеет брать обновления из папки так же, как из сети. Так цикл
# обновления проверяется целиком до того, как заведён публичный репозиторий.
#
# Перед выпуском — секция `## <версия>` в CHANGELOG.md: скрипт берёт из неё
# заметки релиза и без неё не стартует.
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

    [string]$RepoUrl = "https://github.com/afest/saytype",

    # Пропустить сборку PyInstaller, если dist уже актуален
    [switch]$SkipBuild,

    # Имя пакета. Меняется только ради проверки цикла обновления: тестовая
    # сборка ставится рядом (`%LocalAppData%\<packId>`) и не трогает боевую
    # установку, а профиль пользователя у них общий — именно его и проверяем.
    [string]$PackId = "saytype-app"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if ($env:SAYTYPE_PYTHON) { $env:SAYTYPE_PYTHON } else { "python" }
$vpk = Join-Path $env:USERPROFILE ".dotnet\tools\vpk.exe"
if (-not (Test-Path $vpk)) {
    throw "vpk не найден ($vpk). Поставьте: dotnet tool install -g vpk"
}

# --- Заметки релиза (T-328) ---
# Это текст, который человек прочитает в окне «Доступно обновление», решая,
# обновляться сейчас или потом. Источник — секция `## <версия>` в CHANGELOG.md.
#
# Проверка стоит здесь, до сборки, и роняет выпуск, а не предупреждает. Файл без
# правила записи умирает: три CHANGELOG.md в соседних проектах доказали это тем,
# что последняя строка в них написана в день создания. Двадцать минут PyInstaller
# ради обещания «допишу потом» — цена, которую выпускающий платит один раз.
$changelogPath = "CHANGELOG.md"
if (-not (Test-Path $changelogPath)) {
    throw "Нет $changelogPath — заметки релиза брать неоткуда."
}
$changelog = [System.IO.File]::ReadAllText((Resolve-Path $changelogPath).Path)
$section = [regex]::Match($changelog, "(?ms)^##\s+$([regex]::Escape($Version))\b.*?(?=^##\s|\z)")
if (-not $section.Success) {
    throw "В $changelogPath нет секции '## $Version'. Заметки пишутся ДО выпуска: допишите 3-5 строк о том, что изменилось для человека, и запустите снова."
}
$notes = (($section.Value -split "`r?`n" | Select-Object -Skip 1) -join "`n").Trim()
if (-not $notes) {
    throw "Секция '## $Version' в $changelogPath пустая — в окно обновления писать нечего."
}
$notesPath = Join-Path ([System.IO.Path]::GetTempPath()) "saytype-notes-$Version.md"
[System.IO.File]::WriteAllText($notesPath, $notes, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "заметки релиза: $(($notes -split "`n").Count) строк из $changelogPath"

# --- Версия в трёх местах должна быть одна ---
# `__version__` в пакете — то, что приложение покажет в настройках; --packVersion —
# то, по чему апдейтер сравнивает релизы. Разъедутся — пользователь увидит одно,
# а обновляться будет по другому.
$initPath = "src\saytype\__init__.py"
$init = Get-Content $initPath -Raw -Encoding utf8
$patched = [regex]::Replace($init, '__version__ = "[^"]*"', "__version__ = `"$Version`"")
if ($patched -ne $init) {
    [System.IO.File]::WriteAllText((Resolve-Path $initPath), $patched, (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "версия в $initPath → $Version"
}

# --- Сборка ---
# Два прохода, и это не перестраховка. Список сторонних лицензий считается по
# фактическому составу сборки (TOC PyInstaller), а сам список кладётся внутрь
# сборки. Значит, первый проход даёт состав, потом список пересчитывается, и
# если он изменился — нужен второй проход, иначе в поставке едет вчерашний
# список. Проверять надо архив, а не список зависимостей.
if (-not $SkipBuild) {
    # Codex Desktop добавляет в PATH собственные native tools (включая Poppler).
    # PyInstaller принимает найденные там DLL за зависимости приложения: так в
    # 0.3.2 попала чужая ICU 78, несовместимая с Qt, и Qt6Core падал с WinError 127.
    # Сборка не должна зависеть от оболочки, из которой запущен release.ps1.
    $buildPath = $env:PATH
    try {
        $env:PATH = (($buildPath -split ';') |
            Where-Object { $_ -and $_ -notmatch '(?i)[\\/]\.cache[\\/]codex-runtimes[\\/]' }) -join ';'

        Write-Output "== PyInstaller (проход 1) =="
        & $python -m PyInstaller --noconfirm saytype.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал (код $LASTEXITCODE)" }

        $licenseFile = "licenses\THIRD-PARTY-LICENSES.txt"
        $before = if (Test-Path $licenseFile) { (Get-FileHash $licenseFile).Hash } else { "" }
        Write-Output "== список сторонних лицензий по составу сборки =="
        & $python tools\collect_licenses.py
        if ($LASTEXITCODE -ne 0) { throw "collect_licenses упал (код $LASTEXITCODE)" }
        $after = (Get-FileHash $licenseFile).Hash

        if ($before -ne $after) {
            Write-Output "== список изменился → PyInstaller (проход 2) =="
            & $python -m PyInstaller --noconfirm saytype.spec
            if ($LASTEXITCODE -ne 0) { throw "PyInstaller упал (код $LASTEXITCODE)" }
        } else {
            Write-Output "   список не изменился, второй проход не нужен"
        }
    } finally {
        $env:PATH = $buildPath
    }
}
if (-not (Test-Path "dist\saytype\saytype.exe")) {
    throw "Нет dist\saytype\saytype.exe — сборка не состоялась"
}

# Упаковка и публикация запрещены, если frozen-приложение не может загрузить
# собственные зависимости. Исходниковые pytest этого класса ошибок не видят.
$selftest = "dist\saytype\saytype-selftest.exe"
if (-not (Test-Path $selftest)) {
    throw "Нет $selftest — самопроверка сборки не создана"
}
Write-Output "== self-test frozen-сборки =="
& $selftest
if ($LASTEXITCODE -ne 0) {
    throw "self-test frozen-сборки упал (код $LASTEXITCODE) — пакетировать релиз нельзя"
}

# --- Пакет Velopack ---
# Setup.exe ставит приложение в %LocalAppData%\<packId> и не спрашивает прав
# администратора.
#
# packId = `saytype-app`, а не `saytype`, хотя напрашивается второе. Velopack
# жёстко ставит приложение в папку по имени packId, а `%LocalAppData%\saytype`
# уже занят профилем пользователя (настройки, словарь, скачанные модели,
# CUDA-слой — см. profile.py). Совпади имена, установщик у любого, кто уже
# пользовался приложением, упирается в «папка существует, перезаписать?», а
# согласие стирает гигабайты скачанного. Плюс деинсталляция сносила бы папку
# установки вместе с данными. Отображаемое имя задаёт --packTitle, так что в
# меню и в «Установке и удалении программ» видно нормальное «SayType».
Write-Output "== vpk pack $Version =="
& $vpk pack `
    --packId $PackId `
    --packVersion $Version `
    --packDir "dist\saytype" `
    --mainExe "saytype.exe" `
    --packTitle "SayType" `
    --packAuthors "SayType contributors" `
    --icon "assets\saytype.ico" `
    --releaseNotes $notesPath `
    --outputDir $OutputDir
if ($LASTEXITCODE -ne 0) { throw "vpk pack упал (код $LASTEXITCODE)" }

# --- Публикация ---
if ($Github) {
    Write-Output "== vpk upload github =="
    & $vpk upload github --repoUrl $RepoUrl --publish --releaseName "SayType $Version" --tag "v$Version" --outputDir $OutputDir
    if ($LASTEXITCODE -ne 0) { throw "vpk upload упал (код $LASTEXITCODE)" }
} else {
    Write-Output "== локальный фид: $((Resolve-Path $OutputDir).Path) =="
    Write-Output "   приложение возьмёт обновления отсюда, если задать переменную:"
    Write-Output "   `$env:SAYTYPE_UPDATE_FEED = '$((Resolve-Path $OutputDir).Path)'"
}

Get-ChildItem $OutputDir -File | Sort-Object Length -Descending |
    ForEach-Object { "{0,9:N1} MB  {1}" -f ($_.Length / 1MB), $_.Name }
