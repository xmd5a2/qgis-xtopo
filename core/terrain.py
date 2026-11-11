import gzip
import hashlib
import math
import os
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .bbox import BBox
from . import dem_sources, i18n, sonny, terrain_cache

CHUNK = 256 * 1024

# Сколько тайлов SRTM качаем одновременно.
DEM_DOWNLOAD_WORKERS = 4

# Запас вокруг области для растровых наборов: тайлы Copernicus сдвинуты
# относительно целых градусов, и без запаса у края остаётся полоса nodata.
COG_PADDING = 0.002

# Предел размера окна DEM (пикселей).  Для 3DEP 10 м на большой охват
# окно иначе занимает гигабайты; разрешение в этом случае немного
# снижается, а сообщение об этом выводится в журнал.
MAX_DEM_PIXELS = 120_000_000

# Запас вокруг тайла при нарезке DEM на изолинии.  Алгоритм GDAL считает
# контур по соседним ячейкам, и без запаса линия у края тайла обрывается
# и загибается.  В результат попадает только ядро тайла, поэтому куски
# соседних тайлов не дублируются и стыкуются по границе.
CONTOUR_HALO = 32

# Начиная с этого числа пикселей DEM нарезка на тайлы даёт выигрыш:
# при меньшем размере нарезка стоит дороже самого расчёта.
CONTOUR_TILE_PIXELS = 4_000_000

# Сколько пикселей должно приходиться на один тайл: окно держится в
# памяти, а нахлёст тайла съедает часть выигрыша от параллельности.
CONTOUR_TILE_MAX_PIXELS = 6_000_000

# Сколько долей ядер можно занять расчётом контуров: больше потоков —
# быстрее, но дальше потолок, GEOS держит GIL, и лишние потоки мешают.
CONTOUR_TILE_WORKERS_RATIO = 0.75


def _gdal():
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr, osr


def _numpy():
    import numpy

    return numpy


def srtm_tile_names(bbox):
    return dem_sources.tile_names(bbox)


def _tile_directory(tile):
    return tile[:3]


def _cached_tile(cache_dir, tile):
    if not cache_dir:
        return None
    return Path(cache_dir) / _tile_directory(tile) / f"{tile}.hgt"


class _Speed:
    """Скорость загрузки с усреднением по окну времени."""

    def __init__(self, window=0.5):
        self.window = window
        self.total = 0
        self.speed = 0.0
        self._mark = 0
        self._time = time.monotonic()
        self._lock = threading.Lock()

    def add(self, size):
        """Добавляет байты. True, если скорость пересчитана."""
        with self._lock:
            self.total += size
            now = time.monotonic()
            elapsed = now - self._time
            if elapsed < self.window:
                return False
            current = (self.total - self._mark) / elapsed
            self.speed = current if not self.speed else 0.5 * self.speed + 0.5 * current
            self._mark = self.total
            self._time = now
            return True


def download_srtm(bbox, destination, progress=None, cancel=None, cache_dir=None,
                  status=None, tiles=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    tiles = list(tiles) if tiles else srtm_tile_names(bbox)
    meter = _Speed()
    pending = []
    for tile in tiles:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        target = destination / f"{tile}.hgt"
        if target.exists():
            continue
        for existing in destination.glob(f"{tile}.*"):
            if existing.suffix.lower() in (".tif", ".hgt"):
                break
        else:
            cached = _cached_tile(cache_dir, tile)
            if cached is not None and cached.exists():
                shutil.copy2(cached, target)
                if progress:
                    progress(f"Using cached DEM tile {tile}")
                continue
            pending.append((tile, target, cached))
    if not pending:
        return tiles

    def fetch(item):
        tile, target, cached = item
        url = (
            "https://s3.amazonaws.com/elevation-tiles-prod/skadi/"
            f"{_tile_directory(tile)}/{tile}.hgt.gz"
        )
        if progress:
            progress(f"Downloading {tile}")
        temporary = destination / f"{tile}.hgt.gz"
        try:
            with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("DEM download cancelled")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if meter.add(len(chunk)) and status:
                        status(
                            f"Загрузка рельефа: {meter.total / 1048576:.1f} МБ"
                            f" · {meter.speed / 1048576:.1f} МБ/с"
                        )
            with gzip.open(temporary, "rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        except InterruptedError:
            raise
        except Exception as exc:
            if progress:
                progress(f"WARNING: cannot download {tile}: {exc}")
            target.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        if cached is not None and target.exists():
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, cached)

    workers = min(DEM_DOWNLOAD_WORKERS, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, item) for item in pending]
        for future in as_completed(futures):
            future.result()
    return tiles


def copy_dem_tiles(bbox, source_dir, destination, progress=None):
    source_dir = Path(source_dir).expanduser()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for tile in srtm_tile_names(bbox):
        for extension in (".tif", ".hgt", ".zip"):
            candidate = source_dir / f"{tile}{extension}"
            if candidate.exists():
                shutil.copy2(candidate, destination / candidate.name)
                if progress:
                    progress(f"Using {candidate.name}")
                break
        else:
            if progress:
                progress(f"WARNING: DEM tile not found: {tile}")


def _padded_bbox(bbox, padding=COG_PADDING):
    """Область с запасом вокруг: у края не остаётся полосы nodata."""
    return BBox(
        max(-180.0, bbox.lon_min - padding),
        max(-90.0, bbox.lat_min - padding),
        min(180.0, bbox.lon_max + padding),
        min(90.0, bbox.lat_max + padding),
    )


def _cached_cog(cache_dir, source_key, bbox):
    if not cache_dir:
        return None
    folder = Path(cache_dir) / "cog" / source_key
    name = (
        f"v1_{bbox.lon_min:.6f}_{bbox.lat_min:.6f}_"
        f"{bbox.lon_max:.6f}_{bbox.lat_max:.6f}.tif"
    )
    return folder / name


