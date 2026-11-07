#!/usr/bin/env python3
"""Локальный сервер тайлов для сравнения DEM-наборов (инструмент разработки).

Отдаёт страницу ``tools/dem_compare.html`` и рендерит тайлы подложки
(рельеф) из настоящих наборов данных: SRTM v3, Copernicus DEM GLO-30,
NASADEM, ALOS AW3D30 — все с разрешением 1″ (30 м) или лучше.
Исходники тянутся по требованию и кладутся в кэш.

Запуск::

    python3 tools/dem_compare_server.py [--port 8765] [--cache DIR]

Эндпоинты:

* ``/api/datasets`` — список наборов с метаданными;
* ``/tile/<ds>/<z>/<x>/<y>.png?mode=hillshade|elevation|contours`` — тайл;
* ``/diff/<a>/<b>/<z>/<x>/<y>.png`` — разность высот B−A;
* ``/api/value?ds=&lon=&lat=`` — высота в точке;
* ``/api/profile?ds=&lon1=&lat1=&lon2=&lat2=&n=`` — профиль по отрезку;
* ``/api/info?ds=&lon=&lat=`` — разрешение источника и наличие данных.
"""

import argparse
import gzip
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
osr.UseExceptions()

TILE_SIZE = 256
# Версия кэша тайлов: меняется, когда меняется рендер. Старые файлы не
# удаляются, просто перестают использоваться.
CACHE_VERSION = 4
# Подгрузка тайла с запасом, чтобы шов между тайлами не был виден.
TILE_OVERLAP = 1
# Ограничение на размер промежуточного растра (пиксели по стороне).
MAX_GRID = 2048

# Счётчик для уникальных имён временных файлов.
_counter = 0
_counter_lock = threading.Lock()

ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "dem_compare.html"

DEFAULT_CACHE = (
    Path.home() / "qgis_projects" / "qgisxtopo-cache" / "dem_compare"
)

USER_AGENT = "qgis-xtopo-dem-compare/1.0"

# ---------------------------------------------------------------------------
# Описание наборов данных
# ---------------------------------------------------------------------------

DATASETS = {
    "srtm3": {
        "title": "SRTM v3 (1″)",
        "provider": "NASA/USGS, зеркало AWS elevation-tiles-prod (skadi)",
        "source": "skadi",
        "kind": "surface",
        "datum": "EGM96, ортометрические высоты",
        "note": "SRTMGL1.003, пустоты заполнены. Текущий источник плагина.",
        "attribution": "SRTM data courtesy of the U.S. Geological Survey",
        "src_nodata": -32768,
    },
    "cop30": {
        "title": "Copernicus DEM GLO-30 (1″)",
        "provider": "ESA/Copernicus, бакет AWS copernicus-dem-30m",
        "source": "cop30",
        "kind": "surface",
        "datum": "EGM2008, высоты над геоидом",
        "note": "TanDEM-X. Публичная часть: часть тайлов не отдана (Армения, "
                "Азербайджан, СЗ Иран, Туркменистан).",
        "attribution": "Produced using Copernicus data and information "
                       "funded by the European Union and ESA",
        "src_nodata": None,
    },
    "nasadem": {
        "title": "NASADEM HGT (1″)",
        "provider": "NASA/JPL/USGS, зеркало Planetary Computer",
        "source": "nasadem",
        "kind": "surface",
        "datum": "EGM96, ортометрические высоты",
        "note": "SRTM v3 с доработкой NASA. Покрытие только −56°…+61° широты.",
        "attribution": "NASADEM — courtesy of NASA/JPL/USGS",
        "src_nodata": -32768,
    },
    "alos30": {
        "title": "ALOS World 3D-30m (1″)",
        "provider": "JAXA, зеркало Planetary Computer",
        "source": "alos30",
        "kind": "surface",
        "datum": "EGM2008, высоты над геоидом",
        "note": "DSM: включает растительность и постройки. Нужна ссылка на JAXA.",
        "attribution": "ALOS World 3D-30m — © JAXA",
        "src_nodata": None,
    },
    "3dep": {
        "title": "USGS 3DEP Seamless (1/3″, 10 м)",
        "provider": "USGS, зеркало Planetary Computer",
        "source": "3dep",
        "kind": "bare-earth",
        "datum": "NAVD88, высоты над геоидом",
        "note": "Только США. Настоящий DTM без растительности и построек — "
                "единственный набор в списке с земной поверхностью.",
        "attribution": "3D Elevation Program — courtesy of the U.S. Geological "
                       "Survey (public domain)",
        "src_nodata": None,
        "id_suffix": "-13",
    },
}

