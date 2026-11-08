"""Удаление ложных пиков и правка высоты по рельефу проекта.

Проверка выполняется после подготовки рельефа: правило работает по
``merged_dem.tif`` и тем же порогам, что ``tools/check_peak_placement.py``.
Безымянные пики, которые правило считает ошибочными, удаляются из слоя
``peak.sqlite``.  У остальных безымянных пиков с тегом ``ele`` высота
заменяется на высоту рельефа, если расхождение больше 10 %.

Расхождение высоты само по себе не удаляет пик: конъюнкция 11 при
удалении отключена.  Иначе пик с верным положением, но устаревшей
высотой исчезал бы вместо исправления.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np

from . import i18n, peak_placement

TILE_PIXELS = 3601


def tile_name(lat, lon):
    """Имя тайла SRTM для точки."""
    north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{north}{east}"


@dataclass
class PeakFeature:
    """Пик из слоя проекта: поля, нужные правилу и правке ele."""

    fid: int
    osm_id: str
    lon: float
    lat: float
    ele: Optional[float]
    has_ele: bool
    named: bool

    @property
    def category(self):
        if self.named:
            return "named"
        if self.ele is not None:
            return "ele_only"
        return "anonymous"


class ProjectDem:
    """Окна рельефа проекта в сетке 1″ (EPSG:4326)."""

    def __init__(self, path):
        from osgeo import gdal

        gdal.UseExceptions()
        self.path = Path(path)
        self.dataset = gdal.Open(str(self.path))
        if self.dataset is None:
            raise RuntimeError(f"Не удалось открыть рельеф: {self.path}")
        self.transform = self.dataset.GetGeoTransform()
        self.step_x = abs(self.transform[1])
        self.step_y = abs(self.transform[5])
        self.width = self.dataset.RasterXSize
        self.height = self.dataset.RasterYSize
        self.band = self.dataset.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        self.lon_min = self.transform[0]
        self.lon_max = self.lon_min + self.width * self.step_x
        self.lat_max = self.transform[3]
        self.lat_min = self.lat_max - self.height * self.step_y
        pixel = peak_placement.PIXEL_DEGREES
        self.direct = (
            abs(self.step_x - pixel) <= pixel * 0.01
            and abs(self.step_y - pixel) <= pixel * 0.01
        )

    def covers(self, bbox, margin=0.01):
        """Покрывает ли рельеф текущий охват."""
        return (
            self.lon_min <= bbox.lon_min + margin
            and self.lon_max >= bbox.lon_max - margin
            and self.lat_min <= bbox.lat_min + margin
            and self.lat_max >= bbox.lat_max - margin
        )

    def close(self):
        self.band = None
        self.dataset = None

    def window(self, lon, lat, half_x, half_y):
        if self.direct:
            return self._direct_window(lon, lat, half_x, half_y)
        return self._resampled_window(lon, lat, half_x, half_y)

    def _direct_window(self, lon, lat, half_x, half_y):
        """Прямое чтение, когда растр уже в сетке 1″."""
        width = 2 * half_x + 1
        height = 2 * half_y + 1
        lon_min = lon - half_x * peak_placement.PIXEL_DEGREES
        lat_min = lat - half_y * peak_placement.PIXEL_DEGREES
        lat_max = lat_min + (height - 1) * peak_placement.PIXEL_DEGREES
        out = np.full((height, width), np.nan, dtype="float32")

        x0 = int(round((lon_min - self.transform[0]) / self.step_x))
        y0 = int(round((self.transform[3] - lat_max) / self.step_y))
        x1 = x0 + width
        y1 = y0 + height
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1 = min(self.width, x1)
        sy1 = min(self.height, y1)
        if sx1 <= sx0 or sy1 <= sy0:
            return out

        piece = self.band.ReadAsArray(
            sx0, sy0, sx1 - sx0, sy1 - sy0
        ).astype("float32")
        if self.nodata is not None:
            piece[piece == self.nodata] = np.nan
        # Строки растра идут с севера на юг, а в окне — с юга на север.
        piece = piece[::-1]
        out_x = sx0 - x0
        out_y = (height - 1) - (sy1 - 1 - y0)
        out[out_y:out_y + piece.shape[0], out_x:out_x + piece.shape[1]] = piece
        return out

    def _resampled_window(self, lon, lat, half_x, half_y):
        """Чтение с приведением к сетке 1″ (3DEP, ручной рельеф)."""
        width = 2 * half_x + 1
        height = 2 * half_y + 1
        pixel = peak_placement.PIXEL_DEGREES
        # Привязываем сетку 1″ к сетке растра проекта: инструмент так же
        # округляет окно к узлам сетки, а не к точке пика.
        lon_min = self.transform[0] + round(
            (lon - half_x * pixel - self.transform[0]) / pixel
        ) * pixel
        lat_max = self.transform[3] - round(
            (self.transform[3] - (lat + half_y * pixel)) / pixel
        ) * pixel
        lat_min = lat_max - (height - 1) * pixel
        longitudes = lon_min + np.arange(width) * pixel
        latitudes = lat_min + np.arange(height) * pixel
        columns = (longitudes - self.transform[0]) / self.transform[1] - 0.5
        rows = (latitudes - self.transform[3]) / self.transform[5] - 0.5

        c0 = max(0, int(math.floor(columns.min())) - 1)
        c1 = min(self.width, int(math.ceil(columns.max())) + 2)
        r0 = max(0, int(math.floor(rows.min())) - 1)
        r1 = min(self.height, int(math.ceil(rows.max())) + 2)
        if c1 <= c0 or r1 <= r0:
            return np.full((height, width), np.nan, dtype="float32")

        piece = self.band.ReadAsArray(
            c0, r0, c1 - c0, r1 - r0
        ).astype("float32")
        if self.nodata is not None:
            piece[piece == self.nodata] = np.nan
        piece = piece[::-1]
        sample_rows = (r1 - 1) - rows
        sample_columns = columns - c0
        row_grid, column_grid = np.meshgrid(
            sample_rows, sample_columns, indexing="ij"
        )
        return peak_placement.sample(piece, row_grid, column_grid)


class SrtmTiles:
    """Тайлы SRTM из кэша плагина: та же сетка, что у инструмента."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir).expanduser()
        self._tiles = {}
        self._order = []

    def path(self, name):
        return self.cache_dir / name[:3] / f"{name}.hgt"

    def load(self, name):
        if name in self._tiles:
            return self._tiles[name]
        path = self.path(name)
        if not path.exists():
            return None
        array = np.fromfile(str(path), dtype=">i2")
        if array.size != TILE_PIXELS * TILE_PIXELS:
            return None
        array = array.reshape(TILE_PIXELS, TILE_PIXELS).astype("float32")
        array[array <= -1000] = np.nan
        self._tiles[name] = array
        self._order.append(name)
        while len(self._order) > 6:
            self._tiles.pop(self._order.pop(0), None)
        return array

    def _tile_window(self, name, lon_min, lon_max, lat_min, lat_max):
        array = self.load(name)
        if array is None:
            return None
        lat0 = int(name[1:3])
        if name[0] == "S":
            lat0 = -lat0
        lon0 = int(name[4:7])
        if name[3] == "W":
            lon0 = -lon0
        pixel = peak_placement.PIXEL_DEGREES
        col_min = int(round((lon_min - lon0) / pixel))
        col_max = int(round((lon_max - lon0) / pixel))
        row_min = int(round((lat0 + 1 - lat_max) / pixel))
        row_max = int(round((lat0 + 1 - lat_min) / pixel))
        col_min = max(0, min(TILE_PIXELS - 1, col_min))
        col_max = max(0, min(TILE_PIXELS - 1, col_max))
        row_min = max(0, min(TILE_PIXELS - 1, row_min))
        row_max = max(0, min(TILE_PIXELS - 1, row_max))
        if col_max < col_min or row_max < row_min:
            return None
        return array[row_min:row_max + 1, col_min:col_max + 1]

    def window(self, lon, lat, half_px_x, half_px_y):
        """Окно SRTM в сетке тайлов (строка 0 — южный край)."""
        pixel = peak_placement.PIXEL_DEGREES
        lon_min = lon - half_px_x * pixel
        lon_max = lon + half_px_x * pixel
        lat_min = lat - half_px_y * pixel
        lat_max = lat + half_px_y * pixel
        rows = 2 * half_px_y + 1
        columns = 2 * half_px_x + 1
        out = np.full((rows, columns), np.nan, dtype="float32")
        lat_from = math.floor(lat_min)
        lat_to = math.floor(lat_max)
        lon_from = math.floor(lon_min)
        lon_to = math.floor(lon_max)
        for lat_index in range(lat_from, lat_to + 1):
            for lon_index in range(lon_from, lon_to + 1):
                name = tile_name(lat_index, lon_index)
                overlap_lon_min = max(lon_min, lon_index)
                overlap_lon_max = min(lon_max, lon_index + 1)
                overlap_lat_min = max(lat_min, lat_index)
                overlap_lat_max = min(lat_max, lat_index + 1)
                if (overlap_lon_max < overlap_lon_min
                        or overlap_lat_max < overlap_lat_min):
                    continue
                piece = self._tile_window(
                    name, overlap_lon_min, overlap_lon_max,
                    overlap_lat_min, overlap_lat_max,
                )
                if piece is None:
                    continue
                col_offset = int(round(
                    (overlap_lon_min - lon_min) / pixel
                ))
                row_offset = int(round(
                    (overlap_lat_min - lat_min) / pixel
                ))
                out[
                    row_offset:row_offset + piece.shape[0],
                    col_offset:col_offset + piece.shape[1],
                ] = piece[::-1]
        return out