def _cog_sources(source_key, bbox, progress=None, cancel=None):
    """Ссылки на растровые источники набора для области."""
    item = dem_sources.source(source_key)
    try:
        items = dem_sources.stac_items(
            item.collection,
            (bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - каталог STAC отвечает медленно
        items = []
    if source_key == "3dep":
        # В коллекции есть и 1″, и 1/3″; нужен более подробный слой.
        preferred = [entry for entry in items
                     if entry.get("id", "").endswith("-13")]
        if preferred:
            items = preferred
    if progress:
        progress(f"Найдено тайлов {item.short}: {len(items)}")
    sources = []
    if source_key == "cop30":
        # Открытое зеркало в AWS неполное, часть тайлов лежит только в
        # каталоге STAC: сначала пробуем адрес без подписи, а для
        # остальных подписываем ссылку из каталога.
        entries = []
        for entry in items:
            tile = dem_sources.item_tile(entry)
            if tile is None:
                continue
            entries.append((tile, dem_sources.item_asset_href(entry)))
        existing = dem_sources.cop30_existing_tiles(
            [tile for tile, _href in entries]
        )
        for tile, href in entries:
            if tile in existing:
                url = dem_sources.aws_tile_url("cop30", *tile)
            elif href:
                url = dem_sources.sign_pc(href)
            else:
                continue
            sources.append("/vsicurl/" + url)
        if not sources:
            # Каталог STAC недоступен: остаются тайлы открытого зеркала.
            for tile in dem_sources.cop30_existing_tiles(
                dem_sources.tiles_for_bbox(bbox)
            ):
                url = dem_sources.aws_tile_url("cop30", *tile)
                sources.append("/vsicurl/" + url)
        return sources
    for entry in items:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        href = dem_sources.item_asset_href(entry)
        if not href:
            continue
        url = dem_sources.sign_pc(href)
        sources.append("/vsicurl/" + url)
    return sources


def _native_resolution(path):
    """Шаг сетки источника в градусах (None — не удалось открыть)."""
    gdal, _, _ = _gdal()
    dataset = gdal.Open(str(path))
    if dataset is None:
        return None
    try:
        transform = dataset.GetGeoTransform()
        spatial_ref = dataset.GetSpatialRef()
        step = max(abs(transform[1]), abs(transform[5]))
        if spatial_ref is not None and spatial_ref.IsGeographic():
            return step
        return step / 111320.0
    finally:
        dataset = None


def _warp_callback(cancel, status, label):
    """Обратный вызов GDAL: отмена и проценты в строке состояния."""
    state = {"last": 0.0}

    def callback(complete, message, data):
        if cancel is not None and cancel.is_set():
            return 0
        now = time.monotonic()
        if status and now - state["last"] >= 0.5:
            state["last"] = now
            status(f"Загрузка рельефа {label}: {complete:.0%}")
        return 1

    return callback


def download_cog_dem(source_key, bbox, destination, progress=None, cancel=None,
                     cache_dir=None, status=None):
    """Скачивает окно растрового набора (COG) в локальный GeoTIFF.

    Целиком многогигабайтные тайлы не качаются: GDAL читает нужные блоки
    по HTTP Range.  Результат — один файл на область в EPSG:4326.
    """
    gdal, _, _ = _gdal()
    item = dem_sources.source(source_key)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"dem_{source_key}.tif"
    cached = _cached_cog(cache_dir, source_key, bbox)
    if cached is not None and cached.exists() and cached.stat().st_size > 0:
        shutil.copy2(cached, target)
        if progress:
            progress(f"Использован кэш рельефа: {item.short}")
        return target

    padded = _padded_bbox(bbox)
    if progress:
        progress(f"Загрузка рельефа {item.short}")
    sources = _cog_sources(source_key, bbox, progress=progress, cancel=cancel)
    if not sources:
        raise RuntimeError(
            f"Нет данных {item.short} для выбранного охвата"
        )

    resolution = _native_resolution(sources[0]) or item.native_resolution
    span_x = padded.lon_max - padded.lon_min
    span_y = padded.lat_max - padded.lat_min
    width = max(1, int(math.ceil(span_x / resolution)))
    height = max(1, int(math.ceil(span_y / resolution)))
    if width * height > MAX_DEM_PIXELS:
        factor = math.sqrt(width * height / MAX_DEM_PIXELS)
        width = max(1, int(width / factor))
        height = max(1, int(height / factor))
        reduced = max(span_x / width, span_y / height)
        if progress:
            progress(
                "Большой охват: разрешение снижено до "
                f"~{reduced * 111320:.0f} м"
            )

    temporary = target.with_name(target.name + ".part")
    options = gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:4326",
        outputBounds=(padded.lon_min, padded.lat_min,
                      padded.lon_max, padded.lat_max),
        width=width,
        height=height,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Int16,
        dstNodata=-32768,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES", "TILED=YES"],
        multithread=True,
        callback=_warp_callback(cancel, status, item.short),
    )
    result = gdal.Warp(str(temporary), sources, options=options)
    if result is None:
        temporary.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        raise RuntimeError(f"Не удалось собрать рельеф {item.short}")
    result = None
    temporary.replace(target)
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary_cache = cached.with_name(cached.name + ".tmp")
        shutil.copy2(target, temporary_cache)
        temporary_cache.replace(cached)
    return target


def download_dem(source_key, bbox, destination, progress=None, cancel=None,
                 cache_dir=None, status=None):
    """Скачивает рельеф выбранного источника для области."""
    if source_key == dem_sources.MANUAL_SOURCE:
        raise ValueError("Manual DEM source does not download tiles")
    if source_key == "srtm":
        return download_srtm(
            bbox, destination, progress=progress, cancel=cancel,
            cache_dir=cache_dir, status=status,
        )
    return download_cog_dem(
        source_key, bbox, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status,
    )


# --- Мозаика рельефа ----------------------------------------------------

# Порядок наборов в мозаике: чем позже набор в списке, тем выше его
# приоритет — данные более приоритетного набора перекрывают остальные.
AUTO_ORDER = ("srtm", dem_sources.AUTO_BACKGROUND, "sonny", "3dep")


def _cells_window(cells):
    """Прямоугольник, покрывающий тайлы 1°×1°."""
    if not cells:
        return None
    lats = [cell[0] for cell in cells]
    lons = [cell[1] for cell in cells]
    return BBox(min(lons), min(lats), max(lons) + 1.0, max(lats) + 1.0)


def _mosaic_grid(span_x, span_y, resolutions):
    """Шаг сетки мозаики: самый подробный, который влезает в бюджет.

    Возвращает тройку ``(шаг, ширина, высота)``.  Если ни один шаг не
    помещается в предел по числу пикселей, берётся самый грубый и он
    дополнительно укрупняется.
    """
    candidates = sorted({value for value in resolutions if value > 0})
    if not candidates:
        candidates = [1.0 / 3600.0]
    for resolution in candidates:
        width = max(1, int(math.ceil(span_x / resolution)))
        height = max(1, int(math.ceil(span_y / resolution)))
        if width * height <= MAX_DEM_PIXELS:
            return resolution, width, height
    resolution = candidates[-1]
    width = max(1, int(math.ceil(span_x / resolution)))
    height = max(1, int(math.ceil(span_y / resolution)))
    factor = math.sqrt(width * height / MAX_DEM_PIXELS)
    width = max(1, int(width / factor))
    height = max(1, int(height / factor))
    return span_x / width, width, height


def _layer_options(gdal, bounds, width, height, cancel, status, label):
    """Параметры приведения растра к общей сетке мозаики."""
    return gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:4326",
        outputBounds=(bounds.lon_min, bounds.lat_min,
                      bounds.lon_max, bounds.lat_max),
        width=width,
        height=height,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Int16,
        dstNodata=-32768,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES", "TILED=YES"],
        multithread=True,
        callback=_warp_callback(cancel, status, label),
    )