# Наборы для попарного сравнения по умолчанию.
DEFAULT_A = "cop30"
DEFAULT_B = "srtm3"


# ---------------------------------------------------------------------------
# Загрузка исходных тайлов
# ---------------------------------------------------------------------------

def _download(url, target):
    """Скачивает файл с докачкой: при обрыве продолжаем с места обрыва."""
    tmp = target.with_name(target.name + ".part")
    existing = tmp.stat().st_size if tmp.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers),
                timeout=300) as response:
            append = existing > 0 and response.status == 206
            mode = "ab" if append else "wb"
            with tmp.open(mode) as handle:
                while True:
                    chunk = response.read(1 << 18)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code in (403, 404):
            return False
        raise
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(target)
    return True


def _tile_name(lat, lon):
    """Имя тайла SRTM-стиля: юго-западный угол."""
    north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{north}{east}"


def _aws_url(source, lat, lon):
    """Адрес исходного тайла в публичных бакетах AWS."""
    name = _tile_name(lat, lon)
    if source == "skadi":
        return (f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/"
                f"{name[:3]}/{name}.hgt.gz")
    if source == "cop30":
        token = f"{name[:3]}_00_{name[3:]}_00"
        return (f"https://copernicus-dem-30m.s3.amazonaws.com/"
                f"Copernicus_DSM_COG_10_{token}_DEM/"
                f"Copernicus_DSM_COG_10_{token}_DEM.tif")
    raise KeyError(source)


