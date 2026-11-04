# Сборка инсталлятора QGIS-xtopo для Windows.
#
# Скрипт PowerShell — вариант installer/build-installer.sh для Windows
# (используется в CI, .github/workflows/build-installer.yml).
#
# Требуется Inno Setup 6 (компилятор ISCC.exe в PATH).

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Payload = Join-Path $PSScriptRoot "payload"

Push-Location $Root
try {
    Write-Host "== Сборка плагина =="
    python tools/build_plugin.py
    if ($LASTEXITCODE -ne 0) { throw "Сборка плагина не удалась" }

    $version = (Select-String -Path (Join-Path $Root "metadata.txt") -Pattern "^version=").Line -replace "^version=", ""
    if (-not $version) { throw "metadata.txt: не найдена строка version=" }
    $archive = Join-Path $Dist "qgis_xtopo-$version.zip"
    if (-not (Test-Path $archive)) { throw "Не найден архив $archive" }

    Write-Host "== Подготовка payload =="
    if (Test-Path $Payload) { Remove-Item -Recurse -Force $Payload }
    New-Item -ItemType Directory -Path $Payload | Out-Null
    Expand-Archive -Path $archive -DestinationPath $Payload -Force
    New-Item -ItemType Directory -Path (Join-Path $Payload "scripts") -Force | Out-Null
    Copy-Item (Join-Path $PSScriptRoot "scripts/install-qgis.ps1") `
        (Join-Path $Payload "scripts/install-qgis.ps1") -Force

    Write-Host "== Компиляция инсталлятора =="
    $iscc = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )
        foreach ($candidate in $candidates) {
            if (Test-Path $candidate) { $iscc = $candidate; break }
        }
    }
    if (-not $iscc) {
        throw "Компилятор Inno Setup не найден (ISCC.exe). Установите Inno Setup 6."
    }
    Write-Host "Компилятор: $iscc"

    & $iscc (Join-Path $PSScriptRoot "qgis-xtopo.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup вернул код $LASTEXITCODE" }

    $setup = Join-Path $Dist "qgis-xtopo-setup-$version.exe"
    if (-not (Test-Path $setup)) { throw "Инсталлятор не собран: $setup" }
    Write-Host "== Готово =="
    Get-Item $setup | Select-Object FullName, Length
}
finally {
    Pop-Location
}