def _write_layer(gdal, sources, bounds, width, height, destination, name,
                 cancel, status, label):
    """Приводит набор источников к общей сетке и пишет слой мозаики.

    Источники, которые не открылись (отсутствующий тайл набора),
    пропускаются: в мозаике останется пустое место.
    """
    datasets = []
    for source in sources:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        try:
            dataset = gdal.Open(source)
        except Exception:  # noqa: BLE001 - нет данных за набором
            dataset = None
        if dataset is not None:
            datasets.append(dataset)
    if not datasets:
        return None
    target = Path(destination) / name
    temporary = target.with_name(target.name + ".part")
    options = _layer_options(gdal, bounds, width, height, cancel, status, label)
    try:
        result = gdal.Warp(str(temporary), datasets, options=options)
    finally:
        for dataset in datasets:
            dataset = None
    if result is None:
        temporary.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        raise RuntimeError(f"Не удалось собрать рельеф {label}")
    result = None
    temporary.replace(target)
    return target


def _cog_layer(source_key, cells, bounds, width, height, destination,
                progress=None, cancel=None, status=None):
    """Слой мозаики из растрового набора (COG читается по HTTP Range)."""
    gdal, _, _ = _gdal()
    item = dem_sources.source(source_key)
    window = _cells_window(cells)
    sources = _cog_sources(source_key, window, progress=progress, cancel=cancel)
    if not sources:
        return None
    return _write_layer(
        gdal, sources, bounds, width, height, destination,
        f"layer_{source_key}.tif", cancel, status, item.short,
    )


def _srtm_layer(cells, bounds, width, height, destination, cache_dir=None,
                 progress=None, cancel=None, status=None):
    """Слой мозаики из тайлов SRTM."""
    gdal, _, _ = _gdal()
    names = [dem_sources.tile_name(*cell) for cell in sorted(cells)]
    if not names:
        return None
    download_srtm(
        None, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status, tiles=names,
    )
    files = [str(Path(destination) / f"{name}.hgt") for name in names]
    files = [name for name in files if Path(name).exists()]
    if not files:
        return None
    return _write_layer(
        gdal, files, bounds, width, height, destination,
        "layer_srtm.tif", cancel, status, "SRTM v3",
    )


def _sonny_layers(cells, catalogue, tile_dir, bounds, width, height,
                  destination, progress=None, cancel=None, status=None):
    """Слои мозаики из наборов Sonny DTM.

    Наборы разной подробности кладутся отдельными слоями: более
    подробный идёт позже и перекрывает более грубый.
    """
    gdal, _, _ = _gdal()
    assignment = sonny.assign_datasets(catalogue, cells)
    groups = {}
    for cell, dataset_key in assignment.items():
        groups.setdefault(dataset_key, set()).add(cell)
    layers = []
    for dataset in sorted(sonny.DATASETS, key=lambda item: -item.rank):
        dataset_cells = groups.get(dataset.key)
        if not dataset_cells:
            continue
        paths = sonny.tile_paths(
            catalogue, tile_dir, dataset.key, dataset_cells,
            progress=progress, cancel=cancel,
        )
        files = [str(path) for _cell, path in paths]
        if not files:
            continue
        layer = _write_layer(
            gdal, files, bounds, width, height, destination,
            f"layer_sonny_{dataset.key}.tif", cancel, status,
            i18n.tr(dataset.title),
        )
        if layer is not None:
            layers.append(layer)
    return layers


def _mosaic_resolution(groups):
    """Шаги сетки наборов, входящих в мозаику."""
    resolutions = []
    for key in ("3dep", "cop30", "srtm"):
        if groups.get(key):
            resolutions.append(dem_sources.source(key).native_resolution)
    for dataset_key in groups.get("sonny", ()):
        dataset = sonny.DATASET_BY_KEY.get(dataset_key)
        if dataset is not None:
            resolutions.append(dataset.native_resolution)
    return resolutions


def download_auto_dem(bbox, destination, progress=None, cancel=None,
                      cache_dir=None, status=None, availability=None,
                      sonny_catalogue=None, sonny_tiles_dir=None,
                      priority=None, used=None):
    """Собирает мозаику рельефа в режиме «Автоматически».

    Для каждого тайла 1°×1° выбирается лучший доступный набор: USGS
    3DEP, затем Sonny DTM, фон — Copernicus GLO-30, в крайнем случае
    SRTM.  Наборы кладутся в один растр в порядке приоритета, поэтому
    данные более приоритетного набора перекрывают менее приоритетный.

    ``priority`` задаёт свой порядок выбора наборов: для набора Sonny
    DTM это ``(Sonny, Copernicus, SRTM)``.  В ``used`` дописываются
    ключи наборов, вошедших в мозаику, — для строки атрибуции.
    """
    gdal, _, _ = _gdal()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    layers_dir = destination / "_layers"
    if layers_dir.exists():
        shutil.rmtree(layers_dir, ignore_errors=True)
    layers_dir.mkdir(parents=True, exist_ok=True)

    catalogue = sonny.load_catalogue(sonny_catalogue)
    sonny_cells = set(sonny.catalogue_tiles(sonny_catalogue))
    if availability is None:
        availability = dem_sources.auto_coverage(
            bbox, sonny_tiles=sonny_cells,
            keys=priority or dem_sources.AUTO_PRIORITY,
        )
    plan = dem_sources.auto_plan(
        bbox, availability, sonny_cells, priority=priority
    )
    if not plan:
        raise RuntimeError("Нет данных рельефа для выбранного охвата")

    groups = {}
    for cell, key in plan.items():
        groups.setdefault(key, set()).add(cell)
    if progress:
        parts = []
        for key in dem_sources.AUTO_PRIORITY:
            if groups.get(key):
                parts.append(f"{dem_sources.source(key).short}: "
                             f"{len(groups[key])}")
        progress("Мозаика рельефа — " + ", ".join(parts))

    padded = _padded_bbox(bbox)
    span_x = padded.lon_max - padded.lon_min
    span_y = padded.lat_max - padded.lat_min
    sonny_datasets = set()
    if groups.get("sonny"):
        sonny_datasets = set(sonny.assign_datasets(catalogue, groups["sonny"]))
    resolution_groups = dict(groups)
    resolution_groups["sonny"] = sonny_datasets
    resolution, width, height = _mosaic_grid(
        span_x, span_y, _mosaic_resolution(resolution_groups)
    )
    if progress:
        progress(
            f"Разрешение мозаики: ~{resolution * 111320:.0f} м "
            f"({width}×{height})"
        )

    layers = []
    built = set()
    for key in AUTO_ORDER:
        cells = groups.get(key)
        if not cells:
            continue
        if key == "srtm":
            layer = _srtm_layer(
                cells, padded, width, height, layers_dir, cache_dir=cache_dir,
                progress=progress, cancel=cancel, status=status,
            )
            made = [layer] if layer else []
        elif key == "sonny":
            made = _sonny_layers(
                cells, catalogue, sonny_tiles_dir, padded, width, height,
                layers_dir, progress=progress, cancel=cancel, status=status,
            )
        else:
            layer = _cog_layer(
                key, cells, padded, width, height, layers_dir,
                progress=progress, cancel=cancel, status=status,
            )
            made = [layer] if layer else []
        if not made:
            continue
        layers.extend(made)
        built.add(key)
        if used is not None and key not in used:
            used.append(key)

    # Набор не удалось получить (каталог STAC недоступен, тайла нет в
    # зеркале): его тайлы добираются SRTM.  Слой ставится в начало —
    # приоритет у SRTM самый низкий.
    failed = set(groups) - built
    if failed and "srtm" not in built:
        cells = set()
        for key in failed:
            cells |= groups[key]
        layer = _srtm_layer(
            cells, padded, width, height, layers_dir, cache_dir=cache_dir,
            progress=progress, cancel=cancel, status=status,
        )
        if layer:
            layers.insert(0, layer)
            built.add("srtm")
            if used is not None and "srtm" not in used:
                used.append("srtm")

    if not layers:
        raise RuntimeError("Нет данных рельефа для выбранного охвата")

    target = destination / "dem_auto.tif"
    if len(layers) == 1:
        # Один набор — слой уже лежит на нужной сетке, склеивать нечего.
        shutil.move(str(layers[0]), str(target))
        shutil.rmtree(layers_dir, ignore_errors=True)
        return target
    temporary = target.with_name(target.name + ".part")
    options = _layer_options(gdal, padded, width, height, cancel, status,
                             "мозаика")
    result = gdal.Warp(str(temporary), [str(path) for path in layers],
                       options=options)
    if result is None:
        temporary.unlink(missing_ok=True)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        raise RuntimeError("Не удалось собрать мозаику рельефа")
    result = None
    temporary.replace(target)
    shutil.rmtree(layers_dir, ignore_errors=True)
    return target


