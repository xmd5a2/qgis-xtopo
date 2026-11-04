<#
.SYNOPSIS
  Поиск установленного QGIS и (по запросу) его скачивание и установка.

.DESCRIPTION
  Вызывается инсталлятором QGIS-xtopo, когда QGIS в системе не найден.
  Скрипт скачивает официальный установщик QGIS (standalone MSI) и ставит
  его без вопросов, затем печатает путь к qgis-bin.exe.

.PARAMETER Install
  Если задан, QGIS скачивается и устанавливается, когда его не найдено.

.PARAMETER LogFile
  Куда писать журнал действий (необязательно).
#>
param(
    [switch]$Install,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# На старых системах TLS 1.2 может быть выключен по умолчанию.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    if ($LogFile) {
        try {
            [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine)
        } catch {
            # Журнал не критичен: установка продолжается без него.
        }
    }
}

function Get-QgisExe {
    $candidates = @()

    foreach ($key in @("HKLM:\SOFTWARE\QGIS\QGIS3", "HKLM:\SOFTWARE\WOW6432Node\QGIS\QGIS3")) {
        if (Test-Path $key) {
            $properties = Get-ItemProperty -Path $key
            $named = $properties.InstallPath
            if ($named) { $candidates += [string]$named }
            $fallback = $properties.'(default)'
            if ($fallback) { $candidates += [string]$fallback }
        }
    }

    if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "QGIS 3") }
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} "QGIS 3") }
    if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "Programs\QGIS 3") }
    if ($env:SystemDrive) { $candidates += (Join-Path $env:SystemDrive "OSGeo4W") }

    foreach ($base in $candidates) {
        if (-not $base -or -not (Test-Path $base)) { continue }
        $direct = Join-Path $base "bin\qgis-bin.exe"
        if (Test-Path $direct) { return $direct }
        $versioned = Get-ChildItem -Path $base -Directory -Filter "QGIS *" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Select-Object -First 1
        if ($versioned) {
            $exe = Join-Path $versioned.FullName "bin\qgis-bin.exe"
            if (Test-Path $exe) { return $exe }
        }
    }
    return $null
}

function Get-LatestMsiName {
    $page = Invoke-WebRequest -Uri "https://qgis.org/download/" -UseBasicParsing
    $names = [regex]::Matches($page.Content, "QGIS-OSGeo4W-[0-9.]+-[0-9]+\.msi") |
        ForEach-Object { $_.Value } |
        Sort-Object -Unique
    if (-not $names) { return $null }

    $best = $null
    $bestKey = $null
    $bestName = $null
    foreach ($name in $names) {
        if ($name -match "QGIS-OSGeo4W-([0-9.]+)-([0-9]+)\.msi") {
            $key = [version]$Matches[1]
            $build = [int]$Matches[2]
            if ($null -eq $bestKey -or $key -gt $bestKey -or ($key -eq $bestKey -and $build -gt $best)) {
                $bestKey = $key
                $best = $build
                $bestName = $name
            }
        }
    }
    return $bestName
}

$existing = Get-QgisExe
if ($existing) {
    Write-Log "QGIS найден: $existing"
    Write-Output $existing
    exit 0
}

if (-not $Install) {
    Write-Log "QGIS не найден"
    exit 2
}

Write-Log "QGIS не найден — скачиваем установщик"
$name = Get-LatestMsiName
if (-not $name) {
    Write-Log "Не удалось определить последнюю версию QGIS"
    exit 3
}
Write-Log "Установщик: $name"

$target = Join-Path $env:TEMP $name
$urls = @(
    "https://download.qgis.org/downloads/$name",
    "https://dl1.qgis.org/downloads/$name"
)
$downloaded = $false
foreach ($url in $urls) {
    try {
        Write-Log "Скачивание $url"
        (New-Object System.Net.WebClient).DownloadFile($url, $target)
        $downloaded = $true
        break
    } catch {
        Write-Log "Не удалось скачать с $url : $($_.Exception.Message)"
    }
}
if (-not $downloaded) {
    Write-Log "Скачивание не удалось"
    exit 4
}

Write-Log "Установка QGIS (может занять несколько минут)"
$process = Start-Process -FilePath "msiexec.exe" `
    -ArgumentList "/i `"$target`" /qn /norestart" `
    -Wait -PassThru
if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
    # 0 — успех, 3010 — успех, но нужна перезагрузка.
    Write-Log "msiexec завершился с кодом $($process.ExitCode)"
    exit 5
}

$installed = Get-QgisExe
if (-not $installed) {
    Write-Log "После установки QGIS не найден"
    exit 6
}
Write-Log "QGIS установлен: $installed"
Write-Output $installed
exit 0
