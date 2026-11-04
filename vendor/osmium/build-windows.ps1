# Сборка бинарника osmium-tool для Windows x86_64.
#
# Нужны: git, Visual Studio 2022 (с C++), Python 3, cmake.
# Скрипт поднимает vcpkg, ставит зависимости статически и собирает
# osmium-tool 1.19.0 вместе с libosmium 2.20.0 (только заголовки).
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File vendor\osmium\build-windows.ps1
#
$ErrorActionPreference = "Stop"

$OsmiumToolVersion = "1.19.0"
$LibosmiumVersion = "2.20.0"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$DestDir = if ($env:DEST_DIR) { $env:DEST_DIR } else { Join-Path $ScriptDir "win-x86_64" }
$WorkDir = if ($env:WORK_DIR) { $env:WORK_DIR } else { Join-Path $env:TEMP "osmium-tool-build" }
$VcpkgDir = Join-Path $WorkDir "vcpkg"
$Triplet = "x64-windows-static"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Write-Host "==> Рабочий каталог: $WorkDir"

if (-Not (Test-Path (Join-Path $VcpkgDir ".git"))) {
    Write-Host "==> Клонируем vcpkg"
    git clone --depth 1 https://github.com/microsoft/vcpkg.git $VcpkgDir
}
Write-Host "==> Устанавливаем vcpkg"
& (Join-Path $VcpkgDir "bootstrap-vcpkg.bat") -disableMetrics

Write-Host "==> Ставим зависимости"
# vcpkg-boost — meta-пакет с файлами find_package(Boost).  Без него
# CMake ищет BoostConfig.cmake в дереве x64-windows, а собираем мы
# статическую сборку из x64-windows-static, и поиск не проходит.
$packages = @(
    "boost-program-options",
    "vcpkg-boost",
    "bzip2",
    "expat",
    "lz4",
    "nlohmann-json",
    "protozero",
    "zlib"
)
& (Join-Path $VcpkgDir "vcpkg.exe") install `
    ($packages | ForEach-Object { "$_`:$Triplet" }) `
    --triplet $Triplet

Write-Host "==> Скачиваем исходники"
$osmiumTarball = Join-Path $WorkDir "osmium-tool-$OsmiumToolVersion.tar.gz"
$libosmiumTarball = Join-Path $WorkDir "libosmium-$LibosmiumVersion.tar.gz"
if (-Not (Test-Path $osmiumTarball)) {
    Invoke-WebRequest -Uri "https://github.com/osmcode/osmium-tool/archive/refs/tags/v$OsmiumToolVersion.tar.gz" -OutFile $osmiumTarball
}
if (-Not (Test-Path $libosmiumTarball)) {
    Invoke-WebRequest -Uri "https://github.com/osmcode/libosmium/archive/refs/tags/v$LibosmiumVersion.tar.gz" -OutFile $libosmiumTarball
}
Push-Location $WorkDir
tar xzf $osmiumTarball
tar xzf $libosmiumTarball
Pop-Location

Write-Host "==> Конфигурируем"
$buildDir = Join-Path $WorkDir "build"
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
cmake -S (Join-Path $WorkDir "osmium-tool-$OsmiumToolVersion") -B $buildDir `
    -DCMAKE_BUILD_TYPE=Release `
    -DBUILD_SHARED_LIBS=OFF `
    -DBUILD_TESTING=OFF `
    -DCMAKE_TOOLCHAIN_FILE="$VcpkgDir\scripts\buildsystems\vcpkg.cmake" `
    -DVCPKG_TARGET_TRIPLET=$Triplet `
    -DBoost_DIR="$VcpkgDir\installed\$Triplet\share\boost" `
    -DOSMIUM_INCLUDE_DIR="$WorkDir\libosmium-$LibosmiumVersion\include" `
    -DCMAKE_INSTALL_PREFIX="$WorkDir\install"

Write-Host "==> Собираем"
cmake --build $buildDir --config Release

Write-Host "==> Устанавливаем бинарник"
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
$exe = Get-ChildItem -Path $buildDir -Recurse -Filter "osmium.exe" |
    Where-Object { $_.FullName -match "Release" } |
    Select-Object -First 1
if (-Not $exe) {
    $exe = Get-ChildItem -Path $buildDir -Recurse -Filter "osmium.exe" | Select-Object -First 1
}
if (-Not $exe) {
    throw "osmium.exe не найден в $buildDir"
}
Copy-Item $exe.FullName (Join-Path $DestDir "osmium.exe") -Force

Write-Host "==> Проверка"
& (Join-Path $DestDir "osmium.exe") --version
Write-Host "==> Готово: $DestDir\osmium.exe"