def download_sonny_dem(bbox, destination, progress=None, cancel=None,
                       cache_dir=None, status=None, availability=None,
                       sonny_catalogue=None, sonny_tiles_dir=None,
                       used=None):
    """Скачивает рельеф Sonny DTM, а недостающие тайлы — фоном Copernicus.

    Набор выбран один, но тайлы в нём могут быть разной подробности:
    0,5″ для Австрии и Швейцарии, 1″ и 3″ для остальной Европы.
    """
    return download_auto_dem(
        bbox, destination, progress=progress, cancel=cancel,
        cache_dir=cache_dir, status=status, availability=availability,
        sonny_catalogue=sonny_catalogue, sonny_tiles_dir=sonny_tiles_dir,
        priority=(dem_sources.SONNY_SOURCE, dem_sources.AUTO_BACKGROUND,
                  "srtm"),
        used=used,
    )


def _write_dem_attribution(config, used):
    """Записывает в проект точную атрибуцию наборов мозаики.

    Проект создаётся до рельефа, поэтому атрибуция сначала общая; после
    сборки мозаики известно, какие наборы действительно вошли.
    """
    from . import project

    attribution = dem_sources.attribution_for_keys(used)
    if not attribution:
        return
    path = project.project_file(config)
    if not path.exists():
        return
    project.rewrite_project_archive(
        path, path, config.project_name, dem_attribution=attribution
    )


def _clear_terrain_input(config):
    """Убирает прошлые тайлы: иначе в сборку попадут данные другого набора."""
    try:
        entries = list(config.terrain_input_dir.iterdir())
    except OSError:
        return
    for path in entries:
        if path.is_file() and path.suffix.lower() in (".tif", ".hgt", ".zip", ".vrt"):
            path.unlink(missing_ok=True)


def _write_raster(path, array, template):
    gdal, _, _ = _gdal()
    driver = gdal.GetDriverByName("GTiff")
    output = driver.Create(
        str(path),
        array.shape[1],
        array.shape[0],
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    output.SetGeoTransform(template.GetGeoTransform())
    output.SetProjection(template.GetProjection())
    band = output.GetRasterBand(1)
    band.WriteArray(array)
    band.FlushCache()
    output = None


def _read_band(path):
    gdal, _, _ = _gdal()
    dataset = gdal.Open(str(path))
    if dataset is None:
        raise RuntimeError(f"Cannot open raster: {path}")
    band = dataset.GetRasterBand(1)
    array = band.ReadAsArray().astype("float32")
    return dataset, array


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_dem(config, bbox, progress=None, cancel=None):
    gdal, _, _ = _gdal()
    tiles = sorted(
        [path for path in config.terrain_input_dir.iterdir() if path.suffix.lower() in (".tif", ".hgt")]
    )
    if not tiles:
        raise RuntimeError(
            f"No DEM tiles found in {config.terrain_input_dir}. "
            "Enable SRTM download or provide a DEM directory."
        )
    source = str(tiles[0])
    vrt_path = None
    if len(tiles) > 1:
        if progress:
            progress(f"Merging {len(tiles)} DEM tile(s)")
        vrt_path = config.temp_dir / "input_terrain.vrt"
        source = gdal.BuildVRT(str(vrt_path), [str(path) for path in tiles])
        if source is None:
            raise RuntimeError("Failed to build a VRT from DEM tiles")
    crop = config.project_dir / "crop.geojson"
    merged = config.raster_dir / "merged_dem.tif"
    options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=str(crop),
        cropToCutline=True,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(merged), source, options=options)
    source = None
    if vrt_path is not None:
        vrt_path.unlink(missing_ok=True)
    if result is None:
        raise RuntimeError("Failed to crop and merge DEM tiles")
    result = None

    merged_3857 = config.raster_dir / "merged_dem_3857.tif"
    cache_marker = merged_3857.with_suffix(merged_3857.suffix + ".sha256")
    source_hash = _file_sha256(merged)
    cache_key = f"EPSG:3857:{source_hash}"
    cached_key = None
    if merged_3857.exists() and cache_marker.exists():
        try:
            cached_key = cache_marker.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            pass
    if cached_key == cache_key:
        if progress:
            progress("Using cached reprojected DEM (EPSG:3857)")
        return merged

    if progress:
        progress("Reprojecting DEM to EPSG:3857")
    # Маркер удаляем до записи: прерванный Warp не должен оставить
    # действительный кэш для частично записанного растра.
    cache_marker.unlink(missing_ok=True)
    options = gdal.WarpOptions(
        dstSRS="EPSG:3857",
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(merged_3857), str(merged), options=options)
    if result is None:
        raise RuntimeError("Failed to reproject DEM")
    result = None
    cache_marker.write_text(cache_key + "\n", encoding="ascii")
    return merged


