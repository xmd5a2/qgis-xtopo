#!/usr/bin/env python3
"""Сборка дистрибутива плагина и установка его в профиль QGIS.

Корень репозитория одновременно является корнем плагина: при установке в
QGIS каталог называется `qgis_xtopo`, поэтому в архив файлы складываются
в каталог `qgis_xtopo/`.

Использование:
    python3 tools/build_plugin.py            # только собрать архив
    python3 tools/build_plugin.py --install  # собрать и обновить профиль QGIS
"""

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "qgis_xtopo"
DIST = ROOT / "dist"
READY = DIST / "qgis-xtopo-ready"
LAUNCHER = ROOT / "launcher" / "install-and-run.sh"
# Шаблон проекта хранится один раз — в корне репозитория.  В собранный
# пакет он попадает как resources/automap.qgz.
TEMPLATE = ROOT / "automap.qgz"

# Файлы и каталоги, попадающие в дистрибутив.
INCLUDE = (
    "__init__.py",
    "plugin.py",
    "metadata.txt",
    "icon.svg",
    "logo_icon.png",
    "core",
    "gui",
    "resources",
    "tests",
    "vendor",
)
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _is_backup(path):
    """Служебные копии файлов в дистрибутив не попадают."""
    return ".bak" in path.name


def plugin_version():
    for line in (ROOT / "metadata.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("metadata.txt: не найдена строка version=")


def _iter_files(source):
    for path in sorted(source.rglob("*")):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        if _is_backup(path):
            continue
        if path.is_file():
            yield path


def stage(target):
    package = target / PACKAGE_NAME
    package.mkdir(parents=True)
    for name in INCLUDE:
        source = ROOT / name
        if not source.exists():
            continue
        if source.is_dir():
            for path in _iter_files(source):
                relative = path.relative_to(source)
                if name == "resources" and relative == Path("automap.qgz"):
                    # Шаблон берётся из корня, копия в resources не нужна.
                    continue
                destination = package / name / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        else:
            shutil.copy2(source, package / name)
    if not TEMPLATE.exists():
        raise SystemExit(f"Нет исходного шаблона: {TEMPLATE}")
    destination = package / "resources" / TEMPLATE.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATE, destination)
    return package


def build_zip():
    version = plugin_version()
    DIST.mkdir(parents=True, exist_ok=True)
    archive = DIST / f"{PACKAGE_NAME}-{version}.zip"
    with tempfile.TemporaryDirectory() as directory:
        package = stage(Path(directory))
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(package.rglob("*")):
                if path.is_dir():
                    continue
                zipped.write(path, path.relative_to(package.parent))
    return archive


def build_bundle(archive):
    """Готовый набор для новых пользователей: архив + лаунчер."""
    READY.mkdir(parents=True, exist_ok=True)
    for old in READY.glob(f"{PACKAGE_NAME}-*.zip"):
        old.unlink()
    shutil.copy2(archive, READY / archive.name)
    target = READY / LAUNCHER.name
    shutil.copy2(LAUNCHER, target)
    target.chmod(0o755)
    return READY


def make_executables(target):
    """Возвращает бит исполнения поставляемым бинарникам.

    В zip права не сохраняются, поэтому после распаковки osmium из
    поставки плагина не запускается.  В самом плагине бит исполнения
    ставится ещё раз при поиске бинарника, но полезно и здесь.
    """
    for path in (Path(target) / "vendor" / "osmium").glob("*/*"):
        if path.is_file() and path.suffix in ("", ".exe"):
            path.chmod(0o755)


def install(archive):
    target = (
        Path.home()
        / ".local/share/QGIS/QGIS3/profiles/default/python/plugins"
        / PACKAGE_NAME
    )
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(directory)
        shutil.copytree(Path(directory) / PACKAGE_NAME, target)
    for cache in target.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    make_executables(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true", help="обновить плагин в профиле QGIS"
    )
    arguments = parser.parse_args()

    archive = build_zip()
    print(f"Архив: {archive} ({archive.stat().st_size} байт)")
    print(f"Набор для новых пользователей: {build_bundle(archive)}")
    if arguments.install:
        target = install(archive)
        print(f"Установлено в: {target}")


if __name__ == "__main__":
    main()
