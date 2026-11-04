#!/bin/sh
# Лаунчер для новых пользователей: устанавливает плагин в профиль QGIS
# и сразу запускает QGIS с открытой панелью плагина.
#
# Работает из каталога, где лежит архив qgis_xtopo-<версия>.zip (например,
# из готового набора dist/qgis-xtopo-ready/).
#
# Использование:
#   ./install-and-run.sh                     # взять единственный архив рядом
#   ./install-and-run.sh путь/к/архиву.zip   # конкретная версия
#
# Переменные окружения (для тестов и нестандартных установок):
#   QGIS_XTOPO_PROFILES   — каталог профилей QGIS (по умолчанию стандартный
#                           для системы ~/.local/share/QGIS/QGIS3 и т.п.)
#   QGIS_XTOPO_NO_LAUNCH  — установить плагин, не запуская QGIS
set -e

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Установка в работающий QGIS бессмысленна: файлы прочитаны при старте,
# а изменение профиля будет перезаписано при выходе. Разрешаем, только
# если профиль явно переопределён (тесты, отдельный профиль).
if [ -z "$QGIS_XTOPO_PROFILES" ] && [ -z "$QGIS_XTOPO_NO_LAUNCH" ]; then
    if pgrep -x qgis.bin >/dev/null 2>&1 || pgrep -x qgis >/dev/null 2>&1; then
        echo "QGIS сейчас запущен. Закройте его и запустите лаунчер снова." >&2
        exit 1
    fi
fi

case "${1-}" in
    *.zip)
        QGIS_XTOPO_ZIP="$1"
        export QGIS_XTOPO_ZIP
        shift
        ;;
esac
export QGIS_XTOPO_LAUNCHER_DIR="$DIR"

python3 - <<'PY'
"""Установка плагина из zip в профиль QGIS и включение его."""

import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN = "qgis_xtopo"


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


def find_archive():
    explicit = os.environ.get("QGIS_XTOPO_ZIP")
    if explicit:
        archive = Path(explicit).expanduser().resolve()
        if not archive.is_file():
            fail(f"Архив не найден: {archive}")
        return archive
    directory = Path(os.environ["QGIS_XTOPO_LAUNCHER_DIR"])
    candidates = sorted(
        directory.glob(f"{PLUGIN}-*.zip"), key=lambda p: p.stat().st_mtime
    )
    if not candidates:
        fail(
            f"Рядом с лаунчером ({directory}) нет архива {PLUGIN}-<версия>.zip.\n"
            "Передайте путь к архиву аргументом: ./install-and-run.sh архив.zip"
        )
    return candidates[-1]


def profiles_root():
    override = os.environ.get("QGIS_XTOPO_PROFILES")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/QGIS/QGIS3"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            fail("Не найден каталог APPDATA — укажите QGIS_XTOPO_PROFILES вручную")
        return Path(appdata) / "QGIS/QGIS3"
    return home / ".local/share/QGIS/QGIS3"


def make_executables(target):
    """Возвращает бит исполнения поставляемым бинарникам.

    В zip права не сохраняются, поэтому после распаковки osmium из
    поставки плагина не запускается.
    """
    for path in (target / "vendor" / "osmium").glob("*/*"):
        if path.is_file() and path.suffix in ("", ".exe"):
            path.chmod(0o755)


def install(archive, profile):
    target = profile / "python/plugins" / PLUGIN
    with tempfile.TemporaryDirectory() as directory:
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(directory)
        extracted = Path(directory) / PLUGIN
        if not extracted.is_dir():
            fail(f"В архиве нет каталога {PLUGIN}/: {archive}")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(extracted, target)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    make_executables(target)
    return target


def enable_plugin(profile):
    """Выставляет qgis_xtopo=true в секции [PythonPlugins] профиля.

    Именно там QGIS хранит включённые сторонние плагины. Правка текстовая,
    чтобы не перезаписывать остальное содержимое QGIS3.ini.
    """
    ini = profile / "QGIS/QGIS3.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    text = ini.read_text(encoding="utf-8") if ini.exists() else ""
    key = re.compile(r"^" + re.escape(PLUGIN) + r"\s*=.*$", re.MULTILINE)
    if key.search(text):
        text = key.sub(f"{PLUGIN}=true", text)
    elif re.search(r"^\[PythonPlugins\]\s*$", text, re.MULTILINE):
        text = re.sub(
            r"^\[PythonPlugins\]\s*$",
            f"[PythonPlugins]\n{PLUGIN}=true",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"[PythonPlugins]\n{PLUGIN}=true\n"
    ini.write_text(text, encoding="utf-8")


archive = find_archive()
profile = profiles_root() / "profiles" / "default"
install(archive, profile)
enable_plugin(profile)
print(f"Плагин {archive.name} установлен в профиль: {profile}")
PY

if [ -n "$QGIS_XTOPO_NO_LAUNCH" ]; then
    exit 0
fi

export QGIS_XTOPO_OPEN=1
if [ -n "$QGIS_XTOPO_PROFILES" ]; then
    exec qgis --profiles-path "$QGIS_XTOPO_PROFILES" "$@"
fi
exec qgis "$@"
