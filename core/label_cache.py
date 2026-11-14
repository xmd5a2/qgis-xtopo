"""Кэш линий подписи.

Линия подписи — чистая функция геометрии слоя: тот же вход даёт тот же
результат.  Повторная подготовка того же района не должна пересчитывать
её заново, а расчёт занимает больше десяти секунд на ледниках и лесах.

Ключ кэша — версия алгоритма, имя слоя, режим линии и отпечаток
содержимого исходного слоя.  Содержимое, а не файл: SQLite-файл может
отличаться байтами при той же геометрии.
"""

import hashlib
import shutil
from pathlib import Path

from . import terrain_cache

# Версия расчёта: при изменении алгоритма ключ меняется, и старые линии
# в кэше больше не используются.
LABEL_CACHE_VERSION = "1"
# Сколько последних записей хранить в кэше.
LABEL_CACHE_KEEP = 5


def cache_key(config, name, layer_path, main_flow=False):
    """Ключ кэша: версия алгоритма, слой, режим линии и его содержимое."""
    digest = hashlib.sha1()
    parts = (
        LABEL_CACHE_VERSION,
        name,
        "main_flow" if main_flow else "plain",
        terrain_cache.layer_digest(layer_path),
    )
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def cache_path(config, key):
    """Путь записи кэша или None, если кэш отключён."""
    root = getattr(config, "label_cache_dir", None)
    if root is None:
        return None
    return Path(root).expanduser() / f"{key}.gpkg"


def restore(config, key, target):
    """Копирует готовые линии подписи; False — кэша нет."""
    source = cache_path(config, key)
    if source is None or not source.exists():
        return False
    try:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError:
        return False
    return True


def store(config, key, source):
    """Сохраняет посчитанные линии подписи в кэш."""
    target = cache_path(config, key)
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    except OSError:
        return
    _prune(target.parent)


def _prune(directory):
    """В кэше остаётся несколько последних записей."""
    try:
        entries = [item for item in directory.iterdir() if item.is_file()]
    except OSError:
        return
    entries.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for item in entries[LABEL_CACHE_KEEP:]:
        item.unlink(missing_ok=True)
