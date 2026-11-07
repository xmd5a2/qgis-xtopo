#!/usr/bin/env python3
"""Локальный сервер тайлов для сравнения DEM-наборов (инструмент разработки).

Отдаёт страницу ``tools/dem_compare.html`` и рендерит тайлы подложки
(рельеф) из настоящих наборов данных: SRTM v3, Copernicus DEM GLO-30,
NASADEM, ALOS AW3D30 — все с разрешением 1″ (30 м) или лучше.
Исходники тянутся по требованию и кладутся в кэш.

Запуск::

    python3 tools/dem_compare_server.py [--port 8765] [--cache DIR]
    python3 tools/dem_compare_server.py --precache [--precache-modes ...]

Прогрев кэша (``--precache``) скачивает исходные данные для всех точек
страницы и рисует тайлы, после чего сервер работает без обращений в сеть.

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
import hashlib
import json
import math
import re
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

# Параллельные запросы блоков COG и многопоточная перепроекция.
gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
gdal.SetConfigOption("GDAL_CACHEMAX", "512")

TILE_SIZE = 256
# Версия кэша тайлов: меняется, когда меняется рендер. Старые файлы не
# удаляются, просто перестают использоваться.
CACHE_VERSION = 5
# Подгрузка тайла с запасом, чтобы шов между тайлами не был виден.
TILE_OVERLAP = 1
# Ограничение на размер промежуточного растра (пиксели по стороне).
MAX_GRID = 2048

# Счётчик для уникальных имён временных файлов.
_counter = 0
_counter_lock = threading.Lock()

# Состояние сервера для индикатора на странице.
STATUS = {"started": time.time(), "active": {}, "rendered": 0, "cached": 0}
STATUS_LOCK = threading.Lock()


def status_snapshot():
    """Что сейчас делает сервер: сколько тайлов рисуется, что в кэше."""
    with STATUS_LOCK:
        active = list(STATUS["active"].values())
        return {
            "uptime": round(time.time() - STATUS["started"], 1),
            "active": active,
            "rendered": STATUS["rendered"],
            "cached": STATUS["cached"],
        }


def _status_enter(key, dataset, z, x, y):
    with STATUS_LOCK:
        STATUS["active"][key] = {
            "key": key, "dataset": dataset, "z": z, "x": x, "y": y,
            "since": round(time.time() - STATUS["started"], 1),
        }


def _status_leave(key, rendered):
    with STATUS_LOCK:
        STATUS["active"].pop(key, None)
        if rendered:
            STATUS["rendered"] += 1
        else:
            STATUS["cached"] += 1

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
        "short": "SRTM v3",
        "provider": "NASA/USGS, зеркало AWS elevation-tiles-prod (skadi)",
        "source": "skadi",
        "kind": "surface",
        "datum": "EGM96, ортометрические высоты",
        "note": "SRTMGL1.003, пустоты заполнены. Текущий источник плагина.",
        "attribution": "SRTM data courtesy of the U.S. Geological Survey",
        "src_nodata": -32768,
        "resolution_m": 30.0,
    },
    "cop30": {
        "title": "Copernicus DEM GLO-30 (1″)",
        "short": "Copernicus GLO-30",
        "provider": "ESA/Copernicus, бакет AWS copernicus-dem-30m",
        "source": "cop30",
        "kind": "surface",
        "datum": "EGM2008, высоты над геоидом",
        "note": "TanDEM-X. Публичная часть: часть тайлов не отдана (Армения, "
                "Азербайджан, СЗ Иран, Туркменистан).",
        "attribution": "Produced using Copernicus data and information "
                       "funded by the European Union and ESA",
        "src_nodata": None,
        "resolution_m": 30.0,
    },
    "nasadem": {
        "title": "NASADEM HGT (1″)",
        "short": "NASADEM",
        "provider": "NASA/JPL/USGS, зеркало Planetary Computer",
        "source": "nasadem",
        "kind": "surface",
        "datum": "EGM96, ортометрические высоты",
        "note": "SRTM v3 с доработкой NASA. Покрытие только −56°…+61° широты.",
        "attribution": "NASADEM — courtesy of NASA/JPL/USGS",
        "src_nodata": -32768,
        "resolution_m": 30.0,
    },
    "alos30": {
        "title": "ALOS World 3D-30m (1″)",
        "short": "ALOS AW3D30",
        "provider": "JAXA, зеркало Planetary Computer",
        "source": "alos30",
        "kind": "surface",
        "datum": "EGM2008, высоты над геоидом",
        "note": "DSM: включает растительность и постройки. Нужна ссылка на JAXA.",
        "attribution": "ALOS World 3D-30m — © JAXA",
        "src_nodata": None,
        "resolution_m": 30.0,
    },
    "3dep": {
        "title": "USGS 3DEP Seamless (1/3″, 10 м)",
        "short": "3DEP 10 м",
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
        "resolution_m": 10.0,
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
        # Открытые источники: GDAL держит блоки COG в своём кэше, поэтому
        # соседние тайлы читаются без повторной сети.
        self._datasets = {}
        # Локальные копии исходников (см. prepare_region): индекс и его mtime.
        self._prepared_lock = threading.Lock()
        self._prepared_cache = {}

    def open_source(self, path):
        """Открывает (и запоминает) источник: возвращает (dataset, lock)."""
        path = str(path)
        with self._lock:
            entry = self._datasets.get(path)
            if entry is not None:
                dataset, lock, expiry = entry
                if expiry == 0 or expiry > time.time() + 60:
                    return dataset, lock
        dataset = gdal.Open(path)
        if dataset is None:
            return None, None
        lock = threading.Lock()
        expiry = 0
        if "?se=" in path:
            import urllib.parse as _parse
            query = _parse.urlparse(path).query
            stamp = _parse.parse_qs(query).get("se", [""])[0]
            if stamp:
                try:
                    expiry = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
                except (ValueError, OverflowError):
                    expiry = time.time() + 1800
        with self._lock:
            self._datasets[path] = (dataset, lock, expiry)
        return dataset, lock

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
        prepared = self._prepared_for(ds, bbox)
        if prepared:
            return prepared
        return self._remote_sources(ds, bbox)

    def _remote_sources(self, ds, bbox):
        """Источники в хранилище: без локальной копии, только сеть."""
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

    # ------------------------------------------------------------------
    # Локальные копии исходников
    # ------------------------------------------------------------------

    def prepared_dir(self, ds):
        return self.cache_dir / "prepared" / f"v{CACHE_VERSION}" / ds

    def _prepared_index(self, ds):
        """Список локальных копий набора: [{"file": ..., "bbox": [...]}]."""
        path = self.prepared_dir(ds) / "index.json"
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return []
        with self._lock:
            cached = self._prepared_cache.get(ds)
        if cached and cached[0] == stamp:
            return cached[1]
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        with self._lock:
            self._prepared_cache[ds] = (stamp, items)
        return items

    def _prepared_for(self, ds, bbox):
        """Наименьшая локальная копия, покрывающая область (если есть)."""
        minx, miny, maxx, maxy = bbox
        # Допуск ~0.1 м: координаты в индексе округлены.
        tolerance = 1e-6
        best = None
        best_area = None
        for item in self._prepared_index(ds):
            area = item.get("bbox")
            if not area or len(area) != 4:
                continue
            if not (area[0] <= minx + tolerance and
                    area[1] <= miny + tolerance and
                    area[2] >= maxx - tolerance and
                    area[3] >= maxy - tolerance):
                continue
            path = self.prepared_dir(ds) / item.get("file", "")
            if not path.exists():
                continue
            size = (area[2] - area[0]) * (area[3] - area[1])
            if best is None or size < best_area:
                best = path
                best_area = size
        return [str(best)] if best else None

    def _prepared_exact(self, ds, bbox):
        """Локальная копия ровно этой области (для прогрева)."""
        minx, miny, maxx, maxy = bbox
        tolerance = 1e-6
        for item in self._prepared_index(ds):
            area = item.get("bbox")
            if not area or len(area) != 4:
                continue
            if (abs(area[0] - minx) <= tolerance and
                    abs(area[1] - miny) <= tolerance and
                    abs(area[2] - maxx) <= tolerance and
                    abs(area[3] - maxy) <= tolerance):
                path = self.prepared_dir(ds) / item.get("file", "")
                if path.exists():
                    return path
        return None

    def prepare_region(self, ds, bbox, name):
        """Сохраняет исходные данные области в локальный GeoTIFF.

        COG-наборы читаются по HTTP Range, и без локальной копии каждый
        запуск сервера снова тянет блоки из сети. Копия режется по области
        и хранится в родном разрешении источника, поэтому она небольшая.
        """
        found = self._prepared_exact(ds, bbox)
        if found:
            return found
        sources = self._remote_sources(ds, bbox)
        if not sources:
            return None
        folder = self.prepared_dir(ds)
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(
            ",".join(f"{v:.8f}" for v in bbox).encode("ascii")).hexdigest()[:8]
        target = folder / f"{name}_{digest}.tif"
        temporary = folder / f"{name}_{digest}.tmp.tif"
        options = gdal.WarpOptions(
            format="GTiff",
            dstSRS="EPSG:4326",
            outputBounds=tuple(bbox),
            resampleAlg="bilinear",
            srcNodata=DATASETS[ds].get("src_nodata"),
            dstNodata=DATASETS[ds].get("src_nodata"),
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES",
                             "BIGTIFF=IF_SAFER"],
        )
        try:
            result = gdal.Warp(str(temporary), sources, options=options)
        except Exception as exc:
            print(f"[dem-compare] {ds}: не удалось сохранить область: {exc}")
            temporary.unlink(missing_ok=True)
            return None
        if result is None:
            temporary.unlink(missing_ok=True)
            return None
        result.FlushCache()
        result = None
        temporary.replace(target)
        self._register_prepared(ds, target.name, bbox)
        return target
    def _register_prepared(self, ds, filename, bbox):
        folder = self.prepared_dir(ds)
        folder.mkdir(parents=True, exist_ok=True)
        index_path = folder / "index.json"
        with self._prepared_lock:
            try:
                items = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                items = []
            if not isinstance(items, list):
                items = []
            items = [item for item in items if item.get("file") != filename]
            items.append({"file": filename,
                          "bbox": [round(float(v), 8) for v in bbox]})
            temporary = index_path.with_name("index.tmp")
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            temporary.replace(index_path)
            with self._lock:
                self._prepared_cache.pop(ds, None)

    def open_at(self, ds, lon, lat):
        """Открывает набор так, чтобы в него попала точка."""
        prepared = self._prepared_for(ds, (lon, lat, lon, lat))
        if prepared:
            dataset, _ = self.open_source(prepared[0])
            if dataset is not None:
                return dataset
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
            dataset, _ = self.open_source(path)
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


def _dem3857(store, ds, z, x, y):
    """DEM набора в Web Mercator, тайл 256×256 float32.

    Промежуточный растр кэшируется на диске: повторный запрос того же
    тайла (другой режим, разность высот, соседний режим) источник не
    читает заново.
    """
    cache_file = (store.cache_dir / "dem3857" / f"v{CACHE_VERSION}" / ds /
                  str(z) / str(x) / f"{y}.npy")
    if cache_file.exists():
        try:
            return np.load(cache_file)
        except (OSError, ValueError):
            cache_file.unlink(missing_ok=True)
    lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
    paths = store.sources(ds, (lon1, lat1, lon2, lat2))
    if not paths:
        return None
    opened = []
    for path in paths:
        dataset, lock = store.open_source(path)
        if dataset is None:
            continue
        opened.append((dataset, lock))
    if not opened:
        return None
    bounds = _mercator_bounds(lon1, lat1, lon2, lat2)
    options = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:3857",
        outputBounds=bounds,
        width=TILE_SIZE,
        height=TILE_SIZE,
        resampleAlg="bilinear",
        srcNodata=DATASETS[ds].get("src_nodata"),
        dstNodata=float("nan"),
        multithread=True,
    )
    # Один исходник — один поток: GDAL-датасет не потокобезопасен.
    for _, lock in opened:
        lock.acquire()
    try:
        warped = gdal.Warp("", [dataset for dataset, _ in opened],
                           options=options)
    finally:
        for _, lock in reversed(opened):
            lock.release()
    if warped is None:
        return None
    array = warped.GetRasterBand(1).ReadAsArray().astype("float32")
    warped = None
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        np.save(cache_file, array)
    except OSError:
        pass
    return array


def _hillshade(values, lat_center, zoom):
    """Отмывка по растру Web Mercator.

    В Mercator шаг по долготе короче наземного в cos(широты) раз — без
    этой поправки склоны выходят плоскими.
    """
    if values is None:
        return None
    valid = ~np.isnan(values)
    filled = np.where(valid, values, 0.0).astype("float64")
    res = 156543.03392 / (2 ** zoom)
    step_x = res / max(math.cos(math.radians(lat_center)), 1e-6)
    step_y = res
    dzdx = np.empty_like(filled)
    dzdx[:, 1:-1] = (filled[:, 2:] - filled[:, :-2]) / 2.0
    dzdx[:, 0] = filled[:, 1] - filled[:, 0]
    dzdx[:, -1] = filled[:, -1] - filled[:, -2]
    dzdy = np.empty_like(filled)
    dzdy[1:-1, :] = (filled[:-2, :] - filled[2:, :]) / 2.0
    dzdy[0, :] = filled[0, :] - filled[1, :]
    dzdy[-1, :] = filled[-1, :] - filled[-2, :]
    dzdx *= step_x
    dzdy *= step_y
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    zenith = math.radians(90.0 - 45.0)
    azimuth = math.radians(315.0)
    shade = 255.0 * (math.cos(zenith) * np.cos(slope) +
                     math.sin(zenith) * np.sin(slope) * np.cos(azimuth - aspect))
    shade = np.clip(shade, 0, 255).astype("uint8")
    shade[~valid] = 0
    return shade


# Фиксированная гипсометрическая шкала: цвета сравнимы между тайлами.
HYPSO_STOPS = [
    (-200.0, (36, 78, 84)),
    (0.0, (58, 106, 62)),
    (300.0, (110, 155, 92)),
    (800.0, (176, 196, 132)),
    (1500.0, (222, 219, 168)),
    (2500.0, (196, 172, 122)),
    (3500.0, (146, 116, 84)),
    (4500.0, (122, 96, 84)),
    (5500.0, (150, 140, 138)),
    (6500.0, (232, 232, 236)),
]


def _elevation_colors(values):
    """Гипсометрия по фиксированной шкале."""
    if values is None:
        return None
    valid = ~np.isnan(values)
    value = np.where(valid, values, 0.0).astype("float64")
    rgb = np.zeros(value.shape + (3,), dtype="uint8")
    for index in range(len(HYPSO_STOPS) - 1):
        low, color_low = HYPSO_STOPS[index]
        high, color_high = HYPSO_STOPS[index + 1]
        mask = (value >= low) & (value < high)
        if not mask.any():
            continue
        t = ((value[mask] - low) / (high - low))[:, None]
        rgb[mask] = (np.array(color_low)[None, :] * (1 - t) +
                     np.array(color_high)[None, :] * t).astype("uint8")
    rgb[value >= HYPSO_STOPS[-1][0]] = HYPSO_STOPS[-1][1]
    rgb[~valid] = (0, 0, 0)
    return rgb


def _contour_mask(values, step):
    """Маска горизонталей методом marching squares по сетке высот."""
    if values is None:
        return None
    valid = ~np.isnan(values)
    if not valid.any():
        return None
    data = np.where(valid, values, np.nan)
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


def _png_bytes(rgb, alpha=None):
    """Собирает PNG из массива RGB(A) без промежуточного растра."""
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
    png_path = _temp_path("tile").replace(".tif", ".png")
    png = gdal.GetDriverByName("PNG").CreateCopy(png_path, mem)
    mem = None
    if png is None:
        return None
    png = None
    data = gdal.VSIGetMemFileBuffer_unsafe(png_path)
    if data is None:
        gdal.Unlink(png_path)
        return None
    result = bytes(data)
    gdal.Unlink(png_path)
    return result


# ---------------------------------------------------------------------------
# Рендер тайлов
# ---------------------------------------------------------------------------

def tile_cache_path(tile_cache, ds, mode, z, x, y):
    """Путь готового тайла в кэше (общий для сервера и прогрева)."""
    return (Path(tile_cache) / f"v{CACHE_VERSION}" / ds / mode /
            str(z) / str(x) / f"{y}.png")


def render_tile(store, ds, z, x, y, mode="hillshade"):
    values = _dem3857(store, ds, z, x, y)
    if values is None:
        return None, None
    valid = ~np.isnan(values)
    lat_center = (tile_bounds(z, x, y)[1] + tile_bounds(z, x, y)[3]) / 2
    if mode == "elevation":
        rgb = _elevation_colors(values)
    elif mode == "contours":
        rgb = _elevation_colors(values)
        shade = _hillshade(values, lat_center, z)
        if shade is not None:
            factor = (shade.astype("float64") / 255.0 * 0.6 + 0.4)[:, :, None]
            rgb = np.clip(rgb.astype("float64") * factor, 0, 255).astype("uint8")
        step = _nice_step(156543.03392 / (2 ** z) * 8)
        mask = _contour_mask(values, step)
        if mask is not None:
            rgb[mask > 0] = (58, 38, 28)
    else:
        shade = _hillshade(values, lat_center, z)
        if shade is None:
            return None, None
        rgb = np.dstack([shade, shade, shade])
    alpha = np.where(valid, 255, 0).astype("uint8")
    data = _png_bytes(rgb, alpha)
    if data is None:
        return None, None
    native = (156543.03392 / (2 ** z) / max(math.cos(math.radians(lat_center)),
                                            1e-6),
              156543.03392 / (2 ** z))
    return data, native


def render_diff(store, ds_a, ds_b, z, x, y):
    values_a = _dem3857(store, ds_a, z, x, y)
    values_b = _dem3857(store, ds_b, z, x, y)
    if values_a is None or values_b is None:
        return None
    diff = values_b - values_a
    valid = (~np.isnan(values_a)) & (~np.isnan(values_b))
    rgb = _diff_colors(diff, valid)
    alpha = np.where(valid, 255, 0).astype("uint8")
    return _png_bytes(rgb, alpha)


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
# Предварительный прогрев кэша
# ---------------------------------------------------------------------------

# Точки берутся из страницы, чтобы список не дублировался в двух файлах.
_POINT_RE = re.compile(
    r'\{name:\s*"(?P<name>[^"]+)".*?'
    r'lon:\s*(?P<lon>-?\d+(?:\.\d+)?)\s*,\s*'
    r'lat:\s*(?P<lat>-?\d+(?:\.\d+)?)\s*,\s*'
    r'zoom:\s*(?P<zoom>\d+)',
    re.S,
)


def load_points(page=None):
    """Точки сравнения из dem_compare.html."""
    path = Path(page) if page else Path(__file__).with_name("dem_compare.html")
    text = path.read_text(encoding="utf-8")
    points = []
    for match in _POINT_RE.finditer(text):
        points.append({
            "name": match.group("name"),
            "lon": float(match.group("lon")),
            "lat": float(match.group("lat")),
            "zoom": int(match.group("zoom")),
        })
    if not points:
        raise SystemExit(f"Не нашёл список точек в {path}")
    return points


def parse_view(text):
    match = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", text)
    if not match:
        raise SystemExit("Размер окна задаётся как ШИРИНАxВЫСОТА, "
                         "например 1400x900")
    return int(match.group(1)), int(match.group(2))


def _world_pixel(lon, lat, zoom):
    scale = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def view_bbox(lon, lat, zoom, width, height, margin=1, pad=0.02):
    """Область всех тайлов, которые страница запросит для окна.

    Границы берутся по сетке тайлов, а не по самому окну: страница рисует
    на один тайл шире окна, и без этого края оставались бы без локальной
    копии и снова шли в сеть.
    """
    px, py = _world_pixel(lon, lat, zoom)
    left = px - width / 2.0
    top = py - height / 2.0
    count = 2 ** zoom
    first_x = math.floor(left / TILE_SIZE) - margin
    last_x = math.floor((left + width) / TILE_SIZE) + margin
    first_y = max(0, math.floor(top / TILE_SIZE) - margin)
    last_y = min(count - 1, math.floor((top + height) / TILE_SIZE) + margin)
    lon1 = first_x / count * 360.0 - 180.0
    lon2 = (last_x + 1) / count * 360.0 - 180.0
    lat1 = _merc_y_to_lat((last_y + 1) / count)
    lat2 = _merc_y_to_lat(first_y / count)
    dx = (lon2 - lon1) * pad
    dy = (lat2 - lat1) * pad
    return (lon1 - dx, lat1 - dy, lon2 + dx, lat2 + dy)


def visible_tiles(lon, lat, zoom, width, height, margin=1):
    """Тайлы, которые страница запросит для окна вокруг точки."""
    px, py = _world_pixel(lon, lat, zoom)
    left = px - width / 2.0
    top = py - height / 2.0
    count = 2 ** zoom
    first_x = math.floor(left / TILE_SIZE) - margin
    last_x = math.floor((left + width) / TILE_SIZE) + margin
    first_y = math.floor(top / TILE_SIZE) - margin
    last_y = math.floor((top + height) / TILE_SIZE) + margin
    for tx in range(first_x, last_x + 1):
        wrapped = tx % count
        for ty in range(max(0, first_y), min(count - 1, last_y) + 1):
            yield wrapped, ty


def native_zoom(lat, resolution_m):
    return math.log2(156543.03392 * math.cos(math.radians(lat)) /
                     resolution_m)


def zoom_range(point_zoom, lat, resolution_m):
    """Зумы от вида по умолчанию до полного разрешения источника.

    Кнопка «1:1» ставит дробный зум, а страница рисует тайлы округлённого
    зума, поэтому границей считается round, а не floor/ceil.
    """
    native = round(native_zoom(lat, resolution_m))
    return min(point_zoom, native), max(point_zoom, native)


def precache(store, tile_cache, points, modes=("hillshade",),
             view=(1400, 900)):
    """Скачивает исходники и рисует тайлы для всех точек страницы.

    Сначала для каждого зума сохраняется локальная копия области в родном
    разрешении источника (для SRTM — просто скачиваются .hgt.gz), потом
    рисуются тайлы. После этого страница открывается без обращений в сеть.
    """
    width, height = view
    started = time.monotonic()
    total_rendered = 0
    total_cached = 0
    total_empty = 0
    print(f"Прогрев кэша: точек {len(points)}, наборов {len(DATASETS)}, "
          f"режимы {', '.join(modes)}, окно {width}×{height}")
    for index, point in enumerate(points, 1):
        lon, lat, zoom = point["lon"], point["lat"], point["zoom"]
        print(f"\n[{index}/{len(points)}] {point['name']}")
        for key, spec in DATASETS.items():
            low, high = zoom_range(zoom, lat, spec["resolution_m"])
            print(f"  {spec['short']:<18} z{low}–{high}")
            source_seconds = 0.0
            ready = True
            for z in range(low, high + 1):
                bbox = view_bbox(lon, lat, z, width, height)
                moment = time.monotonic()
                if spec["source"] == "skadi":
                    ok = bool(store.sources(key, bbox))
                else:
                    ok = store.prepare_region(
                        key, bbox, f"p{index:02d}_z{z}") is not None
                source_seconds += time.monotonic() - moment
                if not ok:
                    ready = False
                    break
            if not ready:
                print(f"    исходники: нет данных ({source_seconds:.1f} с)")
                continue
            print(f"    исходники: готовы ({source_seconds:.1f} с)")
            for z in range(low, high + 1):
                tiles = list(visible_tiles(lon, lat, z, width, height))
                for done, (x, y) in enumerate(tiles, 1):
                    for mode in modes:
                        path = tile_cache_path(tile_cache, key, mode, z, x, y)
                        if path.exists():
                            total_cached += 1
                            continue
                        try:
                            data, _native = render_tile(store, key, z, x, y,
                                                        mode)
                        except Exception as exc:
                            print(f"\n      ошибка {z}/{x}/{y}: {exc}")
                            continue
                        if data is None or len(data) < 256:
                            total_empty += 1
                            continue
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        total_rendered += 1
                    if done % 8 == 0 or done == len(tiles):
                        print(f"\r    тайлы z{z}: {done}/{len(tiles)}",
                              end="", flush=True)
                print()
    elapsed = time.monotonic() - started
    print(f"\nГотово за {elapsed:.0f} с: отрисовано {total_rendered}, "
          f"из кэша {total_cached}, без данных {total_empty}")
    print(f"Кэш: {store.cache_dir}")


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
        if action == "status":
            snapshot = status_snapshot()
            snapshot["datasets"] = list(DATASETS)
            self._json(snapshot)
            return
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
        cache_file = tile_cache_path(self.tile_cache, ds, mode, z, x, y)
        if cache_file.exists():
            _status_leave(f"{ds}/{mode}/{z}/{x}/{y}", rendered=False)
            self._send(200, cache_file.read_bytes(), "image/png",
                       {"X-Tile-Status": "cached", "X-Tile-Seconds": "0"})
            return
        key = f"{ds}/{mode}/{z}/{x}/{y}"
        _status_enter(key, ds, z, x, y)
        started = time.monotonic()
        try:
            result = render_tile(self.store, ds, z, x, y, mode)
        finally:
            _status_leave(key, rendered=True)
        seconds = time.monotonic() - started
        if result is None or result[0] is None:
            self._send(404, b"", "text/plain",
                       {"X-Tile-Status": "empty", "X-Tile-Seconds": f"{seconds:.2f}"})
            return
        data, native = result
        if len(data) < 256:
            # Пустой результат — источник не отдался: не кэшируем.
            self._send(404, b"", "text/plain",
                       {"X-Tile-Status": "empty", "X-Tile-Seconds": f"{seconds:.2f}"})
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        print(f"[dem-compare] tile {ds}/{mode} {z}/{x}/{y} {seconds:.2f}s")
        self._send(200, data, "image/png",
                   {"X-Native-X": f"{native[0]:.2f}",
                    "X-Native-Y": f"{native[1]:.2f}",
                    "X-Tile-Status": "rendered",
                    "X-Tile-Seconds": f"{seconds:.2f}"})

    def _diff(self, ds_a, ds_b, z, x, y):
        z, x, y = int(z), int(x), int(y.split(".")[0])
        cache_file = (self.tile_cache / f"v{CACHE_VERSION}" / "diff" /
                      f"{ds_a}_{ds_b}" / str(z) / str(x) / f"{y}.png")
        if cache_file.exists():
            self._send(200, cache_file.read_bytes(), "image/png",
                       {"X-Tile-Status": "cached", "X-Tile-Seconds": "0"})
            return
        key = f"diff/{ds_a}_{ds_b}/{z}/{x}/{y}"
        _status_enter(key, f"{ds_a}−{ds_b}", z, x, y)
        started = time.monotonic()
        try:
            data = render_diff(self.store, ds_a, ds_b, z, x, y)
        finally:
            _status_leave(key, rendered=True)
        seconds = time.monotonic() - started
        if data is None or len(data) < 256:
            self._send(404, b"", "text/plain",
                       {"X-Tile-Status": "empty", "X-Tile-Seconds": f"{seconds:.2f}"})
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
        self._send(200, data, "image/png",
                   {"X-Tile-Status": "rendered",
                    "X-Tile-Seconds": f"{seconds:.2f}"})


def page_url(host, port):
    """Адрес страницы для печати в консоли.

    Слушая 0.0.0.0, в ссылке показываем 127.0.0.1: по такому адресу
    браузер на этой же машине откроет страницу.
    """
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    return f"http://{host}:{port}/"


def main():
    parser = argparse.ArgumentParser(description="Сервер сравнения DEM")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--precache", action="store_true",
                        help="скачать исходники и отрисовать тайлы для точек "
                             "страницы, затем выйти")
    parser.add_argument("--precache-modes", default="hillshade",
                        help="режимы через запятую: hillshade,elevation,contours")
    parser.add_argument("--precache-view", default="1400x900",
                        help="размер окна страницы для прогрева (ШxВ)")
    args = parser.parse_args()

    cache = Path(args.cache).expanduser()
    store = SourceStore(cache)

    if args.precache:
        modes = [mode.strip() for mode in args.precache_modes.split(",")
                 if mode.strip()]
        unknown = [mode for mode in modes
                   if mode not in ("hillshade", "elevation", "contours")]
        if unknown:
            raise SystemExit(f"Неизвестный режим: {', '.join(unknown)}")
        precache(store, cache / "tiles", load_points(),
                 modes=modes or ("hillshade",),
                 view=parse_view(args.precache_view))
        return

    Handler.store = store
    Handler.tile_cache = cache / "tiles"
    Handler.tile_cache.mkdir(parents=True, exist_ok=True)

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"Не удалось занять {args.host}:{args.port}: {exc}")
        print("Похоже, порт уже занят. Попробуйте другой: --port 8766")
        raise SystemExit(1)
    port = server.server_address[1]

    print("Сервер сравнения DEM запущен.")
    print(f"Страница: {page_url(args.host, port)}")
    print(f"Кэш:      {cache}")
    print("Остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == "__main__":
    main()
