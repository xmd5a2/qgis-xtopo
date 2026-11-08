import gzip
import hashlib
import math
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .bbox import BBox
from . import dem_sources, terrain_cache

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
                  status=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    tiles = srtm_tile_names(bbox)
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
    items = dem_sources.stac_items(
        item.collection,
        (bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
        timeout=60,
    )
    if source_key == "3dep":
        # В коллекции есть и 1″, и 1/3″; нужен более подробный слой.
        preferred = [entry for entry in items
                     if entry.get("id", "").endswith("-13")]
        if preferred:
            items = preferred
    if progress:
        progress(f"Найдено тайлов {item.short}: {len(items)}")
    sources = []
    for entry in items:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("DEM download cancelled")
        href = dem_sources.item_asset_href(entry)
        if not href:
            continue
        if source_key == "cop30":
            tile = dem_sources.item_tile(entry)
            if tile is None:
                continue
            url = dem_sources.aws_tile_url("cop30", *tile)
        else:
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
    dataset = None
    raster = None

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
    """Копия линии без третьего измерения."""
    from osgeo import ogr

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
                inside.extend(_geometry_lines(intersection))
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
        download_dem(
            source_key,
            bbox,
            config.terrain_input_dir,
            progress=progress,
            cancel=cancel,
            cache_dir=config.terrain_cache_dir,
            status=status,
        )

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