class SrtmDem:
    """SRTM из кэша с запасным чтением из растра проекта."""

    def __init__(self, cache_dir, fallback):
        self.tiles = SrtmTiles(cache_dir)
        self.fallback = fallback

    def covers(self, bbox, margin=0.01):
        return self.fallback.covers(bbox, margin)

    def close(self):
        self.fallback.close()

    def window(self, lon, lat, half_x, half_y):
        window = self.tiles.window(lon, lat, half_x, half_y)
        if window is None or np.isnan(window).all():
            return self.fallback.window(lon, lat, half_x, half_y)
        return window


def _as_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _field_map(layer):
    definition = layer.GetLayerDefn()
    return {
        definition.GetFieldDefn(index).GetName().lower(): index
        for index in range(definition.GetFieldCount())
    }


def _read_peak(feature, fields):
    geometry = feature.GetGeometryRef()
    if geometry is None:
        return None
    lon = geometry.GetX()
    lat = geometry.GetY()
    if lon is None or lat is None:
        return None

    named = False
    for name, index in fields.items():
        key = name.lower()
        if key in peak_placement.NAME_TAGS or key.startswith("name:"):
            if feature.GetField(index) not in (None, ""):
                named = True
                break

    ele = None
    has_ele = False
    ele_index = fields.get("ele")
    raw = feature.GetField(ele_index) if ele_index is not None else None
    if raw not in (None, ""):
        ele = _as_float(raw)
        has_ele = ele is not None
    else:
        height_index = fields.get("height")
        if height_index is not None:
            ele = _as_float(feature.GetField(height_index))

    osm_id = ""
    id_index = fields.get("@id")
    if id_index is not None:
        osm_id = str(feature.GetField(id_index) or "")
    if not osm_id:
        osm_id = f"fid/{feature.GetFID()}"
    return PeakFeature(
        fid=feature.GetFID(), osm_id=osm_id, lon=lon, lat=lat,
        ele=ele, has_ele=has_ele, named=named,
    )