def generate_hillshade_slope(config, bbox, progress=None, cancel=None):
    gdal, _, _ = _gdal()
    numpy = _numpy()
    merged = config.raster_dir / "merged_dem.tif"
    if not merged.exists():
        raise RuntimeError("merged_dem.tif is missing")

    if progress:
        progress("Generating slope")
    # Сначала удваиваем разрешение DEM с интерполяцией: уклон получается
    # детальнее, чем при интерполяции уже готового растра уклона.
    dem_ds = gdal.Open(str(merged))
    dem_width, dem_height = dem_ds.RasterXSize, dem_ds.RasterYSize
    dem_ds = None
    dem_x2 = config.raster_dir / "merged_dem_x2.tif"
    options = gdal.WarpOptions(
        width=dem_width * 2,
        height=dem_height * 2,
        resampleAlg=config.terrain_resample_method,
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(dem_x2), str(merged), options=options)
    if result is None:
        raise RuntimeError("Failed to upscale DEM for slope")
    result = None
    slope = config.raster_dir / "slope.tif"
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        slopeFormat="degree",
        scale=111120,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.DEMProcessing(str(slope), str(dem_x2), "slope", options=options)
    if result is None:
        raise RuntimeError("Failed to generate slope")
    result = None
    dem_x2.unlink(missing_ok=True)

    template, slope_array = _read_band(slope)
    slope_cut = slope_array * (slope_array > 1.04)
    height, width = slope_cut.shape
    slope_cut_path = config.raster_dir / "slope_cut.tif"
    _write_raster(slope_cut_path, slope_cut, template)

    if progress:
        progress("Upscaling slope")
    slope_up = config.raster_dir / "slope_upscaled.tif"
    options = gdal.WarpOptions(
        width=width * 3,
        height=height * 3,
        resampleAlg=config.terrain_resample_method,
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(slope_up), str(slope_cut_path), options=options)
    if result is None:
        raise RuntimeError("Failed to upscale slope")
    result = None
    slope.unlink(missing_ok=True)
    slope_cut_path.unlink(missing_ok=True)

    if progress:
        progress("Generating hillshade")
    downscaled = config.raster_dir / "merged_dem_downscaled.tif"
    options = gdal.WarpOptions(
        width=width // 2,
        height=height // 2,
        resampleAlg=config.terrain_resample_method,
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(downscaled), str(merged), options=options)
    if result is None:
        raise RuntimeError("Failed to downscale DEM")
    result = None

    upscaled = config.raster_dir / "merged_dem_upscaled.tif"
    options = gdal.WarpOptions(
        width=width * 3,
        height=height * 3,
        resampleAlg=config.terrain_resample_method,
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(upscaled), str(downscaled), options=options)
    if result is None:
        raise RuntimeError("Failed to upscale DEM")
    result = None
    downscaled.unlink(missing_ok=True)

    hillshade = config.raster_dir / "hillshade.tif"
    # Как gdaldem hillshade по умолчанию: солнце 315°/45°, без вертикального
    # преувеличения. Многопроходная отмывка с altitude=70 и zFactor=2 давала
    # почти белое полотно, на котором рельеф не читался.
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        azimuth=315,
        altitude=45,
        zFactor=1,
        scale=111120,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.DEMProcessing(str(hillshade), str(upscaled), "hillshade", options=options)
    if result is None:
        raise RuntimeError("Failed to generate hillshade")
    result = None

    hillshade_template, hillshade_array = _read_band(hillshade)
    # 10 % подсветки: самые глубокие тени не проваливаются в чёрный.
    composite = numpy.clip(255.0 * (0.10 + 0.90 * (hillshade_array / 255.0)), 0, 255)
    composite_path = config.raster_dir / "hillshade_composite.tif"
    _write_raster(composite_path, composite, hillshade_template)
    hillshade.unlink(missing_ok=True)
    return composite_path


def _subtract_water_from_layer(layer, water_paths, target_srs):
    _, ogr, osr = _gdal()
    if target_srs is not None:
        target_srs = target_srs.Clone()
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    water = None
    for path in water_paths:
        if not path.exists():
            continue
        dataset = ogr.Open(str(path))
        if dataset is None:
            continue
        for water_layer in dataset:
            source_srs = water_layer.GetSpatialRef()
            transform = None
            if source_srs is not None:
                source_srs = source_srs.Clone()
                source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if source_srs is not None and target_srs is not None and not source_srs.IsSame(target_srs):
                transform = osr.CoordinateTransformation(source_srs, target_srs)
            for feature in water_layer:
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                    continue
                geometry = geometry.Clone()
                if transform is not None:
                    geometry.Transform(transform)
                water = geometry if water is None else water.Union(geometry)
        dataset = None
    if water is None:
        return 0

    def line_parts(geometry):
        if ogr.GT_Flatten(geometry.GetGeometryType()) == ogr.wkbLineString:
            return [geometry.Clone()]
        parts = []
        for index in range(geometry.GetGeometryCount()):
            parts.extend(line_parts(geometry.GetGeometryRef(index)))
        return parts

    changed = 0
    updates = []
    layer.ResetReading()
    feature = layer.GetNextFeature()
    while feature is not None:
        geometry = feature.GetGeometryRef()
        if geometry is not None and not geometry.IsEmpty() and geometry.Intersects(water):
            clipped = geometry.Difference(water)
            parts = [] if clipped is None or clipped.IsEmpty() else line_parts(clipped)
            updates.append((feature.GetFID(), parts))
            changed += 1
        feature = layer.GetNextFeature()
    for fid, parts in updates:
        feature = layer.GetFeature(fid)
        if not parts:
            layer.DeleteFeature(fid)
            continue
        feature.SetGeometry(parts[0])
        layer.SetFeature(feature)
        for part in parts[1:]:
            additional = feature.Clone()
            additional.SetGeometry(part)
            additional.SetFID(-1)
            layer.CreateFeature(additional)
    return changed


def _water_mask_paths(config):
    """Слои воды, по которым режутся изолинии.

    Берётся и постоянная вода, и вода с ``intermittent=yes`` (пересыхающие
    озёра и водохранилища): по сухому озеру горизонтали не нужны, а без
    этого срезания контуры шли по площадиrelation'а вроде Chott el Djerid
    (relation/3969167).  Растворённый слой предпочтительнее исходного.
    """
    paths = []
    groups = (
        ("water_dissolved.sqlite", "water.sqlite"),
        ("water_intermittent_dissolved.sqlite", "water_intermittent.sqlite"),
    )
    for names in groups:
        for name in names:
            path = config.vector_dir / name
            if path.exists():
                paths.append(path)
                break
    return paths


def _contour_workers(tiles):
    """Сколько потоков считает контуры тайлов.

    Занимаем три четверти ядер: замер на реальном рельефе показывает,
    что дальше потолок — GEOS держит GIL, и лишние потоки мешают.
    """
    available = os.cpu_count() or 2
    limit = max(1, int(available * CONTOUR_TILE_WORKERS_RATIO))
    return max(1, min(tiles, limit))


def _contour_tile_side(pixels):
    """Сколько тайлов по стороне нарезать DEM.

    Тайлов тем больше, чем больше растр: у большого DEM выигрыш от
    параллельности перекрывает работу на нахлёст, а тайл в памяти
    должен оставаться небольшим.
    """
    if pixels < CONTOUR_TILE_PIXELS:
        return 1
    side = 2
    while pixels / (side * side) > CONTOUR_TILE_MAX_PIXELS:
        side += 1
    return side


