import gzip
import math
import shutil
import time
import urllib.request
from pathlib import Path

from .bbox import parse_bbox

CHUNK = 256 * 1024


def _gdal():
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr, osr


def _numpy():
    import numpy

    return numpy


def srtm_tile_names(bbox):
    if isinstance(bbox, str):
        bbox = parse_bbox(bbox)
    names = []
    for lat in range(math.floor(bbox.lat_min), math.floor(bbox.lat_max) + 1):
        for lon in range(math.floor(bbox.lon_min), math.floor(bbox.lon_max) + 1):
            north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
            east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
            names.append(f"{north}{east}")
    return names


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

    def add(self, size):
        """Добавляет байты. True, если скорость пересчитана."""
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
    if progress:
        progress(f"Merging {len(tiles)} DEM tile(s)")
    vrt_path = config.temp_dir / "input_terrain.vrt"
    vrt = gdal.BuildVRT(str(vrt_path), [str(path) for path in tiles])
    if vrt is None:
        raise RuntimeError("Failed to build a VRT from DEM tiles")
    crop = config.project_dir / "crop.geojson"
    merged = config.raster_dir / "merged_dem.tif"
    options = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=str(crop),
        cropToCutline=True,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(merged), vrt, options=options)
    vrt = None
    vrt_path.unlink(missing_ok=True)
    if result is None:
        raise RuntimeError("Failed to crop and merge DEM tiles")
    result = None

    if progress:
        progress("Reprojecting DEM to EPSG:3857")
    merged_3857 = config.raster_dir / "merged_dem_3857.tif"
    options = gdal.WarpOptions(
        dstSRS="EPSG:3857",
        outputType=gdal.GDT_Float32,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(merged_3857), str(merged), options=options)
    if result is None:
        raise RuntimeError("Failed to reproject DEM")
    result = None
    return merged


