# Сборка бинарника osmium-tool для Windows x86_64.
#
# Нужны: git, Visual Studio 2022 (с C++), Python 3, cmake.
# Зависимости ставит vcpkg, osmium-tool собирается из исходников.
#
# Рецепт повторяет CI самого osmium-tool
# (.github/workflows/ci.yml): триплет x64-windows, protozero и libosmium
# из исходников, CMake с указанием каталогов вручную.
#
# Использование:
#   powershell -ExecutionPolicy Bypass -File vendor\osmium\build-windows.ps1
#
$ErrorActionPreference = "Stop"

$OsmiumToolVersion = "1.19.0"
$LibosmiumVersion = "2.20.0"
$ProtozeroVersion = "v1.8.2"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$DestDir = if ($env:DEST_DIR) { $env:DEST_DIR } else { Join-Path $ScriptDir "win-x86_64" }
$WorkDir = if ($env:WORK_DIR) { $env:WORK_DIR } else { Join-Path $env:TEMP "osmium-tool-build" }
$VcpkgDir = Join-Path $WorkDir "vcpkg"
# Триплет как в CI osmium-tool: динамическая сборка, все зависимости
# (включая boost) попадают в одно дерево vcpkg.
$Triplet = "x64-windows"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Write-Host "==> Рабочий каталог: $WorkDir"

if (-Not (Test-Path (Join-Path $VcpkgDir ".git"))) {
    Write-Host "==> Клонируем vcpkg"
    git clone --depth 1 https://github.com/microsoft/vcpkg.git $VcpkgDir
}
Write-Host "==> Устанавливаем vcpkg"
& (Join-Path $VcpkgDir "bootstrap-vcpkg.bat") -disableMetrics

Write-Host "==> Ставим зависимости"
$packages = @(
    "boost-program-options",
    "bzip2",
    "expat",
    "lz4",
    "nlohmann-json",
    "zlib"
)
& (Join-Path $VcpkgDir "vcpkg.exe") install `
    ($packages | ForEach-Object { "$_`:$Triplet" }) `
    --triplet $Triplet

$Installed = Join-Path $VcpkgDir "installed\$Triplet"

function Find-VcpkgLib {
    <#  Ищет библиотеку в дереве vcpkg. Имена у портов разные:
        zlib даёт z.lib, bzip2 — bzip2.lib. #>
    param([string[]]$Names)
    $libDir = Join-Path $Installed "lib"
    foreach ($name in $Names) {
        $hit = Get-ChildItem -Path $libDir -Filter "$name.lib" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

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

Write-Host "==> Клонируем protozero"
$protozeroDir = Join-Path $WorkDir "protozero"
if (-Not (Test-Path (Join-Path $protozeroDir ".git"))) {
    git clone --quiet --depth 1 --branch $ProtozeroVersion `
        https://github.com/mapbox/protozero.git $protozeroDir
}

# Пути к зависимостям задаются явно: так не зависит от того, как
# CMake ищет библиотеки в дереве vcpkg.
$zlib = Find-VcpkgLib @("zlib", "z")
$expat = Find-VcpkgLib @("expat")
$bzip2 = Find-VcpkgLib @("bzip2")
$lz4 = Find-VcpkgLib @("lz4")
foreach ($pair in @(
    @("zlib", $zlib), @("expat", $expat),
    @("bzip2", $bzip2), @("lz4", $lz4))) {
    if (-Not $pair[1]) { throw "Библиотека $($pair[0]) не найдена в $Installed\lib" }
    Write-Host "==> $($pair[0]): $($pair[1])"
}

$includeDir = Join-Path $Installed "include"
foreach ($header in @("zlib.h", "expat.h", "bzlib.h", "lz4.h")) {
    if (-Not (Test-Path (Join-Path $includeDir $header))) {
        throw "Заголовок $header не найден в $includeDir"
    }
}

Write-Host "==> Конфигурируем"
$buildDir = Join-Path $WorkDir "build"
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }
cmake -S (Join-Path $WorkDir "osmium-tool-$OsmiumToolVersion") -B $buildDir `
    -DCMAKE_BUILD_TYPE=Release `
    -DBUILD_SHARED_LIBS=OFF `
    -DBUILD_TESTING=OFF `
    -DCMAKE_TOOLCHAIN_FILE="$VcpkgDir\scripts\buildsystems\vcpkg.cmake" `
    -DVCPKG_TARGET_TRIPLET=$Triplet `
    -DBoost_DIR="$Installed\share\boost" `
    -DOSMIUM_INCLUDE_DIR="$WorkDir\libosmium-$LibosmiumVersion\include" `
    -DPROTOZERO_INCLUDE_DIR="$protozeroDir\include" `
    -DZLIB_INCLUDE_DIR="$includeDir" `
    -DZLIB_LIBRARY_RELEASE="$zlib" `
    -DEXPAT_INCLUDE_DIR="$includeDir" `
    -DEXPAT_LIBRARY_RELEASE="$expat" `
    -DBZIP2_INCLUDE_DIR="$includeDir" `
    -DBZIP2_LIBRARIES_RELEASE="$bzip2" `
    -DLZ4_INCLUDE_DIR="$includeDir" `
    -DLZ4_LIBRARY="$lz4" `
    -DCMAKE_INSTALL_PREFIX="$WorkDir\install"
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Состояние CMake (диагностика)"
    $cache = Join-Path $buildDir "CMakeCache.txt"
    if (Test-Path $cache) {
        Get-Content $cache | Select-String -Pattern "VCPKG_TARGET_TRIPLET|VCPKG_INSTALLED_DIR|CMAKE_PREFIX_PATH|ZLIB|EXPAT|BZIP2|PROTOZERO|LZ4|Boost"
    }
    Write-Host "==> Содержимое дерева vcpkg"
    Get-ChildItem -Path $Installed -Recurse -Depth 2 -ErrorAction SilentlyContinue |
        Select-Object -First 60 -ExpandProperty FullName
    throw "cmake вернул код $LASTEXITCODE"
}

Write-Host "==> Собираем"
cmake --build $buildDir --config Release
if ($LASTEXITCODE -ne 0) { throw "Сборка не удалась" }

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