def _http_json(url, retries=6):
    """Запрос JSON с повторами: сервис может ответить 429."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    delay = 5.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 1.6
    raise RuntimeError("не удалось получить ответ")


def _sign_pc(href):
    url = ("https://planetarycomputer.microsoft.com/api/sas/v1/sign?href="
           + urllib.parse.quote(href, safe=""))
    return _http_json(url)["href"]


class SourceStore:
    """Источники DEM: локальный кэш для gzip-SRTM, HTTP-чтение — для COG.

    Тайлы COG (Copernicus, NASADEM, ALOS, 3DEP) не скачиваются целиком:
    GDAL читает их по HTTP Range прямо из бакета, запрашивая только нужные
    блоки. Это важно: тайл 3DEP в 10 м — это десятки мегабайт.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._download_locks = {}
        self._signs = {}

    def _signed(self, href):
        """Подпись SAS кэшируется: подписывать каждый раз нельзя — 429."""
        with self._lock:
            entry = self._signs.get(href)
            if entry and entry[0] > time.time() + 120:
                return entry[1]
        signed = _sign_pc(href)
        with self._lock:
            self._signs[href] = (time.time() + 1800, signed)
        return signed

    def _tile_lock(self, key):
        with self._lock:
            if key not in self._download_locks:
                self._download_locks[key] = threading.Lock()
            return self._download_locks[key]

    def local_path(self, ds, lat, lon):
        folder = self.cache_dir / ds
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{_tile_name(lat, lon)}.tif"

    def sources(self, ds, bbox):
        """Список источников для области: локальные файлы и /vsicurl."""
        spec = DATASETS[ds]
        source = spec["source"]
        if source == "skadi":
            paths = []
            for lat, lon in _tiles_for(bbox):
                path = self._ensure_srtm(ds, lat, lon)
                if path:
                    paths.append(path)
            return paths
        if source == "cop30":
            paths = []
            for lat, lon in _tiles_for(bbox):
                url = _aws_url(source, lat, lon)
                paths.append("/vsicurl/" + url)
            return paths
        return self._pc_sources(ds, bbox)

    def open_at(self, ds, lon, lat):
        """Открывает набор так, чтобы в него попала точка."""
        spec = DATASETS[ds]
        source = spec["source"]
        if source == "skadi":
            path = self._ensure_srtm(ds, int(math.floor(lat)),
                                     int(math.floor(lon)))
            return gdal.Open(path) if path else None
        if source == "cop30":
            url = _aws_url(source, int(math.floor(lat)), int(math.floor(lon)))
            return gdal.Open("/vsicurl/" + url)
        sources = self._pc_sources(ds, (math.floor(lon), math.floor(lat),
                                         math.floor(lon) + 1,
                                         math.floor(lat) + 1),
                                   center_only=True)
        for path in sources:
            dataset = gdal.Open(path)
            if dataset is None:
                continue
            gt = dataset.GetGeoTransform()
            spatial_ref = dataset.GetSpatialRef()
            if spatial_ref and not spatial_ref.IsGeographic():
                source_ref = osr.SpatialReference()
                source_ref.ImportFromEPSG(4326)
                transform = osr.CoordinateTransformation(source_ref,
                                                         spatial_ref)
                x, y = transform.TransformPoint(lon, lat)[:2]
            else:
                x, y = lon, lat
            minx = gt[0]
            maxx = gt[0] + gt[1] * dataset.RasterXSize
            maxy = gt[3]
            miny = gt[3] + gt[5] * dataset.RasterYSize
            if minx <= x <= maxx and miny <= y <= maxy:
                return dataset
            dataset = None
        return None

    def _ensure_srtm(self, ds, lat, lon):
        """Скачивает .hgt.gz и превращает в GeoTIFF (gzip по HTTP не читаем)."""
        target = self.local_path(ds, lat, lon)
        if target.exists() and target.stat().st_size > 0:
            return target
        with self._tile_lock(f"{ds}:{target.name}"):
            if target.exists() and target.stat().st_size > 0:
                return target
            raw = target.with_name(target.name + ".raw")
            if not _download(_aws_url("skadi", lat, lon), raw):
                raw.unlink(missing_ok=True)
                return None
            try:
                with gzip.open(raw, "rb") as source_handle:
                    data = source_handle.read()
                # .hgt — big-endian, GDAL ждёт родной порядок байт.
                array = np.frombuffer(data, dtype=">i2").reshape(3601, 3601)
                array = array.astype(np.int16)
                temporary = target.with_name(target.name + ".tmp")
                dataset = gdal.GetDriverByName("GTiff").Create(
                    str(temporary), 3601, 3601, 1, gdal.GDT_Int16,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
                dataset.SetGeoTransform((lon, 1.0 / 3600.0, 0, lat + 1, 0,
                                         -1.0 / 3600.0))
                dataset.SetProjection("EPSG:4326")
                band = dataset.GetRasterBand(1)
                band.WriteArray(array)
                band.SetNoDataValue(-32768)
                dataset.FlushCache()
                dataset = None
                temporary.replace(target)
            except Exception:
                raw.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            finally:
                raw.unlink(missing_ok=True)
        return target

    def _pc_sources(self, ds, bbox, center_only=False):
        """Подписанные ссылки на файлы наборов Planetary Computer."""
        collection = {"nasadem": "nasadem", "alos30": "alos-dem",
                      "3dep": "3dep-seamless"}[DATASETS[ds]["source"]]
        suffix = DATASETS[ds].get("id_suffix")
        query_bbox = bbox or (0, 0, 1, 1)
        key = self.cache_dir / f"pc_{collection}_{int(query_bbox[0])}_" \
                                 f"{int(query_bbox[1])}.json"
        if key.exists():
            try:
                items = json.loads(key.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                items = []
        else:
            items = []
        if not items:
            url = ("https://planetarycomputer.microsoft.com/api/stac/v1/search"
                   f"?collections={collection}"
                   f"&bbox={','.join(str(v) for v in query_bbox)}&limit=100")
            items = _http_json(url).get("features", [])
            key.write_text(json.dumps(items), encoding="utf-8")
        if suffix:
            preferred = [item for item in items
                         if item.get("id", "").endswith(suffix)]
            if preferred:
                items = preferred
        if center_only:
            # Подписывать лишнее нельзя: сервис подписи отвечает 429.
            center = (query_bbox[0] + 0.5, query_bbox[1] + 0.5)
            items = [item for item in items
                     if not item.get("bbox") or
                     (item["bbox"][0] <= center[0] <= item["bbox"][2] and
                      item["bbox"][1] <= center[1] <= item["bbox"][3])]
        sources = []
        for item in items:
            href = None
            for asset in item.get("assets", {}).values():
                candidate = asset.get("href", "")
                if candidate.lower().endswith((".tif", ".tiff")):
                    href = candidate
                    break
            if href:
                sources.append("/vsicurl/" + self._signed(href))
        return sources


# ---------------------------------------------------------------------------
# Геометрия тайлов
# ---------------------------------------------------------------------------

def _merc_y_to_lat(value):
    value = min(max(value, 0.0), 1.0)
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * value))))


def tile_bounds(z, x, y):
    """Границы тайла Web Mercator в EPSG:4326 (minx, miny, maxx, maxy)."""
    count = 2 ** z
    lon1 = x / count * 360.0 - 180.0
    lon2 = (x + 1) / count * 360.0 - 180.0
    lat1 = _merc_y_to_lat((y + 1) / count)
    lat2 = _merc_y_to_lat(y / count)
    return lon1, lat1, lon2, lat2


