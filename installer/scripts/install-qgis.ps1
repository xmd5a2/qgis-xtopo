<#
.SYNOPSIS
  Поиск установленного QGIS и (по запросу) его скачивание и установка.

.DESCRIPTION
  Способы установки QGIS (в порядке предпочтения):

    1. Официальный сетевой установщик OSGeo4W (osgeo4w-setup.exe) с
       фиксированным списком пакетов. Ставится только QGIS LTR
       (пакет qgis-ltr и его зависимости) — около 410 МБ вместо
       580 МБ у standalone-MSI. Установка идёт в каталог пользователя
       (%LOCALAPPDATA%\OSGeo4W), права администратора не нужны.
    2. Официальный standalone-MSI QGIS (QGIS-OSGeo4W-<версия>.msi) —
       запасной путь, если сетевой установщик не сработал.

  Оба варианта — подписанные официальные файлы OSGeo/QGIS.

  Раскладка файлов у двух вариантов разная, поэтому поиск QGIS
  проверяет оба:
    * standalone: <корень>\bin\qgis-bin.exe, запуск напрямую;
    * OSGeo4W:    <корень>\bin\qgis-ltr-bin.exe + <корень>\bin\qgis-ltr.bat,
                  запуск через .bat — он выставляет переменные окружения
                  (PATH, PYTHONPATH, QGIS_PREFIX_PATH, GDAL_DATA...).

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
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityPointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Пакет OSGeo4W с QGIS LTR: тянёт GDAL, PROJ, Qt5, Python и numpy —
# ровно то, что нужно плагину. Версию берёт сам установщик (последняя LTR).
$Osgeo4wPackages = "qgis-ltr"
$Osgeo4wSite = "https://download.osgeo.org/osgeo4w/v2/"
$Osgeo4wSetupUrls = @(
    "https://download.osgeo.org/osgeo4w/v2/osgeo4w-setup.exe",
    "https://ftp2.osuosl.org/pub/osgeo/download/osgeo4w/v2/osgeo4w-setup.exe"
)

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

<#
  В корне ищет QGIS. В корне может быть версионный подкаталог
  ("QGIS 3.44.15") — как в standalone-установке.
#>
function Find-QgisIn {
    param([string]$Base)
    if (-not $Base -or -not (Test-Path $Base)) { return $null }

    $candidates = @($Base)
    foreach ($dir in (Get-ChildItem -Path $Base -Directory -Filter "QGIS *" -ErrorAction SilentlyContinue)) {
        $candidates += $dir.FullName
    }

    foreach ($root in $candidates) {
        # Standalone-установка: запуск исполняемого файла.
        $standalone = Join-Path $root "bin\qgis-bin.exe"
        if (Test-Path $standalone) {
            return [pscustomobject]@{
                Root = $root
                Exe = $standalone
                Launcher = $standalone
                Kind = "standalone"
            }
        }
        # Установка OSGeo4W: приложение и .bat с переменными окружения.
        $osgeo = Join-Path $root "bin\qgis-ltr-bin.exe"
        if (Test-Path $osgeo) {
            $bat = Join-Path $root "bin\qgis-ltr.bat"
            $launcher = $osgeo
            if (Test-Path $bat) { $launcher = $bat }
            return [pscustomobject]@{
                Root = $root
                Exe = $osgeo
                Launcher = $launcher
                Kind = "osgeo4w"
            }
        }
    }
    return $null
}

<#
  Ищет QGIS и возвращает описание установки: Root (корень), Exe
  (исполняемый файл), Launcher (чем запускать). $null — не найден.