def _contour_tiles(raster, side, halo=CONTOUR_HALO):
    """Окна нарезки DEM: ядро тайла и окно с запасом вокруг него."""
    width = raster.RasterXSize
    height = raster.RasterYSize
    step_x = width // side
    step_y = height // side
    tiles = []
    for row in range(side):
        for column in range(side):
            xoff = column * step_x
            yoff = row * step_y
            xsize = step_x if column < side - 1 else width - xoff
            ysize = step_y if row < side - 1 else height - yoff
            window_xoff = max(0, xoff - halo)
            window_yoff = max(0, yoff - halo)
            tiles.append({
                "core": (xoff, yoff, xsize, ysize),
                "window": (
                    window_xoff,
                    window_yoff,
                    min(width - window_xoff, xsize + 2 * halo),
                    min(height - window_yoff, ysize + 2 * halo),
                ),
            })
    return tiles


def _core_box(transform, core):
    """Границы ядра тайла в координатах карты."""
    xoff, yoff, xsize, ysize = core
    corners = [
        (xoff, yoff),
        (xoff + xsize, yoff),
        (xoff, yoff + ysize),
        (xoff + xsize, yoff + ysize),
    ]
    xs = []
    ys = []
    for x, y in corners:
        xs.append(transform[0] + x * transform[1] + y * transform[2])
        ys.append(transform[3] + x * transform[4] + y * transform[5])
    return min(xs), max(xs), min(ys), max(ys)


def _clip_to_core(geometry, box):
    """Части линии, попавшие в ядро тайла.

    Линия целиком внутри ядра остаётся как есть; пересекающая границу
    обрезается по ней, поэтому кусок соседнего тайла продолжается с
    той же точки и шва не видно.
    """
    if geometry is None or geometry.IsEmpty():
        return []
    envelope = geometry.GetEnvelope()
    if (envelope[0] >= box[0] and envelope[1] <= box[1]
            and envelope[2] >= box[2] and envelope[3] <= box[3]):
        return [geometry]
    if (envelope[1] < box[0] or envelope[0] > box[1]
            or envelope[3] < box[2] or envelope[2] > box[3]):
        # Ядро линия не задевает — обрезать нечего.
        return []
    from osgeo import ogr, osr

    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint(box[0], box[2])
    ring.AddPoint(box[1], box[2])
    ring.AddPoint(box[1], box[3])
    ring.AddPoint(box[0], box[3])
    ring.AddPoint(box[0], box[2])
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    try:
        clipped = geometry.Intersection(polygon)
    except Exception:  # noqa: BLE001 - битая геометрия не должна ронять сборку
        return []
    return _geometry_lines(clipped) if clipped is not None else []