def _sample_dem_values(dem, peaks, cancel, fraction):
    values = {}
    total = len(peaks)
    for index, peak in enumerate(peaks, 1):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Peak check cancelled")
        window = dem.window(peak.lon, peak.lat, 1, 1)
        if window is None or np.isnan(window).all():
            values[peak.osm_id] = float("nan")
        else:
            window = peak_placement.fill_nan(window)
            values[peak.osm_id] = float(np.mean(window[0:2, 0:2]))
        if fraction is not None and (index % 100 == 0 or index == total):
            fraction(0.3 * index / total)
    return values


def _neighbour_stats(peaks, dem_values):
    results = [
        (peak, SimpleNamespace(dem=dem_values[peak.osm_id]), "", [], None)
        for peak in peaks
    ]
    return peak_placement.neighbour_stats(results)


def _needs_ele_update(peak, metrics, thresholds):
    if not peak.has_ele or peak.ele in (None, 0):
        return False
    if math.isnan(metrics.dem):
        return False
    difference = abs(metrics.dem - peak.ele) / abs(peak.ele) * 100.0
    return difference > thresholds["ele_diff"]


def _apply_changes(layer, fields, removed, updates):
    from osgeo import ogr

    for peak in removed:
        layer.DeleteFeature(peak.fid)

    ele_index = fields.get("ele")
    if updates and ele_index is not None:
        definition = layer.GetLayerDefn()
        field_type = definition.GetFieldDefn(ele_index).GetType()
        numeric = (ogr.OFTInteger, ogr.OFTReal)
        integer64 = getattr(ogr, "OFTInteger64", None)
        if integer64 is not None:
            numeric = numeric + (integer64,)
        for peak, value in updates:
            feature = layer.GetFeature(peak.fid)
            if feature is None:
                continue
            rounded = int(round(value))
            if field_type in numeric:
                feature.SetField(ele_index, rounded)
            else:
                feature.SetField(ele_index, str(rounded))
            layer.SetFeature(feature)
    layer.SyncToDisk()