#>
function Get-QgisInstall {
    $roots = @()

    foreach ($key in @("HKLM:\SOFTWARE\QGIS\QGIS3", "HKLM:\SOFTWARE\WOW6432Node\QGIS\QGIS3")) {
        if (Test-Path $key) {
            $properties = Get-ItemProperty -Path $key
            $named = $properties.InstallPath
            if ($named) { $roots += [string]$named }
            $fallback = $properties.'(default)'
            if ($fallback) { $roots += [string]$fallback }
        }
    }

    if ($env:ProgramFiles) { $roots += (Join-Path $env:ProgramFiles "QGIS 3") }
    if (${env:ProgramFiles(x86)}) { $roots += (Join-Path ${env:ProgramFiles(x86)} "QGIS 3") }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "Programs\QGIS 3") }
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "OSGeo4W") }
    if ($env:SystemDrive) { $roots += (Join-Path $env:SystemDrive "OSGeo4W") }

    $seen = @{}
    foreach ($base in $roots) {
        if (-not $base -or $seen.ContainsKey($base)) { continue }
        $seen[$base] = $true
        $found = Find-QgisIn $base
        if ($found) { return $found }
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

function Download-File {
    param([string]$Url, [string]$Target)
    Write-Log "Скачивание $Url"
    (New-Object System.Net.WebClient).DownloadFile($Url, $Target)
}

<#
  Установка через официальный сетевой установщик OSGeo4W: ставится
  только QGIS LTR с зависимостями. Каталог — в профиле пользователя,
  поэтому права администратора не нужны.
#>
function Install-Osgeo4W {
    $target = Join-Path $env:TEMP "osgeo4w-setup.exe"
    $downloaded = $false
    foreach ($url in $Osgeo4wSetupUrls) {
        try {
            Download-File -Url $url -Target $target
            $downloaded = $true
            break
        } catch {
            Write-Log "Не удалось скачать с $url : $($_.Exception.Message)"
        }
    }
    if (-not $downloaded) {
        Write-Log "Скачивание установщика OSGeo4W не удалось"
        return $false
    }

    $root = Join-Path $env:LOCALAPPDATA "OSGeo4W"
    # Аргументы одной строкой: путь установки может содержать пробелы,
    # Start-Process не добавляет кавычки сам.
    $arguments = '-q -k -A -O -s "{0}" -R "{1}" -P {2} -d -n' -f `
        $Osgeo4wSite, $root, $Osgeo4wPackages
    Write-Log ("Установка QGIS через OSGeo4W: пакеты {0} в {1}" -f $Osgeo4wPackages, $root)
    $process = Start-Process -FilePath $target -ArgumentList $arguments `
        -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        Write-Log "osgeo4w-setup.exe завершился с кодом $($process.ExitCode)"
        $setupLog = Join-Path $root "var\log\setup.log"
        if (Test-Path $setupLog) {
            Write-Log "Журнал установщика:"
            try {
                Get-Content $setupLog -Tail 20 | ForEach-Object { Write-Log "  $_" }
            } catch {
                # Журнал недоступен — не критично.
            }
        }
        return $false
    }

    $installed = Get-QgisInstall
    if (-not $installed) {
        Write-Log "После установки OSGeo4W QGIS не найден"
        return $false
    }
    Write-Log "QGIS установлен: $($installed.Exe)"
    return $true
}

<#
  Запасной путь: официальный standalone-MSI QGIS.
#>
function Install-Msi {
    $name = Get-LatestMsiName
    if (-not $name) {
        Write-Log "Не удалось определить последнюю версию QGIS"
        return $false
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
            Download-File -Url $url -Target $target
            $downloaded = $true
            break
        } catch {
            Write-Log "Не удалось скачать с $url : $($_.Exception.Message)"
        }
    }
    if (-not $downloaded) {
        Write-Log "Скачивание не удалось"
        return $false
    }

    Write-Log "Установка QGIS (может занять несколько минут)"
    $process = Start-Process -FilePath "msiexec.exe" `
        -ArgumentList "/i `"$target`" /qn /norestart" `
        -Wait -PassThru
    if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
        # 0 — успех, 3010 — успех, но нужна перезагрузка.
        Write-Log "msiexec завершился с кодом $($process.ExitCode)"
        return $false
    }

    $installed = Get-QgisInstall
    if (-not $installed) {
        Write-Log "После установки QGIS не найден"
        return $false
    }
    Write-Log "QGIS установлен: $($installed.Exe)"
    return $true
}

$existing = Get-QgisInstall
if ($existing) {
    Write-Log "QGIS найден: $($existing.Exe)"
    Write-Output $existing.Exe
    exit 0
}

if (-not $Install) {
    Write-Log "QGIS не найден"
    exit 2
}

Write-Log "QGIS не найден — устанавливаем"
if (Install-Osgeo4W) {
    $installed = Get-QgisInstall
    Write-Output $installed.Exe
    exit 0
}

Write-Log "OSGeo4W не сработал, пробуем standalone-MSI"
if (Install-Msi) {
    $installed = Get-QgisInstall
    Write-Output $installed.Exe
    exit 0
}

Write-Log "Установить QGIS автоматически не удалось"
exit 7