def _contour_tile(task):
    """Контуры одного тайла: только то, что попадает в его ядро.

    Тайл считается по окну с запасом, а в результат попадает только
    ядро: так линия у края получается такой же, как при расчёте по
    всему DEM, и тайлы не дублируют друг друга.

    Возвращает слой с отрезками: файл на тайл не пишется — создание
    GeoPackage обходится дороже самой генерации.
    """
    source_path, tile, step = task
    gdal, ogr, osr = _gdal()
    raster = gdal.Open(source_path)
    if raster is None:
        raise RuntimeError(f"Cannot open DEM: {source_path}")
    band = raster.GetRasterBand(1)
    transform = raster.GetGeoTransform()
    xoff, yoff, xsize, ysize = tile["window"]
    # Окно читается прямо в память: копия массива в памяти обошлась бы
    # вдвое дороже по месту.
    memory = gdal.Translate(
        "", source_path, format="MEM",
        srcWin=[xoff, yoff, xsize, ysize],
    )
    memory_band = memory.GetRasterBand(1)
    if band.GetNoDataValue() is not None:
        memory_band.SetNoDataValue(band.GetNoDataValue())
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromWkt(raster.GetProjection())

    contours = ogr.GetDriverByName("Memory").CreateDataSource("contours")
    contour_layer = contours.CreateLayer(
        "isolines", spatial_ref, ogr.wkbLineString)
    # GDAL пишет контуры в поля ID и ELEV: без обоих он падает с
    # «Invalid index».
    contour_layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    contour_layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    gdal.ContourGenerate(
        memory_band, float(step), 0.0, [], 0, 0.0, contour_layer, 0, 1)

    box = _core_box(transform, tile["core"])
    result = ogr.GetDriverByName("Memory").CreateDataSource("tile")
    layer = result.CreateLayer("isolines", spatial_ref, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    written = 0
    for feature in contour_layer:
        geometry = feature.GetGeometryRef()
        for piece in _clip_to_core(geometry, box):
            item = ogr.Feature(layer.GetLayerDefn())
            item.SetField("ID", written)
            item.SetField("ELEV", feature.GetField("ELEV"))
            # Копировать геометрию поточечно нельзя: контуров миллионы
            # точек, и обход через Python занимает больше, чем весь
            # расчёт.  Лишнее измерение убирает сам writer.
            item.SetGeometry(piece.Clone())
            layer.CreateFeature(item)
            item = None
            written += 1
    memory = None
    raster = None
    return result, written


def _merge_contour_tiles(target, tiles):
    """Склеивает контуры тайлов в один слой, перенумеровывая ID."""
    from osgeo import ogr

    written = 0
    definition = target.GetLayerDefn()
    for _index, dataset, _count in tiles:
        source_layer = dataset.GetLayer(0)
        for feature in source_layer:
            item = ogr.Feature(definition)
            for index in range(definition.GetFieldCount()):
                item.SetField(index, feature.GetField(index))
            item.SetField("ID", written)
            item.SetGeometry(feature.GetGeometryRef().Clone())
            target.CreateFeature(item)
            item = None
            written += 1
    return written


def generate_isolines(config, bbox, progress=None, cancel=None):
    gdal, ogr, osr = _gdal()
    source = config.raster_dir / "merged_dem.tif"
    if config.smooth_isolines and (config.raster_dir / "merged_dem_upscaled.tif").exists():
        source = config.raster_dir / "merged_dem_upscaled.tif"
    if not source.exists():
        raise RuntimeError("DEM raster is missing")

    if progress:
        progress("Generating contour lines")
    shape_path = config.temp_dir / "isolines_full.shp"
    driver = ogr.GetDriverByName("ESRI Shapefile")
    dataset = driver.CreateDataSource(str(shape_path))
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromWkt(gdal.Open(str(source)).GetProjection())
    layer = dataset.CreateLayer("isolines", spatial_ref, ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))

    raster = gdal.Open(str(source))
    pixels = raster.RasterXSize * raster.RasterYSize
    side = _contour_tile_side(pixels)
    if side <= 1:
        band = raster.GetRasterBand(1)
        gdal.ContourGenerate(
            band,
            float(config.isolines_step),
            0.0,
            [],
            0,
            0.0,
            layer,
            0,
            1,
        )
        raster = None
    else:
        # DEM режется на тайлы с запасом, контуры считаются параллельно,
        # в результат попадает только середина тайла.
        tiles = _contour_tiles(raster, side, CONTOUR_HALO)
        raster = None
        tasks = [
            (str(source), tile, config.isolines_step)
            for tile in tiles
        ]
        workers = _contour_workers(len(tasks))
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_contour_tile, task): index
                for index, task in enumerate(tasks)
            }
            done = 0
            for future in as_completed(futures):
                results.append((futures[future], *future.result()))
                done += 1
                if progress:
                    progress(f"Contour lines: tile {done} of {len(tasks)}")
                if cancel is not None and cancel.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise InterruptedError("Preparation cancelled")
        results.sort()
        written = _merge_contour_tiles(layer, results)
        if progress:
            progress(f"Contour lines: {written} segments")
    dataset = None

    dataset = ogr.Open(str(shape_path), 1)
    layer = dataset.GetLayer(0)
    # Вода режет контуры до того, как они делятся на ледниковые и обычные,
    # поэтому обе группы не заходят на воду — в том числе ледниковые.
    changed = _subtract_water_from_layer(
        layer, _water_mask_paths(config), spatial_ref
    )
    if progress and changed:
        progress(f"Contour lines cut by water: {changed}")
    layer.SyncToDisk()
    dataset = None

    destination = config.vector_dir / "isolines_full.sqlite"
    options = [
        "-nln", "isolines",
        "-dsco", "SPATIALITE=YES",
        "-lco", "COMPRESS_GEOM=YES",
    ]
    try:
        result = gdal.VectorTranslate(str(destination), str(shape_path), options=options)
    except Exception:
        result = None
    if result is None:
        # -skipfailures отключает пакетную запись и в несколько раз
        # замедляет перенос, поэтому используется только как запасной путь.
        destination.unlink(missing_ok=True)
        try:
            result = gdal.VectorTranslate(
                str(destination), str(shape_path), options=options + ["-skipfailures"]
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to write contour lines: {exc}") from exc
    if result is None:
        raise RuntimeError("Failed to write contour lines")
    result = None
    glacier_destination = _generate_glacier_isolines(config, shape_path, progress)
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        (config.temp_dir / f"isolines_full{suffix}").unlink(missing_ok=True)
    if glacier_destination is None:
        # Ледников нет: все изолинии остаются обычными.
        shutil.copy2(destination, config.vector_dir / "isolines_regular.sqlite")
    return destination


def _geometry_lines(geometry):
    """Двумерные линии из результата пересечения геометрий."""
    from osgeo import ogr

    if geometry is None or geometry.IsEmpty():
        return []
    kind = geometry.GetGeometryType()
    if kind in (ogr.wkbLineString, ogr.wkbLineString25D):
        return [geometry.Clone()]
    if kind in (ogr.wkbMultiLineString, ogr.wkbMultiLineString25D):
        # Части нужно копировать: ссылка GetGeometryRef() принадлежит
        # родительской геометрии, и после её освобождения указатель
        # становится недействительным (падение в GetPointCount).
        return [
            part.Clone()
            for part in (
                geometry.GetGeometryRef(index)
                for index in range(geometry.GetGeometryCount())
            )
            if part is not None and not part.IsEmpty()
        ]
    if kind in (ogr.wkbGeometryCollection, ogr.wkbGeometryCollection25D):
        lines = []
        for index in range(geometry.GetGeometryCount()):
            lines.extend(_geometry_lines(geometry.GetGeometryRef(index)))
        return lines
    return []


def _flat_line(geometry):
    """Копия линии без третьего измерения.

    Точки копируются поштучно только когда геометрия не линейная: у
    контуров их миллионы, и обход через Python занимает больше, чем
    весь остальной расчёт.  Простая линия копируется целиком — лишнее
    измерение убирает сам writer.
    """
    from osgeo import ogr

    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbLineString:
        return geometry.Clone()
    line = ogr.Geometry(ogr.wkbLineString)
    for point in geometry.GetPoints():
        line.AddPoint(point[0], point[1])
    return line


class _PolygonIndex:
    """Пространственный индекс полигонов: равномерная сетка по рамке.

    Нужен, чтобы для каждой изолинии перебирать только те ледники, чья
    рамка её пересекает. Свой фильтр GDAL для слоёв SQLite/SpatiaLite
    работает неверно (часть объектов не попадает в выборку), поэтому
    отбор ведём сами.
    """

    def __init__(self, items, cells=32):
        self.items = items
        self.cells = {}
        if not items:
            return
        minx = min(item[1][0] for item in items)
        maxx = max(item[1][1] for item in items)
        miny = min(item[1][2] for item in items)
        maxy = max(item[1][3] for item in items)
        self.minx = minx
        self.miny = miny
        self.width = (maxx - minx) or 1e-9
        self.height = (maxy - miny) or 1e-9
        self.columns = max(1, int(cells ** 0.5))
        self.rows = max(1, int(cells ** 0.5))
        for index, (_geometry, envelope) in enumerate(items):
            for column in range(self._column(envelope[0]), self._column(envelope[1]) + 1):
                for row in range(self._row(envelope[2]), self._row(envelope[3]) + 1):
                    self.cells.setdefault((column, row), []).append(index)

    def _column(self, x):
        column = int((x - self.minx) / self.width * self.columns)
        return min(self.columns - 1, max(0, column))

    def _row(self, y):
        row = int((y - self.miny) / self.height * self.rows)
        return min(self.rows - 1, max(0, row))

    def candidates(self, envelope):
        """Полигоны, рамка которых пересекает рамку запроса."""
        if not self.items:
            return []
        found = set()
        for column in range(self._column(envelope[0]), self._column(envelope[1]) + 1):
            for row in range(self._row(envelope[2]), self._row(envelope[3]) + 1):
                found.update(self.cells.get((column, row), ()))
        result = []
        for index in found:
            geometry, box = self.items[index]
            if (box[1] < envelope[0] or box[0] > envelope[1]
                    or box[3] < envelope[2] or box[2] > envelope[3]):
                continue
            result.append(geometry)
        return result


def _glacier_polygons(config):
    """Полигоны ледников со слоя glacier.sqlite."""
    from osgeo import ogr

    glacier = config.vector_dir / "glacier.sqlite"
    if not glacier.exists():
        return []
    dataset = ogr.Open(str(glacier))
    if dataset is None:
        return []
    layer = dataset.GetLayer(0)
    polygons = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        polygons.append((geometry.Clone(), geometry.GetEnvelope()))
    dataset = None
    return polygons


def _split_isolines_by_glaciers(config, shape_path, progress=None):
    """Делит изолинии на части внутри ледников и вне их.

    Внутри ледника изолинии относятся к леднику (в исходном проекте они
    красились синим), поэтому обычные изолинии на площади ледника убираются.
    Возвращает путь к слою изолиний внутри ледников; None — если ледников
    нет или слой с ними недоступен.
    """
    from osgeo import ogr

    polygons = _glacier_polygons(config)
    if not polygons:
        return None
    source = ogr.Open(str(shape_path))
    if source is None:
        return None
    polygon_index = _PolygonIndex(polygons)
    glacier_destination = config.vector_dir / "isolines_glacier.sqlite"
    regular_destination = config.vector_dir / "isolines_regular.sqlite"
    glacier_destination.unlink(missing_ok=True)
    regular_destination.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("SQLite")
    glacier_output = None
    regular_output = None
    glacier_written = 0
    regular_written = 0
    try:
        source_layer = source.GetLayer(0)
        definition = source_layer.GetLayerDefn()
        field_count = definition.GetFieldCount()
        glacier_output = driver.CreateDataSource(str(glacier_destination))
        regular_output = driver.CreateDataSource(str(regular_destination))
        spatial_ref = source_layer.GetSpatialRef()
        glacier_target = glacier_output.CreateLayer(
            "isolines", spatial_ref, ogr.wkbLineString
        )
        regular_target = regular_output.CreateLayer(
            "isolines", spatial_ref, ogr.wkbLineString
        )
        for index in range(field_count):
            field = definition.GetFieldDefn(index)
            glacier_target.CreateField(field)
            regular_target.CreateField(field)
        # Без транзакции SQLite фиксирует каждую линию отдельно, и
        # запись изолиний занимает больше времени, чем их расчёт.
        glacier_target.StartTransaction()
        regular_target.StartTransaction()
        for feature in source_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            envelope = geometry.GetEnvelope()
            remainder = geometry.Clone()
            inside = []
            for polygon in polygon_index.candidates(envelope):
                try:
                    intersection = remainder.Intersection(polygon)
                except Exception:  # noqa: BLE001 - битая геометрия ледника
                    continue
                pieces = _geometry_lines(intersection)
                if not pieces:
                    # Ледник не задевает линию: разность всё равно не
                    # изменится, а операция дороже пересечения.
                    continue
                inside.extend(pieces)
                try:
                    remainder = remainder.Difference(polygon)
                except Exception:  # noqa: BLE001 - битая геометрия ледника
                    continue
                if remainder is None or remainder.IsEmpty():
                    remainder = None
                    break
            for piece in inside:
                item = ogr.Feature(glacier_target.GetLayerDefn())
                for index in range(field_count):
                    item.SetField(index, feature.GetField(index))
                item.SetGeometry(_flat_line(piece))
                glacier_target.CreateFeature(item)
                glacier_written += 1
            outside = _geometry_lines(remainder) if remainder is not None else []
            for piece in outside:
                item = ogr.Feature(regular_target.GetLayerDefn())
                for index in range(field_count):
                    item.SetField(index, feature.GetField(index))
                item.SetGeometry(_flat_line(piece))
                regular_target.CreateFeature(item)
                regular_written += 1
        glacier_target.CommitTransaction()
        regular_target.CommitTransaction()
        glacier_output = None
        regular_output = None
    except Exception as exc:  # noqa: BLE001 - слой не должен ломать сборку
        if progress:
            progress(f"Glacier contour lines skipped: {exc}")
        glacier_destination.unlink(missing_ok=True)
        regular_destination.unlink(missing_ok=True)
        return None
    finally:
        source = None
    if progress:
        progress(f"Glacier contour lines ready: {glacier_written}")
        progress(f"Contour lines outside glaciers: {regular_written}")
    return glacier_destination


def _generate_glacier_isolines(config, shape_path, progress=None):
    """Контурные линии, обрезанные по полигонам ледников.

    Обычные изолинии при этом тоже пересобираются: на площади ледников
    их нет, там рисуются ледниковые (синие).
    """
    return _split_isolines_by_glaciers(config, shape_path, progress)


def prepare_terrain(config, bbox, progress=None, cancel=None, status=None):
    if not config.generate_terrain:
        return
    config.terrain_input_dir.mkdir(parents=True, exist_ok=True)
    source_key = config.resolved_terrain_source()
    source = dem_sources.source(source_key)
    if source.manual:
        if not config.terrain_src_dir:
            raise RuntimeError("terrain_src_dir is required for the manual DEM source")
        _clear_terrain_input(config)
        copy_dem_tiles(bbox, config.terrain_src_dir, config.terrain_input_dir, progress=progress)
    elif config.download_terrain_tiles:
        _clear_terrain_input(config)
        used = []
        if source_key == dem_sources.AUTO_SOURCE:
            download_auto_dem(
                bbox,
                config.terrain_input_dir,
                progress=progress,
                cancel=cancel,
                cache_dir=config.terrain_cache_dir,
                status=status,
                sonny_catalogue=sonny.catalogue_dir(),
                sonny_tiles_dir=sonny.tile_cache_dir(config.projects_dir),
                used=used,
            )
        elif source_key == dem_sources.SONNY_SOURCE:
            download_sonny_dem(
                bbox,
                config.terrain_input_dir,
                progress=progress,
                cancel=cancel,
                cache_dir=config.terrain_cache_dir,
                status=status,
                sonny_catalogue=sonny.catalogue_dir(),
                sonny_tiles_dir=sonny.tile_cache_dir(config.projects_dir),
                used=used,
            )
        else:
            download_dem(
                source_key,
                bbox,
                config.terrain_input_dir,
                progress=progress,
                cancel=cancel,
                cache_dir=config.terrain_cache_dir,
                status=status,
            )
        if used:
            _write_dem_attribution(config, used)

    for archive in list(config.terrain_input_dir.glob("*.zip")):
        shutil.unpack_archive(str(archive), str(config.terrain_input_dir))
        archive.unlink(missing_ok=True)

    # Хест исходных тайлов считаем до конвертации: содержимое .hgt
    # стабильно, а результат конвертации может отличаться от запуска
    # к запуску.
    source_digest = terrain_cache.dem_source_digest(config)

    gdal, _, _ = _gdal()
    for hgt in list(config.terrain_input_dir.glob("*.hgt")):
        temporary = hgt.with_suffix(".tif")
        gdal.Translate(str(temporary), str(hgt))
        hgt.unlink(missing_ok=True)

    if progress:
        progress("Preparing terrain")
    # Рельеф и изолинии считаются долго, поэтому готовый результат
    # переиспользуется, если не изменились версия плагина, параметры
    # и исходные данные (см. core/terrain_cache.py).
    dem_key = terrain_cache.dem_signature(config, bbox, source_digest)
    if terrain_cache.restore_dem(config, dem_key):
        if progress:
            progress("Использован кэш рельефа: повторная обработка не требуется")
    else:
        build_dem(config, bbox, progress=progress, cancel=cancel)
        if config.generate_terrain_hillshade_slope:
            generate_hillshade_slope(config, bbox, progress=progress, cancel=cancel)
        terrain_cache.store_dem(config, dem_key)
    if config.generate_terrain_isolines:
        isolines_key = terrain_cache.isolines_signature(config, bbox, dem_key)
        if terrain_cache.restore_isolines(config, isolines_key):
            if progress:
                progress("Использован кэш изолиний: повторная обработка не требуется")
        else:
            generate_isolines(config, bbox, progress=progress, cancel=cancel)
            terrain_cache.store_isolines(config, isolines_key)
    if config.download_terrain_tiles:
        for path in config.terrain_input_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
