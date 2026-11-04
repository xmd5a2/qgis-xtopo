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
    <#  Имена файлов у портов разные: zlib даёт z.lib, у остальных
        своя схема.  Ищем по шаблонам, при неудаче печатаем содержимое
        каталога — так видно, что именно собрал vcpkg. #>
    param([string]$Label, [string[]]$Patterns)
    $libDir = Join-Path $Installed "lib"
    foreach ($pattern in $Patterns) {
        $hit = Get-ChildItem -Path $libDir -Filter $pattern -ErrorAction SilentlyContinue |
            Sort-Object Name | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    Write-Host "==> Содержимое $libDir :"
    Get-ChildItem -Path $libDir -Filter "*.lib" -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    $($_.Name)" }
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
$zlib = Find-VcpkgLib "zlib" @("z.lib", "zlib.lib")
$expat = Find-VcpkgLib "expat" @("expat*.lib", "libexpat*.lib")
$bzip2 = Find-VcpkgLib "bzip2" @("bzip2*.lib", "bz2*.lib", "libbz2*.lib")
$lz4 = Find-VcpkgLib "lz4" @("lz4*.lib", "liblz4*.lib")
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
$configureLog = Join-Path $WorkDir "cmake-configure.log"
cmake -S (Join-Path $WorkDir "osmium-tool-$OsmiumToolVersion") -B $buildDir `
    -DCMAKE_BUILD_TYPE=Release `
    -DBUILD_SHARED_LIBS=OFF `
    -DBUILD_TESTING=OFF `
    -DCMAKE_TOOLCHAIN_FILE="$VcpkgDir\scripts\buildsystems\vcpkg.cmake" `
    -DBoost_DIR="$Installed\share\boost" `
    -DOSMIUM_INCLUDE_DIR="$WorkDir\libosmium-$LibosmiumVersion\include" `
    -DPROTOZERO_INCLUDE_DIR="$protozeroDir\include" `
    -DZLIB_INCLUDE_DIR="$includeDir" `
    -DZLIB_LIBRARY_RELEASE="$zlib" `
    -DEXPAT_INCLUDE_DIR="$includeDir" `
    -DEXPAT_LIBRARY_RELEASE="$expat" `
    -DBZIP2_INCLUDE_DIR="$includeDir" `
    -DBZIP2_LIBRARIES="$bzip2" `
    -DBZIP2_LIBRARY_RELEASE="$bzip2" `
    -DLZ4_INCLUDE_DIR="$includeDir" `
    -DLZ4_LIBRARY="$lz4" `
    -DCMAKE_INSTALL_PREFIX="$WorkDir\install" *>&1 |
    Tee-Object -FilePath $configureLog
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> Последние строки конфигурации CMake:"
    Get-Content $configureLog -Tail 60
    Write-Host "==> Состояние CMake (диагностика)"
    $cache = Join-Path $buildDir "CMakeCache.txt"
    if (Test-Path $cache) {
        Write-Host "==> Переменные, которые не нашлись:"
        Get-Content $cache | Select-String "-NOTFOUND"
        Write-Host "==> Важные переменные:"
        Get-Content $cache | Select-String -Pattern "VCPKG_TARGET_TRIPLET|VCPKG_INSTALLED_DIR|CMAKE_PREFIX_PATH|Boost_DIR|OSMIUM_INCLUDE_DIR|PROTOZERO_INCLUDE_DIR"
    }
    Write-Host "==> Содержимое дерева vcpkg"
    Get-ChildItem -Path $Installed -Recurse -Depth 2 -ErrorAction SilentlyContinue |
        Select-Object -First 60 -ExpandProperty FullName
    throw "cmake вернул код $LASTEXITCODE"
}

# Триплет vcpkg задаётся самой toolchain: для Windows x64 это
# x64-windows.  Проверяем, что он совпал с тем, куда ставились
# зависимости: иначе библиотеки ищутся не в том дереве.
$cache = Join-Path $buildDir "CMakeCache.txt"
$usedTriplet = (Get-Content $cache |
    Select-String "^VCPKG_TARGET_TRIPLET:" |
    Select-Object -First 1).Line -replace "^VCPKG_TARGET_TRIPLET:.*=", ""
if ($usedTriplet -ne $Triplet) {
    throw "Триплет CMake ($usedTriplet) не совпадает с триплетом vcpkg ($Triplet)"
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
# Сборка динамическая: рядом с бинарником нужны DLL из vcpkg, иначе
# он не запустится.  vcpkg кладёт их в каталог бинарника (APPLOCAL).
Get-ChildItem -Path $exe.Directory -Filter "*.dll" -ErrorAction SilentlyContinue |
    Copy-Item -Destination $DestDir -Force
Write-Host "==> Содержимое $DestDir :"
Get-ChildItem -Path $DestDir | ForEach-Object { Write-Host "    $($_.Name)" }

Write-Host "==> Проверка"
& (Join-Path $DestDir "osmium.exe") --version
Write-Host "==> Готово: $DestDir\osmium.exe"