def generate_hillshade_slope(config, bbox, progress=None, cancel=None):
    gdal, _, _ = _gdal()
    numpy = _numpy()
    merged = config.raster_dir / "merged_dem.tif"
    if not merged.exists():
        raise RuntimeError("merged_dem.tif is missing")

    if progress:
        progress("Generating slope")
    slope = config.raster_dir / "slope.tif"
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        slopeFormat="degree",
        scale=111120,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.DEMProcessing(str(slope), str(merged), "slope", options=options)
    if result is None:
        raise RuntimeError("Failed to generate slope")
    result = None

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
    options = gdal.DEMProcessingOptions(
        computeEdges=True,
        multiDirectional=True,
        zFactor=2,
        scale=111120,
        altitude=70,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.DEMProcessing(str(hillshade), str(upscaled), "hillshade", options=options)
    if result is None:
        raise RuntimeError("Failed to generate hillshade")
    result = None

    hillshade_template, hillshade_array = _read_band(hillshade)
    slope_template, slope_up_array = _read_band(slope_up)
    slope_shade = 255.0 * (1.0 - numpy.clip(slope_up_array / 90.0, 0.0, 1.0))
    composite = numpy.clip(hillshade_array + slope_shade * 0.5, 0, 255)
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
    water = config.vector_dir / "water_dissolved.sqlite"
    if not water.exists():
        water = config.vector_dir / "water.sqlite"
    changed = _subtract_water_from_layer(layer, [water], spatial_ref)
    if progress and changed:
        progress(f"Contour lines cut by water: {changed}")
    layer.SyncToDisk()
    dataset = None

    destination = config.vector_dir / "isolines_full.sqlite"
    options = [
        "-nln", "isolines",
        "-dsco", "SPATIALITE=YES",
        "-lco", "COMPRESS_GEOM=YES",
        "-skipfailures",
    ]
    result = gdal.VectorTranslate(str(destination), str(shape_path), options=options)
    if result is None:
        raise RuntimeError("Failed to write contour lines")
    result = None
    _generate_glacier_isolines(config, shape_path, progress)
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        (config.temp_dir / f"isolines_full{suffix}").unlink(missing_ok=True)
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


def _generate_glacier_isolines(config, shape_path, progress=None):
    """Контурные линии, обрезанные по полигонам ледников."""
    _, ogr, _ = _gdal()
    glacier = config.vector_dir / "glacier.sqlite"
    if not glacier.exists():
        return None
    source = ogr.Open(str(shape_path))
    if source is None:
        return None
    glacier_source = ogr.Open(str(glacier))
    if glacier_source is None:
        source = None
        return None
    destination = config.vector_dir / "isolines_glacier.sqlite"
    destination.unlink(missing_ok=True)
    try:
        source_layer = source.GetLayer(0)
        glacier_layer = glacier_source.GetLayer(0)
        driver = ogr.GetDriverByName("SQLite")
        output = driver.CreateDataSource(str(destination))
        target = output.CreateLayer(
            "isolines", source_layer.GetSpatialRef(), ogr.wkbLineString
        )
        for index in range(source_layer.GetLayerDefn().GetFieldCount()):
            definition = source_layer.GetLayerDefn().GetFieldDefn(index)
            target.CreateField(definition)
        written = 0
        field_count = source_layer.GetLayerDefn().GetFieldCount()
        for feature in source_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            envelope = geometry.GetEnvelope()
            glacier_layer.SetSpatialFilterRect(*envelope)
            pieces = []
            for glacier_feature in glacier_layer:
                glacier_geometry = glacier_feature.GetGeometryRef()
                if glacier_geometry is None or glacier_geometry.IsEmpty():
                    continue
                try:
                    intersection = geometry.Intersection(glacier_geometry)
                except Exception:  # noqa: BLE001 - битая геометрия ледника
                    continue
                pieces.extend(_geometry_lines(intersection))
            if not pieces:
                continue
            for piece in pieces:
                item = ogr.Feature(target.GetLayerDefn())
                for index in range(field_count):
                    item.SetField(index, feature.GetField(index))
                item.SetGeometry(_flat_line(piece))
                target.CreateFeature(item)
                item = None
                written += 1
        output = None
    except Exception as exc:  # noqa: BLE001 - слой не должен ломать сборку
        if progress:
            progress(f"Glacier contour lines skipped: {exc}")
        destination.unlink(missing_ok=True)
        return None
    finally:
        source = None
        glacier_source = None
    if progress:
        progress(f"Glacier contour lines ready: {written}")
    return destination


def prepare_terrain(config, bbox, progress=None, cancel=None, status=None):
    if not config.generate_terrain:
        return
    config.terrain_input_dir.mkdir(parents=True, exist_ok=True)
    if config.download_terrain_tiles:
        download_srtm(
            bbox,
            config.terrain_input_dir,
            progress=progress,
            cancel=cancel,
            cache_dir=config.terrain_cache_dir,
            status=status,
        )
    elif config.get_terrain_tiles:
        if not config.terrain_src_dir:
            raise RuntimeError("terrain_src_dir is required when get_terrain_tiles=true")
        copy_dem_tiles(bbox, config.terrain_src_dir, config.terrain_input_dir, progress=progress)

    for archive in list(config.terrain_input_dir.glob("*.zip")):
        shutil.unpack_archive(str(archive), str(config.terrain_input_dir))
        archive.unlink(missing_ok=True)

    gdal, _, _ = _gdal()
    for hgt in list(config.terrain_input_dir.glob("*.hgt")):
        temporary = hgt.with_suffix(".tif")
        gdal.Translate(str(temporary), str(hgt))
        hgt.unlink(missing_ok=True)

    if progress:
        progress("Preparing terrain")
    build_dem(config, bbox, progress=progress, cancel=cancel)
    if config.generate_terrain_hillshade_slope:
        generate_hillshade_slope(config, bbox, progress=progress, cancel=cancel)
    if config.generate_terrain_isolines:
        generate_isolines(config, bbox, progress=progress, cancel=cancel)
    if config.download_terrain_tiles:
        for path in config.terrain_input_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)
