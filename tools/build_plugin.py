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
import filecmp
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "qgis_xtopo"
DIST = ROOT / "dist"
TEMPLATE = ROOT / "automap.qgz"
TEMPLATE_COPY = ROOT / "resources" / "automap.qgz"

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
)
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def plugin_version():
    for line in (ROOT / "metadata.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("metadata.txt: не найдена строка version=")


def sync_template():
    """Копирует исходный шаблон проекта в ресурсы плагина."""
    if not TEMPLATE.exists():
        raise SystemExit(f"Нет исходного шаблона: {TEMPLATE}")
    if TEMPLATE_COPY.exists() and filecmp.cmp(TEMPLATE, TEMPLATE_COPY, shallow=False):
        return False
    shutil.copy2(TEMPLATE, TEMPLATE_COPY)
    return True


def _iter_files(source):
    for path in sorted(source.rglob("*")):
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
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
                destination = package / name / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        else:
            shutil.copy2(source, package / name)
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
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true", help="обновить плагин в профиле QGIS"
    )
    arguments = parser.parse_args()

    if sync_template():
        print(f"Шаблон обновлён: {TEMPLATE_COPY.relative_to(ROOT)}")
    archive = build_zip()
    print(f"Архив: {archive} ({archive.stat().st_size} байт)")
    if arguments.install:
        target = install(archive)
        print(f"Установлено в: {target}")


if __name__ == "__main__":
    main()
