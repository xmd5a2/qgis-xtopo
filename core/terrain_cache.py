"""Кэш результатов обработки рельефа.

Рельеф и изолинии считаются долго — на большой площади это минуты. Поэтому
результаты сохраняются в кэш и переиспользуются, пока не изменилось:

* версия плагина;
* параметры обработки (шаг изолиний, сглаживание, метод ресемплинга);
* исходные данные (тайлы DEM, слои воды и ледников).

Ключ кэша — хеш этих данных. Сменилась версия плагина, параметры или
данные — ключ не совпадает, и рельеф считается заново.

Кэш лежит рядом с кэшем тайлов SRTM: ``<проекты>/qgisxtopo-cache/terrain``.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

# Файлы рельефа, которые переиспользуются между запусками.
DEM_FILES = (
    "merged_dem.tif",
    "merged_dem_3857.tif",
    "merged_dem_upscaled.tif",
    "slope_upscaled.tif",
    "hillshade_composite.tif",
)

# Версия расчёта рельефа (формат растров, сборка мозаики, репроекция).
# Меняя результат, поднимайте число: иначе старый кэш отдаст устаревший
# рельеф даже после обновления плагина.  Версия плагина сюда намеренно не
# входит — обновление интерфейса или стиля не должно пересчитывать DEM.
DEM_VERSION = 2

# Версия параметров отмывки (core/terrain.py: generate_hillshade_slope).
# Меняя направление света, преувеличение или подсветку теней, поднимайте
# число: иначе кэш вернёт растры, посчитанные по старым параметрам.
HILLSHADE_STYLE_VERSION = 2

# Версия расчёта изолиний: 2 — DEM режется на тайлы с нахлёстом,
# контуры считаются параллельно, в результат попадает середина тайла.
ISOLINES_VERSION = 2
# Маркер репроекции: без него повторная сборка DEM не найдёт свой кэш.
DEM_EXTRA_FILES = ("merged_dem_3857.tif.sha256",)
ISOLINES_FILES = (
    "isolines_regular.sqlite",
    "isolines_glacier.sqlite",
)

# Сколько последних записей кэша оставляем в каждой группе.
KEEP_ENTRIES = 3
# Сколько подсказок «эта область уже посчитана» храним.  Подсказка
# избавляет от скачивания тайлов DEM перед проверкой кэша.
KEEP_HINTS = 16

_CHUNK = 1024 * 1024


def plugin_version():
    """Версия плагина из metadata.txt."""
    from .resources import PACKAGE_DIR

    path = Path(PACKAGE_DIR) / "metadata.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "0"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    return "0"


def _digest(parts):
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _file_sha256(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return "missing"
    return digest.hexdigest()


def _layer_digest(path):
    """Хест содержимого слоя (геометрия и атрибуты).

    Хеш файла не годится: SQLite-файл может отличаться байтами при той же
    геометрии. Поэтому сравниваем сами объекты слоя.
    """
    path = Path(path)
    if not path.exists():
        return "none"
    try:
        from osgeo import ogr
    except ImportError:
        return _file_sha256(path)
    digest = hashlib.sha1()
    try:
        dataset = ogr.Open(str(path))
    except Exception:  # noqa: BLE001 - битый файл считаем пустым
        return "broken"
    if dataset is None:
        return "broken"
    try:
        for index in range(dataset.GetLayerCount()):
            layer = dataset.GetLayer(index)
            definition = layer.GetLayerDefn()
            fields = [
                definition.GetFieldDefn(position).GetName()
                for position in range(definition.GetFieldCount())
            ]
            digest.update(("|".join(fields)).encode("utf-8"))
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and not geometry.IsEmpty():
                    digest.update(geometry.ExportToWkb())
                for name in fields:
                    digest.update(str(feature.GetField(name)).encode("utf-8"))
    finally:
        dataset = None
    return digest.hexdigest()


def dem_source_digest(config):
    """Хест исходных тайлов DEM.

    Считается по файлам `.hgt` (как скачаны) — их содержимое стабильно от
    запуска к запуску, в отличие от результата конвертации в `.tif`.
    """
    parts = []
    try:
        entries = sorted(config.terrain_input_dir.iterdir())
    except OSError:
        return _digest([])
    for path in entries:
        if not path.is_file():
            continue
        if path.suffix.lower() not in (".hgt", ".tif"):
            continue
        parts.append(path.name)
        parts.append(_file_sha256(path))
    return _digest(parts)


def dem_signature(config, bbox, source_digest=None):
    """Ключ кэша рельефа: версия формата, область, параметры и тайлы DEM."""
    if source_digest is None:
        source_digest = dem_source_digest(config)
    parts = [
        DEM_VERSION,
        bbox.as_csv() if hasattr(bbox, "as_csv") else str(bbox),
        config.resolved_terrain_source(),
        config.terrain_resample_method,
        config.generate_terrain_hillshade_slope,
        HILLSHADE_STYLE_VERSION,
        source_digest,
    ]
    return _digest(parts)


def _stat_digest(directory):
    """Быстрый отпечаток каталога: имена, размеры и время изменения."""
    parts = []
    try:
        entries = sorted(Path(directory).expanduser().iterdir())
    except OSError:
        return _digest(parts)
    for path in entries:
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return _digest(parts)


def dem_hint(config, bbox):
    """Ключ «эта область уже посчитана» без скачивания тайлов.

    Полный ключ :func:`dem_signature` включает хеш содержимого тайлов DEM,
    поэтому его нельзя вычислить до загрузки.  Подсказка строится только
    по настройкам и области: если готовый рельеф уже лежит в кэше, тайлы
    скачивать незачем.  Для ручного каталога и локальных тайлов в ключ
    входит отпечаток файлов, чтобы подмена данных не осталась незамеченной.
    """
    source = config.resolved_terrain_source()
    parts = [
        DEM_VERSION,
        bbox.as_csv() if hasattr(bbox, "as_csv") else str(bbox),
        source,
        config.terrain_resample_method,
        config.generate_terrain_hillshade_slope,
        HILLSHADE_STYLE_VERSION,
        config.download_terrain_tiles,
        config.get_terrain_tiles,
    ]
    if source == "manual":
        parts.append(_stat_digest(config.terrain_src_dir))
    elif not config.download_terrain_tiles:
        parts.append(_stat_digest(config.terrain_input_dir))
    return _digest(parts)


def _hint_dir(config):
    return config.terrain_result_cache_dir / "dem-hints"


def _hint_path(config, hint):
    return _hint_dir(config) / f"{hint}.json"


def _prune_hints(directory):
    """Убирает подсказки на уже удалённые записи кэша."""
    try:
        entries = [item for item in directory.iterdir() if item.is_file()]
    except OSError:
        return
    entries.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for item in entries[KEEP_HINTS:]:
        item.unlink(missing_ok=True)
    dem_dir = directory.parent / "dem"
    for item in entries[:KEEP_HINTS]:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            item.unlink(missing_ok=True)
            continue
        signature = str(data.get("signature") or "")
        if not signature or not (dem_dir / signature).is_dir():
            item.unlink(missing_ok=True)


def store_dem_hint(config, hint, signature, used=(), fallback=""):
    """Запоминает, какой полный ключ соответствует области и настройкам."""
    path = _hint_path(config, hint)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(
        json.dumps(
            {
                "signature": signature,
                "used": list(used or ()),
                "fallback": fallback or "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    _prune_hints(path.parent)


def restore_dem_by_hint(config, hint):
    """Готовый рельеф по подсказке или None.

    Возвращает данные подсказки: полный ключ, наборы мозаики и сообщение
    о замене тайлов.  Если запись кэша успела удалиться, подсказка тоже
    убирается.
    """
    path = _hint_path(config, hint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    signature = str(data.get("signature") or "")
    if not signature or not restore_dem(config, signature):
        path.unlink(missing_ok=True)
        return None
    return data


def isolines_signature(config, bbox, dem_key):
    """Ключ кэша изолиний.

    Кроме рельефа учитываются слои, по которым изолинии режутся: постоянная
    и пересыхающая вода и ледники. Изменились данные — изолинии
    пересчитываются заново.
    """
    parts = [
        dem_key,
        config.isolines_step,
        config.smooth_isolines,
        bbox.as_csv() if hasattr(bbox, "as_csv") else str(bbox),
        _layer_digest(config.vector_dir / "water_dissolved.sqlite"),
        _layer_digest(config.vector_dir / "water.sqlite"),
        _layer_digest(config.vector_dir / "water_intermittent_dissolved.sqlite"),
        _layer_digest(config.vector_dir / "water_intermittent.sqlite"),
        _layer_digest(config.vector_dir / "glacier.sqlite"),
        # Нарезка DEM на тайлы изменилась: старые изолинии считались
        # одним проходом и переиспользовать их нельзя.
        ISOLINES_VERSION,
    ]
    return _digest(parts)


def _group_dir(config, group, signature):
    return config.terrain_result_cache_dir / group / signature


def _prune(directory, keep=KEEP_ENTRIES):
    """Оставляет несколько последних записей кэша."""
    try:
        entries = [item for item in directory.iterdir() if item.is_dir()]
    except OSError:
        return
    entries.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for item in entries[keep:]:
        shutil.rmtree(item, ignore_errors=True)


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _restore_item(item, target):
    """Восстанавливает файл кэша без лишнего копирования.

    Крупные растры занимают гигабайты, и каждый прогон переписывал их
    заново.  Если файл уже лежит на месте с тем же размером и временем,
    копирование не нужно.  Удалённые перед восстановлением растры можно
    связать жёсткой ссылкой: они только читаются, а при следующей
    подготовке удаляются до записи новых.
    """
    if target.exists():
        if _same_file(item, target):
            return
        try:
            source_stat = item.stat()
            target_stat = target.stat()
            if (source_stat.st_size == target_stat.st_size
                    and source_stat.st_mtime_ns == target_stat.st_mtime_ns):
                return
        except OSError:
            pass
    if not target.exists() and target.name in (
            "slope_upscaled.tif", "hillshade_composite.tif"):
        try:
            os.link(item, target)
            return
        except OSError:
            pass
    shutil.copy2(item, target)


def _restore(config, group, signature, names, target_dir, required):
    source = _group_dir(config, group, signature)
    if not source.is_dir():
        return False
    for name in required:
        if not (source / name).exists():
            return False
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        item = source / name
        if item.exists():
            _restore_item(item, target_dir / name)
    return True


def _store(config, group, signature, names, source_dir):
    target = _group_dir(config, group, signature)
    target.mkdir(parents=True, exist_ok=True)
    stored = False
    for name in names:
        item = source_dir / name
        if not item.exists():
            continue
        destination = target / name
        if not _same_file(item, destination):
            shutil.copy2(item, destination)
        stored = True
    if stored:
        _prune(target.parent)
    return stored


def dem_required(config):
    """Растры, которые должны быть в кэше при текущих настройках."""
    names = ["merged_dem.tif", "merged_dem_3857.tif"]
    if config.generate_terrain_hillshade_slope:
        names.extend(
            ["merged_dem_upscaled.tif", "slope_upscaled.tif", "hillshade_composite.tif"]
        )
    return tuple(names)


def restore_dem(config, signature):
    """Возвращает готовые растры рельефа из кэша (True — удалось)."""
    return _restore(
        config, "dem", signature,
        DEM_FILES + DEM_EXTRA_FILES, config.raster_dir, dem_required(config),
    )


def store_dem(config, signature):
    """Сохраняет растры рельефа в кэш."""
    return _store(config, "dem", signature, DEM_FILES + DEM_EXTRA_FILES, config.raster_dir)


def restore_isolines(config, signature):
    """Возвращает готовые изолинии из кэша (True — удалось)."""
    return _restore(
        config, "isolines", signature,
        ISOLINES_FILES, config.vector_dir, ("isolines_regular.sqlite",),
    )


def store_isolines(config, signature):
    """Сохраняет изолинии в кэш."""
    return _store(config, "isolines", signature, ISOLINES_FILES, config.vector_dir)
