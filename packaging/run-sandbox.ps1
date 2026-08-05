# Запустить собранное приложение в Windows Sandbox — чистой Windows без Python,
# CUDA и Visual C++ Redistributable.
#
#     powershell -ExecutionPolicy Bypass -File packaging\run-sandbox.ps1
#
# Конфиг песочницы генерируется здесь, а не лежит в репозитории готовым: .wsb
# требует абсолютных путей, и зашитый `C:\Users\<кто-то>\...` сломался бы у
# всех остальных (заодно это чужой путь в публичном репозитории).
#
# Требует включённой фичи (один раз, с правами администратора, + перезагрузка):
#     Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $repo "dist\saytype"
$out = Join-Path $repo "dist\sandbox-out"
$packaging = $PSScriptRoot

if (-not (Test-Path (Join-Path $dist "saytype.exe"))) {
    throw "Нет сборки в $dist — сначала `pyinstaller saytype.spec`"
}
if (-not (Test-Path "C:\Windows\System32\WindowsSandbox.exe")) {
    throw "Windows Sandbox не установлен. Включите фичу Containers-DisposableClientVM (нужна перезагрузка)."
}
New-Item -ItemType Directory -Force -Path $out | Out-Null

$wsb = Join-Path $env:TEMP "saytype-sandbox.wsb"
@"
<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Default</Networking>
  <AudioInput>Enable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <ProtectedClient>Disable</ProtectedClient>
  <MemoryInMB>8192</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$dist</HostFolder>
      <SandboxFolder>C:\saytype</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$out</HostFolder>
      <SandboxFolder>C:\out</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$packaging</HostFolder>
      <SandboxFolder>C:\packaging</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -ExecutionPolicy Bypass -NoProfile -File C:\packaging\sandbox-check.ps1</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Path $wsb -Encoding utf8

Write-Output "Конфиг: $wsb"
Write-Output "Отчёт появится в: $out\report.txt"
Start-Process -FilePath "C:\Windows\System32\WindowsSandbox.exe" -ArgumentList $wsb
