#!/bin/sh
# Сборка инсталлятора для Windows (Inno Setup).
#
# Шаги:
#   1. собирается архив плагина (tools/build_plugin.py);
#   2. архив распаковывается в installer/payload (содержимое .iss);
#   3. вызывается компилятор Inno Setup (ISCC).
#
# Компилятор ищется в таком порядке:
#   * переменная ISCC (путь к ISCC.exe или ISCC);
#   * iscc в PATH (Windows);
#   * Inno Setup 6 в префиксе wine ($WINEPREFIX, по умолчанию ~/.wine).
#
# Пример:
#   ./installer/build-installer.sh
#   ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe" ./installer/build-installer.sh
set -e

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname "$DIR")
DIST="$ROOT/dist"
PAYLOAD="$DIR/payload"
VERSION=$(sed -n 's/^version=//p' "$ROOT/metadata.txt" | tr -d '\r' | head -1)

if [ -z "$VERSION" ]; then
    echo "metadata.txt: не найдена строка version=" >&2
    exit 1
fi

echo "== Сборка плагина $VERSION =="
python3 "$ROOT/tools/build_plugin.py"

ARCHIVE="$DIST/qgis_xtopo-$VERSION.zip"
if [ ! -f "$ARCHIVE" ]; then
    echo "Не найден архив $ARCHIVE" >&2
    exit 1
fi

echo "== Подготовка payload =="
rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD"
python3 - "$ARCHIVE" "$PAYLOAD" <<'PY'
import sys
import zipfile

archive, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(archive) as zipped:
    zipped.extractall(target)
PY
mkdir -p "$PAYLOAD/scripts"
cp "$DIR/scripts/install-qgis.ps1" "$PAYLOAD/scripts/install-qgis.ps1"

echo "== Поиск компилятора Inno Setup =="
ISCC_BIN=""
if [ -n "${ISCC-}" ]; then
    ISCC_BIN="$ISCC"
elif command -v iscc >/dev/null 2>&1; then
    ISCC_BIN="$(command -v iscc)"
else
    WINE_PREFIX="${WINEPREFIX:-$HOME/.wine}"
    for candidate in \
        "$WINE_PREFIX/drive_c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
        "$WINE_PREFIX/drive_c/Program Files/Inno Setup 6/ISCC.exe" \
        "$WINE_PREFIX/drive_c/inno/ISCC.exe"
    do
        if [ -f "$candidate" ]; then
            ISCC_BIN="$candidate"
            break
        fi
    done
fi

if [ -z "$ISCC_BIN" ]; then
    echo "Компилятор Inno Setup не найден." >&2
    echo "Установите Inno Setup 6 и укажите путь к ISCC.exe через ISCC=..." >&2
    exit 1
fi

case "$ISCC_BIN" in
    *.exe)
        echo "Сборка через wine: $ISCC_BIN"
        WINEPREFIX="${WINEPREFIX:-$HOME/.wine}"
        # ISCC ждёт путь в виде Z:\..., а не /home/...
        SCRIPT=$(WINEPREFIX="${WINEPREFIX:-$HOME/.wine}" winepath -w \
            "$(CDPATH= cd -- "$DIR" && pwd)/qgis-xtopo.iss")
        WINEPREFIX="${WINEPREFIX:-$HOME/.wine}" wine "$ISCC_BIN" "$SCRIPT"
        ;;
    *)
        echo "Сборка: $ISCC_BIN"
        "$ISCC_BIN" "$DIR/qgis-xtopo.iss"
        ;;
esac

SETUP="$DIST/qgis-xtopo-setup-$VERSION.exe"
if [ ! -f "$SETUP" ]; then
    echo "Инсталлятор не собран: $SETUP не найден" >&2
    exit 1
fi

echo "== Готово =="
ls -l "$SETUP"