def _filter_layer(layer_path, dem, config, report, cancel, fraction):
    from osgeo import ogr

    ogr.UseExceptions()
    dataset = ogr.Open(str(layer_path), 1)
    if dataset is None:
        report(i18n.tr("Пики: не удалось открыть слой, проверка пропущена"))
        return None
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            report(i18n.tr("Пики: слой не найден"))
            return None
        fields = _field_map(layer)
        peaks = []
        for feature in layer:
            peak = _read_peak(feature, fields)
            if peak is not None:
                peaks.append(peak)
        if not peaks:
            report(i18n.tr("Пики: нет объектов"))
            return None

        dem_values = _sample_dem_values(dem, peaks, cancel, fraction)
        neighbours = _neighbour_stats(peaks, dem_values)
        thresholds = peak_placement.thresholds_for_source(
            config.resolved_terrain_source()
        )
        radii = sorted({
            thresholds["radius"], thresholds["deep_radius"], 1500,
        })
        # Расхождение ele — повод поправить высоту, а не удалить пик.
        # Остальные конъюнкции по-прежнему удаляют ложные пики.
        removal_thresholds = dict(thresholds)
        removal_thresholds["ele_diff"] = float("inf")

        checked = 0
        removed = []
        updates = []
        no_data = 0
        total = len(peaks)
        for index, peak in enumerate(peaks, 1):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Peak check cancelled")
            if peak.named:
                continue
            if math.isnan(dem_values[peak.osm_id]):
                no_data += 1
                continue
            checked += 1
            metrics = peak_placement.measure(peak, dem, radii, thresholds)
            verdict, _flags, _ = peak_placement.classify(
                peak, metrics, removal_thresholds,
                neighbours.get(peak.osm_id),
            )
            if verdict == "error":
                removed.append(peak)
            elif _needs_ele_update(peak, metrics, thresholds):
                updates.append((peak, metrics.dem))
            if fraction is not None and (index % 25 == 0 or index == total):
                fraction(0.3 + 0.7 * index / total)

        _apply_changes(layer, fields, removed, updates)
        report(i18n.trf(
            "Пики: проверено {}, удалено {}, исправлено ele {}",
            checked, len(removed), len(updates),
        ))
        if no_data:
            report(i18n.trf("Пики: без рельефа {}", no_data))
        return {
            "checked": checked,
            "removed": len(removed),
            "ele_fixed": len(updates),
            "no_data": no_data,
        }
    finally:
        dataset = None


def filter_peak_layer(config, bbox, progress=None, cancel=None, fraction=None):
    """Проверяет слой пиков по рельефу проекта.

    Возвращает словарь со статистикой или None, если проверка не
    выполнялась (нет слоя, нет рельефа или рельеф не покрывает охват).
    """
    report = progress or (lambda message: None)
    layer_path = config.vector_dir / "peak.sqlite"
    if not layer_path.exists():
        return None
    dem_path = config.raster_dir / "merged_dem.tif"
    if not dem_path.exists():
        report(i18n.tr("Пики: рельеф не подготовлен, проверка пропущена"))
        return None
    dem = ProjectDem(dem_path)
    source = config.resolved_terrain_source()
    cache_dir = getattr(config, "terrain_cache_dir", None)
    if source == "srtm" and cache_dir:
        dem = SrtmDem(cache_dir, dem)
    try:
        if not dem.covers(bbox):
            report(i18n.tr("Пики: рельеф не покрывает охват, проверка пропущена"))
            return None
        return _filter_layer(
            layer_path, dem, config, report, cancel, fraction
        )
    finally:
        dem.close()
