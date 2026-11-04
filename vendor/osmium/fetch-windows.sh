#!/usr/bin/env bash
# Получение бинарника osmium-tool для Windows x86_64.
#
# Скрипт сам подбирает свежую совместимую сборку в conda-forge: osmium-tool
# для win-64 (не ниже 1.16) и ровно те версии его зависимостей, которые
# требует эта сборка.  Обновление пакетов на стороне conda-forge не ломает
# получение бинарника.
#
# Результат: vendor/osmium/win-x86_64/osmium.exe и пять DLL рядом с ним.
#
# Требуются: python3, tar с поддержкой zstd.
#
# Использование:
#   DEST_DIR=vendor/osmium ./vendor/osmium/fetch-windows.sh
#   DEST_DIR=vendor/osmium ./vendor/osmium/fetch-windows.sh --update-pin
set -euo pipefail

ScriptDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DestDir="${DEST_DIR:-$ScriptDir/win-x86_64}"

exec python3 "$ScriptDir/fetch-windows.py" --dest "$DestDir" "$@"