def _mercator_bounds(lon1, lat1, lon2, lat2):
    def merc(lat):
        lat = min(max(lat, -85.05112878), 85.05112878)
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / math.pi

    x1 = lon1 * 20037508.34 / 180.0
    x2 = lon2 * 20037508.34 / 180.0
    y1 = merc(lat1) * 20037508.34
    y2 = merc(lat2) * 20037508.34
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def ground_resolution(z, lat):
    """Метров на пиксель тайла в направлении север-юг."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


# ---------------------------------------------------------------------------
# Работа с DEM
# ---------------------------------------------------------------------------

def _tiles_for(bbox):
    """Тайлы 1°×1°, пересекающие область."""
    minx, miny, maxx, maxy = bbox
    tiles = []
    for lat in range(int(math.floor(miny)), int(math.floor(maxy)) + 1):
        for lon in range(int(math.floor(minx)), int(math.floor(maxx)) + 1):
            lon = max(-180, min(179, lon))
            tiles.append((lat, lon))
    return tiles


def _sources_for(store, ds, bbox):
    """Источники DEM для области.

    Список, а не общий VRT: запросы идут параллельно, а общий файл VRT
    затирался бы соседним запросом — и на карте повторялся бы один и тот же
    участок рельефа.
    """
    return store.sources(ds, bbox)


def _dem_grid(store, ds, bbox, pixels=None):
    """DEM набора на регулярной сетке EPSG:4326.

    Возвращает (массив высот, geotransform). Размер сетки по умолчанию —
    по нативному разрешению источника, чтобы не терять детали.
    """
    minx, miny, maxx, maxy = bbox
    sources = _sources_for(store, ds, bbox)
    if not sources:
        return None, None
    source = gdal.Open(sources[0])
    if source is None:
        return None, None
    gt = source.GetGeoTransform()
    spatial_ref = source.GetSpatialRef()
    geographic = bool(spatial_ref and spatial_ref.IsGeographic())
    if geographic:
        deg_x = abs(gt[1]) or 1 / 3600.0
        deg_y = abs(gt[5]) or 1 / 3600.0
    else:
        # Проекция в метрах: переводим нативный шаг в градусы.
        center_lat = (miny + maxy) / 2
        deg_x = (abs(gt[1]) or 30.0) / (111320.0 *
                                         math.cos(math.radians(center_lat)))
        deg_y = (abs(gt[5]) or 30.0) / 110574.0
    native_dx = deg_x
    native_dy = deg_y
    if pixels is None:
        pixels_x = int(math.ceil((maxx - minx) / native_dx))
        pixels_y = int(math.ceil((maxy - miny) / native_dy))
        pixels = max(64, min(MAX_GRID, max(pixels_x, pixels_y)))
    options = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:4326",
        outputBounds=(minx, miny, maxx, maxy),
        width=pixels,
        height=pixels,
        resampleAlg="bilinear",
        srcNodata=DATASETS[ds].get("src_nodata"),
        dstNodata=float("nan"),
        multithread=True,
    )
    warped = gdal.Warp("", sources, options=options)
    source = None
    if warped is None:
        return None, None
    array = warped.GetRasterBand(1).ReadAsArray().astype("float32")
    out_gt = warped.GetGeoTransform()
    warped = None
    return array, out_gt


def _temp_path(prefix):
    """Уникальное имя временного файла.

    Запросы идут параллельно, а файлы с фиксированными именами затирали
    друг друга: отмывка считалась по чужому DEM и тайл выходил чёрным.
    """
    global _counter
    with _counter_lock:
        _counter += 1
        number = _counter
    return f"/vsimem/dem_compare_{prefix}_{number}_{threading.get_ident()}.tif"


def _prepare_dem(store, ds, bbox):
    """Готовит DEM во временном файле: возвращает (путь, массив, geotransform)."""
    array, gt = _dem_grid(store, ds, bbox)
    if array is None:
        return None, None, None
    height, width = array.shape
    mem = gdal.GetDriverByName("MEM").Create("", width, height, 1,
                                             gdal.GDT_Float32)
    mem.SetGeoTransform(gt)
    mem.SetProjection("EPSG:4326")
    band = mem.GetRasterBand(1)
    band.WriteArray(np.where(np.isnan(array), -32768.0,
                              array).astype("float32"))
    band.SetNoDataValue(-32768.0)
    path = _temp_path("dem")
    gdal.GetDriverByName("GTiff").CreateCopy(path, mem,
                                             options=["COMPRESS=DEFLATE"])
    mem = None
    return path, array, gt


def _hillshade(dem_path):
    """Отмывка штатным DEMProcessing — так же, как в плагине."""
    options = gdal.DEMProcessingOptions(computeEdges=True, azimuth=315,
                                        altitude=45, zFactor=1)
    shade_path = _temp_path("shade")
    gdal.DEMProcessing(shade_path, dem_path, "hillshade", options=options)
    dataset = gdal.Open(shade_path)
    shade = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    gdal.Unlink(shade_path)
    return shade


def _contour_mask(array, valid, step):
    """Маска горизонталей методом marching squares по сетке высот."""
    if array is None or not valid.any():
        return None
    data = np.where(valid, array, np.nan)
    mask = np.zeros(data.shape, dtype="uint8")
    lo = float(np.nanmin(data))
    hi = float(np.nanmax(data))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return mask
    first = math.ceil(lo / step) * step
    v00 = data[:-1, :-1]
    v10 = data[:-1, 1:]
    v11 = data[1:, 1:]
    v01 = data[1:, :-1]
    cell_valid = (np.isfinite(v00) & np.isfinite(v10) &
                  np.isfinite(v11) & np.isfinite(v01))
    for level in np.arange(first, hi + step, step):
        corners = np.stack([v00, v10, v11, v01])
        lowest = np.nanmin(corners, axis=0)
        highest = np.nanmax(corners, axis=0)
        crossing = cell_valid & (lowest <= level) & (highest >= level)
        ys, xs = np.nonzero(crossing)
        for y, x in zip(ys, xs):
            points = []
            for (ax, ay, bx, by) in ((x, y, x + 1, y), (x + 1, y, x + 1, y + 1),
                                     (x + 1, y + 1, x, y + 1),
                                     (x, y + 1, x, y)):
                va = data[ay, ax]
                vb = data[by, bx]
                if not (np.isfinite(va) and np.isfinite(vb)):
                    continue
                if (va - level) * (vb - level) > 0:
                    continue
                ratio = (level - va) / (vb - va) if vb != va else 0.0
                points.append((ax + (bx - ax) * ratio,
                               ay + (by - ay) * ratio))
            if len(points) == 2:
                _draw_line(mask, points[0][0], points[0][1],
                           points[1][0], points[1][1])
            elif len(points) == 4:
                _draw_line(mask, points[0][0], points[0][1],
                           points[1][0], points[1][1])
                _draw_line(mask, points[2][0], points[2][1],
                           points[3][0], points[3][1])
    return mask


def _draw_line(mask, x0, y0, x1, y1):
    """Рисует отрезок по пикселям (DDA)."""
    steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
    for index in range(steps + 1):
        t = index / steps if steps else 0
        x = int(round(x0 + (x1 - x0) * t))
        y = int(round(y0 + (y1 - y0) * t))
        if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1]:
            mask[y, x] = 255


def _elevation_colors(array, sea_level=0.0):
    """Гипсометрическая раскраска."""
    if array is None:
        return None
    valid = ~np.isnan(array)
    value = np.where(valid, array, sea_level).astype("float64")
    lo = min(sea_level, float(value.min()))
    hi = float(value.max())
    if hi <= lo:
        hi = lo + 1.0
    norm = (value - lo) / (hi - lo)
    stops = [
        (0.00, (58, 106, 62)),
        (0.18, (110, 155, 92)),
        (0.36, (176, 196, 132)),
        (0.52, (222, 219, 168)),
        (0.68, (196, 172, 122)),
        (0.84, (146, 116, 84)),
        (1.00, (96, 74, 62)),
    ]
    rgb = np.zeros(value.shape + (3,), dtype="uint8")
    for index in range(len(stops) - 1):
        p0, c0 = stops[index]
        p1, c1 = stops[index + 1]
        mask = (norm >= p0) & (norm <= p1)
        if not mask.any():
            continue
        t = ((norm[mask] - p0) / (p1 - p0))[:, None]
        rgb[mask] = (np.array(c0)[None, :] * (1 - t) +
                     np.array(c1)[None, :] * t).astype("uint8")
    rgb[~valid] = (0, 0, 0)
    return rgb


def _diff_colors(diff, valid):
    """Расхождение высот: синий — ниже, красный — выше, серый — равно."""
    if not valid.any():
        return np.zeros(diff.shape + (3,), dtype="uint8")
    # Шкалу задаёт 98-й процентль: единичные отвесы не съедают контраст.
    limit = max(5.0, float(np.percentile(np.abs(diff[valid]), 98)))
    norm = np.clip(np.nan_to_num(diff, nan=0.0) / limit, -1.0, 1.0)
    grey = (np.abs(norm) * 235).astype("uint8")
    rgb = np.dstack([grey, grey, grey]).astype("uint8")
    positive = valid & (norm > 0)
    negative = valid & (norm < 0)
    rgb[positive, 0] = (norm[positive] * 255).astype("uint8")
    rgb[positive, 1] = 0
    rgb[positive, 2] = 0
    rgb[negative, 2] = (-norm[negative] * 255).astype("uint8")
    rgb[negative, 0] = 0
    rgb[negative, 1] = 0
    return rgb


def _nice_step(value):
    """Круглое число ступеней для горизонталей."""
    import math as _math
    if value <= 0:
        return 10.0
    exponent = _math.floor(_math.log10(value))
    base = 10 ** exponent
    for factor in (1, 2, 5, 10):
        if value <= factor * base:
            return factor * base
    return 10 * base


def _math_ceil(value):
    return int(math.ceil(value))


def _array_to_dataset(rgb, alpha, gt):
    """Собирает MEM-растр RGB(A) из массивов."""
    height, width = rgb.shape[:2]
    bands = 4 if alpha is not None else 3
    mem = gdal.GetDriverByName("MEM").Create("", width, height, bands,
                                             gdal.GDT_Byte)
    for index in range(3):
        mem.GetRasterBand(index + 1).WriteArray(rgb[..., index])
    if alpha is not None:
        band = mem.GetRasterBand(4)
        band.WriteArray(alpha)
        band.SetColorInterpretation(gdal.GCI_AlphaBand)
    mem.SetGeoTransform(gt)
    mem.SetProjection("EPSG:4326")
    return mem


def _warp_to_png(mem, bounds_3857, size=TILE_SIZE, resample="average"):
    """Переводит растр в Web Mercator и отдаёт PNG-байты."""
    options = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:3857",
        outputBounds=bounds_3857,
        xRes=(bounds_3857[2] - bounds_3857[0]) / size,
        yRes=-(bounds_3857[3] - bounds_3857[1]) / size,
        resampleAlg=resample,
        multithread=True,
    )
    warped = gdal.Warp("", mem, options=options)
    if warped is None:
        return None
    png_path = _temp_path("tile").replace(".tif", ".png")
    png = gdal.GetDriverByName("PNG").CreateCopy(png_path, warped)
    warped = None
    if png is None:
        return None
    png = None
    data = gdal.VSIGetMemFileBuffer_unsafe(png_path)
    if data is None:
        gdal.Unlink(png_path)
        return None
    # Буфер — это memoryview на память GDAL: копируем до удаления файла.
    result = bytes(data)
    gdal.Unlink(png_path)
    return result


# ---------------------------------------------------------------------------
# Рендер тайлов
# ---------------------------------------------------------------------------

def render_tile(store, ds, z, x, y, mode="hillshade"):
    lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
    pad_x = (lon2 - lon1) / TILE_SIZE * TILE_OVERLAP
    pad_y = (lat2 - lat1) / TILE_SIZE * TILE_OVERLAP
    bbox = (lon1 - pad_x, lat1 - pad_y, lon2 + pad_x, lat2 + pad_y)
    dem_path, array, gt = _prepare_dem(store, ds, bbox)
    if dem_path is None:
        return None, None
    valid = ~np.isnan(array)
    if mode == "elevation":
        rgb = _elevation_colors(array)
    elif mode == "contours":
        rgb = _elevation_colors(array)
        shade = _hillshade(dem_path)
        if shade is not None and shade.shape == rgb.shape[:2]:
            factor = (shade.astype("float64") / 255.0 * 0.6 + 0.4)[:, :, None]
            rgb = np.clip(rgb.astype("float64") * factor, 0, 255).astype("uint8")
        step = _nice_step(ground_resolution(z, (lat1 + lat2) / 2) * 10)
        mask = _contour_mask(array, valid, step)
        if mask is not None:
            rgb[mask > 0] = (58, 38, 28)
    else:
        shade = _hillshade(dem_path)
        if shade is None or shade.shape != array.shape:
            return None, None
        rgb = np.dstack([shade, shade, shade])
    alpha = np.where(valid, 255, 0).astype("uint8")
    mem = _array_to_dataset(rgb, alpha, gt)
    bounds = _mercator_bounds(lon1, lat1, lon2, lat2)
    data = _warp_to_png(mem, bounds)
    native = (abs(gt[1]) * 111320.0 *
              math.cos(math.radians((lat1 + lat2) / 2)), abs(gt[5]) * 110574.0)
    return data, native


def render_diff(store, ds_a, ds_b, z, x, y):
    lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
    bbox = (lon1, lat1, lon2, lat2)
    array_a, gt = _dem_grid(store, ds_a, bbox, pixels=TILE_SIZE)
    array_b, _ = _dem_grid(store, ds_b, bbox, pixels=TILE_SIZE)
    if array_a is None or array_b is None:
        return None
    diff = array_b - array_a
    valid = (~np.isnan(array_a)) & (~np.isnan(array_b))
    rgb = _diff_colors(diff, valid)
    alpha = np.where(valid, 255, 0).astype("uint8")
    mem = _array_to_dataset(rgb, alpha, gt)
    bounds = _mercator_bounds(lon1, lat1, lon2, lat2)
    return _warp_to_png(mem, bounds)


def _dataset_transform(dataset, lon, lat):
    """Переводит точку в СК растра: (x, y, native_x_m, native_y_m)."""
    gt = dataset.GetGeoTransform()
    spatial_ref = dataset.GetSpatialRef()
    geographic = bool(spatial_ref and spatial_ref.IsGeographic())
    if geographic:
        x, y = lon, lat
        native_x = abs(gt[1]) * 111320.0 * math.cos(math.radians(lat))
        native_y = abs(gt[5]) * 110574.0
    else:
        source = osr.SpatialReference()
        source.ImportFromEPSG(4326)
        target = spatial_ref
        transform = osr.CoordinateTransformation(source, target)
        x, y = transform.TransformPoint(lon, lat)[:2]
        native_x = abs(gt[1])
        native_y = abs(gt[5])
    return x, y, native_x, native_y


def sample_value(store, ds, lon, lat):
    """Высота в точке из исходного тайла набора."""
    dataset = store.open_at(ds, lon, lat)
    if dataset is None:
        return {"elevation": None, "native_x_m": None, "native_y_m": None}
    gt = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    x, y, native_x, native_y = _dataset_transform(dataset, lon, lat)
    col = int((x - gt[0]) / gt[1])
    row = int((y - gt[3]) / gt[5])
    value = None
    if 0 <= row < dataset.RasterYSize and 0 <= col < dataset.RasterXSize:
        value = float(band.ReadAsArray(col, row, 1, 1)[0][0])
        if nodata is not None and abs(value - nodata) < 1e-6:
            value = None
    dataset = None
    return {"elevation": value, "native_x_m": native_x, "native_y_m": native_y}


def sample_profile(store, ds, lon1, lat1, lon2, lat2, count=200):
    """Профиль высот по отрезку: выборка из DEM сетки вдоль линии."""
    pad = 0.02
    bbox = (min(lon1, lon2) - pad, min(lat1, lat2) - pad,
            max(lon1, lon2) + pad, max(lat1, lat2) + pad)
    array, gt = _dem_grid(store, ds, bbox)
    if array is None:
        return []
    height, width = array.shape
    samples = []
    for index in range(count + 1):
        t = index / count
        lon = lon1 + (lon2 - lon1) * t
        lat = lat1 + (lat2 - lat1) * t
        col = (lon - gt[0]) / gt[1]
        row = (lat - gt[3]) / gt[5]
        c0, r0 = int(math.floor(col)), int(math.floor(row))
        c1, r1 = min(c0 + 1, width - 1), min(r0 + 1, height - 1)
        c0, r0 = max(c0, 0), max(r0, 0)
        values = array[r0:r1 + 1, c0:c1 + 1]
        if values.size == 0 or np.all(np.isnan(values)):
            samples.append([round(lon, 6), round(lat, 6), None])
            continue
        value = float(np.nanmean(values))
        if math.isnan(value):
            samples.append([round(lon, 6), round(lat, 6), None])
        else:
            samples.append([round(lon, 6), round(lat, 6), value])
    return samples


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    store = None
    tile_cache = None

    def log_message(self, fmt, *args):
        message = fmt % args
        if " 404 " in message or " 500 " in message:
            print("[dem-compare]", message)

    def _send(self, code, body, content_type, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(part)
                 for part in parsed.path.strip("/").split("/")]
        query = urllib.parse.parse_qs(parsed.query)
        try:
            self._route(parts, query)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._json({"error": str(exc)}, code=500)

    def do_HEAD(self):
        self.do_GET()

    def _route(self, parts, query):
        if not parts or parts[0] in ("", "index.html"):
            self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
            return
        head = parts[0]
        if head == "api" and len(parts) > 1:
            self._api(parts[1:], query)
            return
        if head == "tile" and len(parts) == 5:
            mode = query.get("mode", ["hillshade"])[0]
            self._tile(parts[1], parts[2], parts[3], parts[4], mode)
            return
        if head == "diff" and len(parts) == 6:
            self._diff(parts[1], parts[2], parts[3], parts[4], parts[5])
            return
        self._json({"error": "not found"}, code=404)

    def _api(self, parts, query):
        action = parts[0]
        if action == "datasets":
            self._json({
                "datasets": [dict(key=key, **value)
                             for key, value in DATASETS.items()],
                "default_a": DEFAULT_A,
                "default_b": DEFAULT_B,
            })
            return
        ds = query.get("ds", [""])[0]
        if ds not in DATASETS:
            self._json({"error": "unknown dataset"}, code=404)
            return
        if action == "value":
            lon = float(query.get("lon", ["0"])[0])
            lat = float(query.get("lat", ["0"])[0])
            result = sample_value(self.store, ds, lon, lat)
            result.update({"dataset": ds, "datum": DATASETS[ds]["datum"],
                           "kind": DATASETS[ds]["kind"]})
            self._json(result)
            return
        if action == "info":
            lon = float(query.get("lon", ["0"])[0])
            lat = float(query.get("lat", ["0"])[0])
            result = sample_value(self.store, ds, lon, lat)
            self._json({
                "dataset": ds,
                "has_data": bool(result and result.get("elevation") is not None),
                "native_x_m": result.get("native_x_m"),
                "native_y_m": result.get("native_y_m"),
                "datum": DATASETS[ds]["datum"],
            })
            return
        if action == "profile":
            lon1 = float(query.get("lon1", ["0"])[0])
            lat1 = float(query.get("lat1", ["0"])[0])
            lon2 = float(query.get("lon2", ["0"])[0])
            lat2 = float(query.get("lat2", ["0"])[0])
            count = int(query.get("n", ["200"])[0])
            self._json({"dataset": ds,
                        "samples": sample_profile(self.store, ds, lon1, lat1,
                                                  lon2, lat2, count)})
            return
        self._json({"error": "unknown api"}, code=404)

    def _tile(self, ds, z, x, y, mode):
        if ds not in DATASETS:
            self._json({"error": "unknown dataset"}, code=404)
            return
        z, x, y = int(z), int(x), int(y.split(".")[0])
        cache_file = (self.tile_cache / f"v{CACHE_VERSION}" / ds / mode /
                      str(z) / str(x) / f"{y}.png")
        if cache_file.exists():
            self._send(200, cache_file.read_bytes(), "image/png")
            return
        started = time.monotonic()
        result = render_tile(self.store, ds, z, x, y, mode)
        if result is None or result[0] is None:
            self._send(404, b"", "text/plain")
            return
        data, native = result
        if len(data) < 256:
            # Пустой результат — источник не отдался: не кэшируем.
            self._send(404, b"", "text/plain")
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        print(f"[dem-compare] tile {ds}/{mode} {z}/{x}/{y} "
              f"{time.monotonic() - started:.2f}s")
        self._send(200, data, "image/png",
                   {"X-Native-X": f"{native[0]:.2f}",
                    "X-Native-Y": f"{native[1]:.2f}"})

    def _diff(self, ds_a, ds_b, z, x, y):
        z, x, y = int(z), int(x), int(y.split(".")[0])
        cache_file = (self.tile_cache / f"v{CACHE_VERSION}" / "diff" /
                      f"{ds_a}_{ds_b}" / str(z) / str(x) / f"{y}.png")
        if cache_file.exists():
            self._send(200, cache_file.read_bytes(), "image/png")
            return
        data = render_diff(self.store, ds_a, ds_b, z, x, y)
        if data is None or len(data) < 256:
            self._send(404, b"", "text/plain")
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        self._send(200, data, "image/png")


def main():
    parser = argparse.ArgumentParser(description="Сервер сравнения DEM")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = parser.parse_args()

    cache = Path(args.cache).expanduser()
    store = SourceStore(cache)
    Handler.store = store
    Handler.tile_cache = cache / "tiles"
    Handler.tile_cache.mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dem-compare: http://{args.host}:{args.port}/  (кэш: {cache})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
