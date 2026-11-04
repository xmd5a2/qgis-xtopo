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
# Маркер репроекции: без него повторная сборка DEM не найдёт свой кэш.
DEM_EXTRA_FILES = ("merged_dem_3857.tif.sha256",)
ISOLINES_FILES = (
    "isolines_regular.sqlite",
    "isolines_glacier.sqlite",
)

# Сколько последних записей кэша оставляем в каждой группе.
KEEP_ENTRIES = 3

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
    """Ключ кэша рельефа: версия плагина, область, параметры и тайлы DEM."""
    if source_digest is None:
        source_digest = dem_source_digest(config)
    parts = [
        plugin_version(),
        bbox.as_csv() if hasattr(bbox, "as_csv") else str(bbox),
        config.terrain_resample_method,
        config.generate_terrain_hillshade_slope,
        source_digest,
    ]
    return _digest(parts)


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
            shutil.copy2(item, target_dir / name)
    return True


def _store(config, group, signature, names, source_dir):
    target = _group_dir(config, group, signature)
    target.mkdir(parents=True, exist_ok=True)
    stored = False
    for name in names:
        item = source_dir / name
        if not item.exists():
            continue
        shutil.copy2(item, target / name)
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
