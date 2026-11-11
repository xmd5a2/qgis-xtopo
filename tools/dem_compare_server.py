#!/usr/bin/env python3
"""Локальный сервер тайлов для сравнения DEM-наборов (инструмент разработки).

Отдаёт страницу ``tools/dem_compare.html`` и рендерит тайлы подложки
(рельеф) из настоящих наборов данных: SRTM v3, Copernicus DEM GLO-30,
NASADEM, ALOS AW3D30, Sonny DTM — все с разрешением 1″ (30 м) или лучше.
Исходники тянутся по требованию и кладутся в кэш.

Запуск::

    python3 tools/dem_compare_server.py [--port 8765] [--cache DIR]
                                        [--temp-dir DIR] [--local-dem DIR]
                                        [--local-dem2 DIR]
    python3 tools/dem_compare_server.py --precache [--precache-tiles]
                                        [--precache-modes ...]

Прогрев кэша (``--precache``) скачивает исходные данные для всех точек
страницы; тайлы по умолчанию не рисуются — из локальных данных они
считаются на лету за десятые доли секунды. ``--precache-tiles`` рисует их
заранее, если нужен показ вообще без обращений к сети.

Эндпоинты:

* ``/api/datasets`` — список наборов с метаданными;
* ``/tile/<ds>/<z>/<x>/<y>.png?mode=hillshade|elevation|contours`` — тайл;
* ``/tiles?ds=&z=&mode=&lo=&hi=&t=x,y&t=x,y…`` — пачка тайлов по мере
  готовности (см. :meth:`Handler._tiles`);
* ``/osm/<z>/<x>/<y>.png`` — тайл подложки OpenStreetMap;
* ``/diff/<a>/<b>/<z>/<x>/<y>.png`` — разность высот B−A;
* ``/api/value?ds=&lon=&lat=`` — высота в точке;
* ``/api/profile?ds=&lon1=&lat1=&lon2=&lat2=&n=`` — профиль по отрезку;
* ``/api/info?ds=&lon=&lat=`` — разрешение источника и наличие данных.
"""

import argparse
import calendar
import contextlib
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Списки тайлов Sonny и загрузка с Google Drive живут в ядре плагина:
# не дублируем формат архивов, имена файлов и VRT-обёртку для 0,5″.
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
try:
    from core import sonny
except Exception:  # noqa: BLE001 - инструмент может работать без QGIS
    sonny = None

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()
osr.UseExceptions()

# Параллельные запросы блоков COG и многопоточная перепроекция.
gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
gdal.SetConfigOption("GDAL_CACHEMAX", "512")
# /vsicurl: без этих настроек GDAL пытается перебрать каталог контейнера в
# поисках sidecar-файлов. У ALOS на Azure это десятки секунд на открытие.
gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")
# Одновременных перепроекций тайлов: больше — сеть и кэш GDAL только
# мешают друг другу, а память растёт.
RENDER_PARALLEL = max(1, int(os.environ.get("DEM_COMPARE_SLOTS", "6")))
RENDER_SLOTS = threading.BoundedSemaphore(RENDER_PARALLEL)
# Потоковая пачка /tiles рисует несколько тайлов сразу. Пул общий на процесс:
# иначе каждая вкладка плодила бы свои потоки.
BATCH_POOL = ThreadPoolExecutor(max_workers=RENDER_PARALLEL)
# Предел тайлов в одном ответе: остальное клиент шлёт следующими пачками.
TILE_BATCH_MAX = 32

TILE_SIZE = 256
# Ниже этого зума рельеф не рисуется и не запрашивается: один тайл там
# покрывает несколько градусных тайлов источника, и загрузка данных не
# оправдана.  Совпадает с RELIEF_MIN_ZOOM на странице.
RELIEF_MIN_ZOOM = 9
# Режимы, где цвет тайла зависит от высот: их шкала задаётся пределами.
SCALED_MODES = ("elevation", "contours")
# Версия кэша: старые каталоги не удаляются, но и не используются, чтобы
# после правок рендера не отдавать тайлы, нарисованные прежним кодом.
CACHE_VERSION = 5
# Версия только готовых PNG: отмывка и гипсометрия меняются чаще, чем
# исходные окна DEM. Отдельный номер не заставит заново качать prepared.
# 8 — чёрные тайлы Sonny, нарисованные при гонке перезаписи VRT.
TILE_CACHE_VERSION = 8
# Подгрузка тайла с запасом, чтобы шов между тайлами не был виден.
TILE_OVERLAP = 1
# Ограничение на размер промежуточного растра (пиксели по стороне).
MAX_GRID = 2048

# Счётчик для уникальных имён временных файлов.
_counter = 0
_counter_lock = threading.Lock()

# Состояние сервера для индикатора на странице.
STATUS = {"started": time.time(), "active": {}, "downloads": {},
          "rendered": 0, "cached": 0}
STATUS_LOCK = threading.Lock()


def status_snapshot():
    """Что сейчас делает сервер: сколько тайлов рисуется, что в кэше."""
    now = time.time()
    with STATUS_LOCK:
        active = list(STATUS["active"].values())
        # Одинаковые метки сводим: странице важно, какие наборы и области
        # качаются, а не сколько параллельных операций их тянут.
        grouped = {}
        for item in STATUS["downloads"].values():
            entry = grouped.get(item["label"])
            if entry is None:
                grouped[item["label"]] = {"label": item["label"],
                                          "since": item["since"],
                                          "count": 1}
            else:
                entry["since"] = min(entry["since"], item["since"])
                entry["count"] += 1
        downloads = [
            {"label": item["label"], "seconds": round(now - item["since"], 1),
             "count": item["count"]}
            for item in grouped.values()
        ]
        # Дольше всего идущее — первым: именно оно объясняет паузу.
        downloads.sort(key=lambda item: -item["seconds"])
        return {
            "uptime": round(now - STATUS["started"], 1),
            "active": active,
            "downloads": downloads,
            "rendered": STATUS["rendered"],
            "cached": STATUS["cached"],
        }


def _download_enter(key, label):
    with STATUS_LOCK:
        STATUS["downloads"][key] = {"label": label, "since": time.time()}


def _download_leave(key):
    with STATUS_LOCK:
        STATUS["downloads"].pop(key, None)


@contextlib.contextmanager
def _net_read(label):
    """Отмечает сетевое чтение: в статусе видно, что именно качается."""
    key = f"net:{os.getpid()}:{threading.get_ident()}:{time.monotonic_ns()}"
    _download_enter(key, label)
    try:
        yield
    finally:
        _download_leave(key)


def _queue_size():
    """Сколько тайлов сейчас в работе (рендер или ожидание ответа)."""
    with STATUS_LOCK:
        return len(STATUS["active"])


def _tile_label(lat, lon):
    """Имя градусного тайла: N45E007, S03W062."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(int(math.floor(lat))):02d}{ew}{abs(int(math.floor(lon))):03d}"


def _cell_label(bbox):
    """Градусный тайл области по её юго-западному углу: N43E043.

    Углы округляются: в bbox гранулы Planetary Computer попадает шум
    вида 42.99999999999997, и без округления тайл назывался бы на
    градус южнее настоящего.
    """
    minx, miny, maxx, maxy = bbox
    return _tile_label(round(miny), round(minx))


def _source_key(path):
    """Ключ источника без токена и параметров: один и тот же файл — один ключ."""
    text = str(path)
    if text.startswith("/vsicurl/"):
        text = text[len("/vsicurl/"):]
    return text.split("?")[0]


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

HTML_FILE = ROOT / "dem_compare.html"

DEFAULT_CACHE = (
    Path.home() / "qgis_projects" / "qgisxtopo-cache" / "dem_compare"
)

USER_AGENT = "qgis-xtopo-dem-compare/1.0"
# Подложка OpenStreetMap: тайлы 256×256 в той же проекции, что и рельеф.
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_MAX_ZOOM = 19
OSM_LOCKS = {}
OSM_LOCKS_GUARD = threading.Lock()


def _osm_lock(key):
    with OSM_LOCKS_GUARD:
        if key not in OSM_LOCKS:
            OSM_LOCKS[key] = threading.Lock()
        return OSM_LOCKS[key]

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
    "sonny": {
        "title": "Sonny DTM LiDAR (1″, 0,5″)",
        "short": "Sonny DTM",
        "provider": "Sonny / 4lima.de, Google Drive",
        "source": "sonny",
        "kind": "bare-earth",
        "datum": "национальные системы, обычно над уровнем моря",
        "note": "LiDAR DTM: земная поверхность без растительности и "
                "построек. 0,5″ — Австрия и Швейцария, 1″ — остальная "
                "Европа. Тайлы скачиваются с Google Drive.",
        "attribution": "LiDAR Digital Terrain Models of Europe by Sonny — "
                       "https://sonny.4lima.de (CC BY 4.0)",
        # Вода и пустоты у Sonny — нули (это данные), а -32768 встречается
        # только на краях тайлов.
        "src_nodata": -32768,
        "resolution_m": 30.0,
    },
}

# Наборы для попарного сравнения по умолчанию.
DEFAULT_A = "cop30"
DEFAULT_B = "srtm3"

# Ключ набора из локального каталога: добавляется по --local-dem или из UI.
LOCAL_KEY = "local"
# Второй локальный каталог: можно сравнивать два своих набора.
LOCAL_KEY_2 = "local2"
LOCAL_KEYS = (LOCAL_KEY, LOCAL_KEY_2)
LOCAL_TITLES = {LOCAL_KEY: "Свой рельеф", LOCAL_KEY_2: "Свой рельеф 2"}

# Расширения растров, которые ищутся в локальном каталоге.
LOCAL_SUFFIXES = (".tif", ".tiff", ".hgt", ".vrt", ".img", ".asc",
                  ".dem", ".dt2", ".bil", ".grd", ".nc")


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


def _bbox_intersects(area, bbox):
    """Пересекаются ли две области (minx, miny, maxx, maxy)."""
    if not area or len(area) != 4:
        return False
    return (area[0] <= bbox[2] and area[2] >= bbox[0] and
            area[1] <= bbox[3] and area[3] >= bbox[1])


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


# SAS-токен на весь контейнер коллекции: подписывать каждый файл по
# отдельности нельзя — сервис отвечает 429, а повтор с паузой занимает
# десятки секунд на тайл.
_PC_TOKENS = {}
_PC_TOKEN_LOCK = threading.Lock()


def _pc_token(collection):
    """Действующий SAS-токен контейнера коллекции Planetary Computer."""
    with _PC_TOKEN_LOCK:
        entry = _PC_TOKENS.get(collection)
        if entry and entry[0] > time.time() + 60:
            return entry[1]
        data = _http_json("https://planetarycomputer.microsoft.com/api/"
                          f"sas/v1/token/{collection}")
        token = data.get("token", "")
        if not token:
            raise RuntimeError("сервис не выдал токен")
        expiry = time.time() + 600
        stamp = data.get("msft:expiry")
        if stamp:
            try:
                expiry = calendar.timegm(
                    time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                pass
        _PC_TOKENS[collection] = (expiry, token)
        return token


def _has_rasters(folder):
    """Есть ли в каталоге растры (для пометки в списке выбора)."""
    try:
        for child in folder.iterdir():
            if child.is_file() and child.suffix.lower() in LOCAL_SUFFIXES:
                return True
    except OSError:
        return False
    return False


def _raster_info(path):
    """Охват (EPSG:4326) и размер пикселя локального растра в метрах."""
    try:
        dataset = gdal.Open(str(path))
    except RuntimeError:
        return None
    if dataset is None:
        return None
    gt = dataset.GetGeoTransform()
    if gt is None:
        return None
    width, height = dataset.RasterXSize, dataset.RasterYSize
    srs = dataset.GetSpatialRef()
    corners = [(gt[0], gt[3]),
               (gt[0] + gt[1] * width, gt[3] + gt[5] * height)]
    minx, maxx = sorted(x for x, _ in corners)
    miny, maxy = sorted(y for _, y in corners)
    if srs is not None and not srs.IsGeographic():
        target = osr.SpatialReference()
        target.ImportFromEPSG(4326)
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        source = srs.Clone()
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(source, target)
        points = []
        for px, py in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
            lon, lat, *_ = transform.TransformPoint(px, py)
            points.append((lon, lat))
        minx = min(point[0] for point in points)
        maxx = max(point[0] for point in points)
        miny = min(point[1] for point in points)
        maxy = max(point[1] for point in points)
        resolution = abs(gt[5])
    else:
        resolution = abs(gt[5]) * 110574.0
    dataset = None
    if resolution <= 0:
        resolution = 30.0
    return (minx, miny, maxx, maxy), resolution


def _union_extent(extents):
    if not extents:
        return None
    return (min(item[0] for item in extents),
            min(item[1] for item in extents),
            max(item[2] for item in extents),
            max(item[3] for item in extents))


def _purge_tree(path):
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def register_local_source(store, directory, key=LOCAL_KEY):
    """Добавляет каталог с рельефом как набор ``local`` или ``local2``.

    Возвращает описание набора; страница обновляет список и выбирает его.
    """
    if key not in LOCAL_KEYS:
        raise ValueError(f"неизвестный локальный ключ: {key}")
    folder, items = store.set_local_dir(key, directory)
    extent = _union_extent([item[1] for item in items])
    resolution = sorted(item[2] for item in items)[len(items) // 2]
    # Тайлы и DEM в кэше привязаны к ключу: при смене каталога они
    # показывали бы прежний рельеф.
    marker = store.cache_dir / f"{key}.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
        previous = previous.get("directory", "")
    except (OSError, ValueError, AttributeError):
        previous = ""
    if previous and Path(previous).expanduser() != folder:
        _purge_tree(store.cache_dir / "dem3857" / f"v{CACHE_VERSION}" / key)
        _purge_tree(store.cache_dir / "tiles" / f"v{TILE_CACHE_VERSION}" / key)
    try:
        marker.write_text(json.dumps({"directory": str(folder)}),
                          encoding="utf-8")
    except OSError:
        pass
    base = LOCAL_TITLES[key]
    title = f"{base} ({folder.name})"
    DATASETS[key] = {
        "title": title,
        "short": base,
        "provider": "локальный каталог",
        "source": "local",
        "kind": "surface",
        "datum": "как в файлах",
        "note": f"{len(items)} растров, {folder}",
        "directory": str(folder),
        "attribution": "",
        "src_nodata": None,
        "resolution_m": resolution,
    }
    return {"key": key, "title": title, "files": len(items),
            "directory": str(folder),
            "extent": list(extent) if extent else None,
            "resolution_m": resolution}


class SourceStore:
    """Источники DEM: локальный кэш для gzip-SRTM, HTTP-чтение — для COG.

    Тайлы COG (Copernicus, NASADEM, ALOS, 3DEP) не скачиваются целиком:
    GDAL читает их по HTTP Range прямо из бакета, запрашивая только нужные
    блоки. Это важно: тайл 3DEP в 10 м — это десятки мегабайт.
    """

    def __init__(self, cache_dir: Path, temp_dir: Path = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Куда класть промежуточные загрузки; постоянный кэш — в cache_dir.
        self.temp_dir = Path(temp_dir) if temp_dir else None
        # Локальные каталоги с рельефом: ключ → (папка, индекс «файл → охват»).
        self._local = {}
        self._local_lock = threading.Lock()
        self._lock = threading.Lock()
        self._download_locks = {}
        self._signs = {}
        self._sign_lock = threading.Lock()
        # Открытые источники: GDAL держит блоки COG в своём кэше, поэтому
        # соседние тайлы читаются без повторной сети.
        self._datasets = {}
        # Локальные копии исходников (см. prepare_region): индекс и его mtime.
        self._prepared_lock = threading.Lock()
        self._prepared_cache = {}
        self._prep_locks = {}
        self._pc_locks = {}
        # Один и тот же тайл могут запросить несколько вкладок сразу.
        self._render_locks = {}
        # Отсутствующие тайлы COG: запрос 404 стоит секунды, а спрашивают
        # его на каждом тайле области. Помним, что источника нет.
        self._missing = {}
        self._missing_ttl = 3600.0
        # Код гранулы для каждого удалённого источника: путь → «N43E043».
        # По нему статус страницы пишет, что именно качается из сети.
        self._source_labels = {}
        # Список тайлов Sonny грузится один раз на процесс: он общий с
        # плагином, а при первом запуске на машине берётся с Google Drive.
        self._sonny_catalogue = None
        self._sonny_lock = threading.Lock()
        self._sonny_warned = False

    def remember_source(self, path, label):
        """Запоминает подпись удалённого источника: «ALOS AW3D30 · N43E043»."""
        with self._lock:
            self._source_labels[_source_key(path)] = label

    def source_label(self, path):
        """Подпись удалённого источника; пусто, если источник локальный."""
        with self._lock:
            return self._source_labels.get(_source_key(path), "")

    def open_source(self, path):
        """Открывает (и запоминает) источник: возвращает (dataset, lock)."""
        path = str(path)
        with self._lock:
            entry = self._datasets.get(path)
            if entry is not None:
                dataset, lock, expiry = entry
                if expiry == 0 or expiry > time.time() + 60:
                    return dataset, lock
        try:
            dataset = gdal.Open(path)
        except RuntimeError:
            # Нет файла в хранилище: это нормальный случай, у GLO-30 часть
            # тайлов не опубликована.
            dataset = None
        if dataset is None:
            self._remember_missing(path)
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

    def _remember_missing(self, path):
        with self._lock:
            self._missing[path] = time.time()

    def is_missing(self, path):
        """Источник уже проверяли и его нет (404 или нет файла)."""
        path = str(path)
        with self._lock:
            stamp = self._missing.get(path)
            if stamp is None:
                return False
            if time.time() - stamp > self._missing_ttl:
                self._missing.pop(path, None)
                return False
            return True

    def _signed(self, href):
        """Подпись SAS кэшируется: подписывать каждый раз нельзя — 429."""
        # Подпись — сетевой запрос; при параллельном прогреве два потока
        # могут одновременно промахнуться по кэшу и получить 429.
        with self._sign_lock:
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

    def _named_lock(self, table, key):
        with self._lock:
            if key not in table:
                table[key] = threading.Lock()
            return table[key]

    def local_path(self, ds, lat, lon):
        folder = self.cache_dir / ds
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{_tile_name(lat, lon)}.tif"

    def sources(self, ds, bbox, allow_download=True):
        """Список источников для области: локальные файлы и /vsicurl.

        ``allow_download=False`` разрешает только уже закэшированные данные:
        ни одного нового сетевого запроса за исходниками.
        """
        prepared = self._prepared_for(ds, bbox)
        if prepared:
            return prepared
        return self._remote_sources(ds, bbox, allow_download)

    # Промах мимо прогретого окна раньше уходил в /vsicurl на каждый тайл:
    # сдвиг карты на пару десятых градуса стоил 20–50 с на тайл. Поэтому
    # недостающая область один раз сохраняется локально блоком примерно в
    # AUTO_BLOCK_PIXELS пикселей источника — соседние тайлы попадают в один
    # блок и сеть дёргается один раз.
    AUTO_BLOCK_PIXELS = 1024
    AUTO_BLOCK_PAD = 0.01
    AUTO_BLOCK_LIMIT = 160

    def tile_sources(self, ds, bbox, allow_download=True):
        """Источники для одного тайла: прогретое окно или автоблок."""
        prepared = self._prepared_for(ds, bbox)
        if prepared:
            return prepared
        if allow_download and self._auto_block_fits(ds, bbox):
            block = self._auto_block(ds, bbox)
            if block:
                return block
        return self._remote_sources(ds, bbox, allow_download)

    def _auto_block_fits(self, ds, bbox):
        """Автоблок имеет смысл только у родного разрешения.

        На мелком зуме COG отдаёт обзорные уровни: выкачивать окно в полном
        разрешении — в разы больше данных, чем нужно для 256-пиксельного
        тайла.
        """
        resolution = DATASETS[ds].get("resolution_m") or 30.0
        lon1, lat1, lon2, lat2 = bbox
        pixel = (lon2 - lon1) * 111320.0 * max(
            math.cos(math.radians((lat1 + lat2) / 2.0)), 0.05) / TILE_SIZE
        return pixel <= resolution * 3.0

    def set_local_dir(self, key, directory):
        """Индексирует каталог с рельефом: файл → охват и размер пикселя."""
        folder = Path(directory).expanduser()
        if not folder.is_dir():
            raise ValueError(f"нет каталога: {folder}")
        items = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in LOCAL_SUFFIXES:
                continue
            info = _raster_info(path)
            if info is None:
                continue
            items.append((path, info[0], info[1]))
        if not items:
            raise ValueError(f"в каталоге нет растров: {folder}")
        with self._local_lock:
            self._local[key] = (folder, items)
        return folder, items

    def local_sources(self, key, bbox):
        """Файлы локального каталога, пересекающиеся с областью."""
        minx, miny, maxx, maxy = bbox
        with self._local_lock:
            entry = self._local.get(key)
        items = list(entry[1]) if entry else []
        return [str(path) for path, area, _resolution in items
                if area[0] <= maxx and area[2] >= minx and
                area[1] <= maxy and area[3] >= miny]

    def _auto_block(self, ds, bbox):
        spec = DATASETS[ds]
        if spec["source"] in ("skadi", "sonny", "local"):
            # SRTM и Sonny качаются целыми градусными тайлами, локальный
            # каталог уже лежит на диске — промежуточное окно им не нужно.
            return None
        resolution = spec.get("resolution_m") or 30.0
        block = self.AUTO_BLOCK_PIXELS * resolution / 111320.0
        block = min(0.5, max(0.05, block))
        lon1, lat1, lon2, lat2 = bbox
        x1, y1 = math.floor(lon1 / block), math.floor(lat1 / block)
        x2, y2 = math.floor(lon2 / block), math.floor(lat2 / block)
        # Тайл, кончающийся ровно на границе блока, не должен занимать два.
        if lon2 <= x2 * block + 1e-9:
            x2 -= 1
        if lat2 <= y2 * block + 1e-9:
            y2 -= 1
        pad = self.AUTO_BLOCK_PAD
        area = (max(-180.0, x1 * block - pad),
                max(-90.0, y1 * block - pad),
                min(180.0, (x2 + 1) * block + pad),
                min(90.0, (y2 + 1) * block + pad))
        path = self.prepare_region(ds, area, f"auto_{x1}_{y1}")
        return [str(path)] if path else None

    def _remote_sources(self, ds, bbox, allow_download=True):
        """Источники в хранилище: без локальной копии, только сеть."""
        spec = DATASETS[ds]
        source = spec["source"]
        if source == "local":
            return self.local_sources(ds, bbox)
        if source == "sonny":
            return self._sonny_sources(ds, bbox, allow_download)
        if source == "skadi":
            paths = []
            for lat, lon in _tiles_for(bbox):
                path = self._ensure_srtm(ds, lat, lon, allow_download)
                if path:
                    paths.append(path)
            return paths
        # COG-наборы читаются по сети (STAC, подпись, /vsicurl): ниже z9
        # обходимся только прогретыми окнами, которые проверены выше.
        if not allow_download:
            return []
        if source == "cop30":
            paths = []
            for lat, lon in _tiles_for(bbox):
                url = _aws_url(source, lat, lon)
                path = "/vsicurl/" + url
                paths.append(path)
                # У GLO-30 гранула названа по юго-западному углу, как у SRTM.
                self.remember_source(
                    path, f"{DATASETS[ds]['short']} · {_tile_label(lat, lon)}")
            return paths
        return self._pc_sources(ds, bbox)

    # ------------------------------------------------------------------
    # Sonny DTM (Google Drive)
    # ------------------------------------------------------------------

    def sonny_catalogue(self, allow_refresh=True):
        """Список тайлов Sonny: ``{набор: {файл: id}}``.

        Кэш общий с плагином. Если его ещё нет (первый запуск на машине),
        список загружается с Google Drive один раз на процесс.  При
        ``allow_refresh=False`` используется только локальный кэш.
        """
        if sonny is None:
            return {}
        with self._sonny_lock:
            if self._sonny_catalogue is None:
                catalogue = sonny.load_catalogue()
                if not any(key in sonny.DATASET_BY_KEY for key in catalogue):
                    if not allow_refresh:
                        # Пустой результат не запоминаем: при возврате на
                        # рабочий зум список должен загрузиться.
                        return catalogue
                    with _net_read(
                            f"{DATASETS['sonny']['short']} · список файлов"):
                        catalogue = sonny.refresh_catalogue()
                self._sonny_catalogue = catalogue
            return self._sonny_catalogue

    def _sonny_sources(self, ds, bbox, allow_download=True):
        """Локальные файлы Sonny для области: скачивает недостающие тайлы."""
        if sonny is None:
            if not self._sonny_warned:
                self._sonny_warned = True
                _log("Sonny DTM: нет доступа к ядру плагина (qgis.PyQt)")
            return []
        catalogue = self.sonny_catalogue(allow_refresh=allow_download)
        if not catalogue:
            return []
        # Для каждого тайла берётся лучший набор: 0,5″ важнее 1″.
        groups = {}
        for cell, dataset_key in sonny.assign_datasets(
                catalogue, set(_tiles_for(bbox))).items():
            groups.setdefault(dataset_key, []).append(cell)
        paths = []
        for dataset_key in sorted(
                groups, key=lambda key: sonny.DATASET_BY_KEY[key].rank):
            dataset = sonny.DATASET_BY_KEY[dataset_key]
            folder = self.cache_dir / "sonny" / dataset_key
            for cell, file_id in sonny.tile_sources(
                    catalogue, dataset_key, groups[dataset_key]):
                path = self._ensure_sonny_tile(
                    ds, dataset, file_id, cell, folder, allow_download)
                if path is not None:
                    paths.append(str(path))
        return paths

    def _ensure_sonny_tile(self, ds, dataset, file_id, cell, folder,
                           allow_download=True):
        """Скачивает один тайл Sonny и возвращает путь для GDAL."""
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{sonny.tile_name(*cell)}.hgt"
        with self._tile_lock(f"sonny:{dataset.key}:{target.name}"):
            path = target
            if not target.exists() or target.stat().st_size == 0:
                if not allow_download:
                    return None
                key = f"sonny:{dataset.key}:{target.name}"
                label = f"{DATASETS[ds]['short']} · {_tile_label(*cell)}"
                _download_enter(key, label)
                try:
                    path = sonny.download_tile(dataset, file_id, folder)
                except Exception as exc:  # noqa: BLE001 - нет тайла в Drive
                    _log(f"Sonny DTM {target.name}: не удалось скачать: "
                         f"{exc}")
                    return None
                finally:
                    _download_leave(key)
            if (path.suffix.lower() == ".hgt" and
                    dataset.samples not in sonny.SRTMHGT_SAMPLES):
                # Драйвер SRTMHGT не читает 7201 точку (0,5″): нужен VRT.
                path = sonny.hgt_vrt(path, dataset.step, dataset.samples)
        return path

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
        digest = hashlib.md5(
            ",".join(f"{v:.8f}" for v in bbox).encode("ascii")).hexdigest()[:8]
        # Параллельные потоки не должны качать одну и ту же область дважды.
        with self._named_lock(self._prep_locks, f"{ds}:{digest}"):
            found = self._prepared_exact(ds, bbox)
            if found:
                return found
            return self._prepare_region_locked(ds, bbox, name, digest)

    def _prepare_region_locked(self, ds, bbox, name, digest):
        # Часть тайлов COG может отсутствовать (GLO-30 публикует не всё):
        # берём только те, что реально открываются, иначе warp падает
        # целиком и блок качается заново на каждом тайле.
        sources = []
        for path in self._remote_sources(ds, bbox):
            if self.is_missing(path):
                continue
            dataset, _lock = self.open_source(path)
            if dataset is not None:
                sources.append(path)
        if not sources:
            return None
        folder = self.prepared_dir(ds)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{name}_{digest}.tif"
        # Временное имя с PID и потоком: два процесса (например, рабочий
        # сервер и второй экземпляр на том же кэше) готовили бы один и тот
        # же файл и мешали друг другу — один переименовывал tmp, второй его
        # уже не находил и ронял весь запрос.
        temporary = folder / (f"{name}_{digest}.{os.getpid()}."
                              f"{threading.get_ident()}.tmp.tif")
        options = gdal.WarpOptions(
            format="GTiff",
            dstSRS="EPSG:4326",
            outputBounds=tuple(bbox),
            resampleAlg="bilinear",
            srcNodata=DATASETS[ds].get("src_nodata"),
            # Если у набора нет nodata, пустые места (неопубликованный тайл)
            # иначе записались бы нулём и выглядели как настоящая высота.
            # Все COG-наборы здесь Int16, поэтому минимум Int16 безопасен.
            dstNodata=(DATASETS[ds].get("src_nodata")
                       if DATASETS[ds].get("src_nodata") is not None
                       else -32768),
            multithread=True,
            creationOptions=["COMPRESS=DEFLATE", "TILED=YES",
                             "BIGTIFF=IF_SAFER"],
        )
        center_lat = (bbox[1] + bbox[3]) / 2.0
        center_lon = (bbox[0] + bbox[2]) / 2.0
        key = f"region:{ds}:{digest}"
        # Подпись — по настоящим гранулам источников: центр области мог
        # попасть в соседний тайл и сбить с толку.
        labels = []
        for path in sources:
            label = self.source_label(path)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            labels = [f"{DATASETS[ds]['short']} · "
                      f"{_tile_label(center_lat, center_lon)}"]
        label = labels[0] if len(labels) == 1 else f"{labels[0]} +{len(labels) - 1}"
        _download_enter(key, label)
        try:
            result = gdal.Warp(str(temporary), sources, options=options)
        except Exception as exc:
            _log(f"{ds}: не удалось сохранить область: {exc}")
            temporary.unlink(missing_ok=True)
            return None
        finally:
            _download_leave(key)
        if result is None:
            temporary.unlink(missing_ok=True)
            return None
        result.FlushCache()
        result = None
        try:
            temporary.replace(target)
        except FileNotFoundError:
            # Временный файл исчез: его убрал другой процесс или уборка.
            # Если цель уже готова — она и нужна, иначе регион считаем
            # неготовым и набор скачается при следующем запросе.
            if target.exists():
                self._register_prepared(ds, target.name, bbox)
                return target
            _log(f"{ds}: временный файл области пропал, регион не готов")
            return None
        except OSError as exc:
            _log(f"{ds}: не удалось сохранить область: {exc}")
            temporary.unlink(missing_ok=True)
            return None
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
            # Автоблоки появляются при панорамировании и могут копиться
            # гигабайтами: держим последние, старые удаляем.
            auto = [item for item in items
                    if str(item.get("file", "")).startswith("auto_")]
            if len(auto) > self.AUTO_BLOCK_LIMIT:
                def _stamp(item):
                    try:
                        return (folder / item.get("file", "")).stat().st_mtime
                    except OSError:
                        return 0.0
                auto.sort(key=_stamp)
                drop = auto[:len(auto) - self.AUTO_BLOCK_LIMIT]
                dropped = {item.get("file") for item in drop}
                for item in drop:
                    (folder / item.get("file", "")).unlink(missing_ok=True)
                items = [item for item in items
                         if item.get("file") not in dropped]
            temporary = index_path.with_name("index.tmp")
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            temporary.replace(index_path)
            with self._lock:
                self._prepared_cache.pop(ds, None)

    def open_at(self, ds, lon, lat, allow_download=True):
        """Открывает набор так, чтобы в него попала точка.

        Возвращает ``(dataset, lock)``: общий датасет из кэша нельзя читать
        из двух потоков сразу — libtiff на этом падает. Для свежеоткрытых
        датасетов блокировка не нужна, поэтому ``lock`` равен ``None``.
        ``allow_download=False`` ограничивает выбор уже закэшированными
        данными.
        """
        prepared = self._prepared_for(ds, (lon, lat, lon, lat))
        if prepared:
            dataset, lock = self.open_source(prepared[0])
            if dataset is not None:
                return dataset, lock
        spec = DATASETS[ds]
        source = spec["source"]
        if source == "local":
            for path in self.local_sources(ds, (lon, lat, lon, lat)):
                dataset, lock = self.open_source(path)
                if dataset is not None:
                    return dataset, lock
            return None, None
        if source == "sonny":
            # Точечный bbox: иначе _tiles_for захватит ещё три соседних
            # тайла на границе градуса и скачает их зря.
            paths = self._sonny_sources(
                ds, (lon, lat, lon, lat), allow_download)
            for path in paths:
                dataset, lock = self.open_source(path)
                if dataset is not None:
                    return dataset, lock
            return None, None
        if source == "skadi":
            path = self._ensure_srtm(ds, int(math.floor(lat)),
                                     int(math.floor(lon)), allow_download)
            return (gdal.Open(path), None) if path else (None, None)
        if source == "cop30":
            if not allow_download:
                return None, None
            url = _aws_url(source, int(math.floor(lat)), int(math.floor(lon)))
            return gdal.Open("/vsicurl/" + url), None
        if not allow_download:
            return None, None
        sources = self._pc_sources(ds, (math.floor(lon), math.floor(lat),
                                         math.floor(lon) + 1,
                                         math.floor(lat) + 1),
                                   center_only=True)
        for path in sources:
            dataset, lock = self.open_source(path)
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
                return dataset, lock
            dataset = None
        return None, None

    def _ensure_srtm(self, ds, lat, lon, allow_download=True):
        """Скачивает .hgt.gz и превращает в GeoTIFF (gzip по HTTP не читаем)."""
        target = self.local_path(ds, lat, lon)
        if target.exists() and target.stat().st_size > 0:
            return target
        if not allow_download:
            return None
        with self._tile_lock(f"{ds}:{target.name}"):
            if target.exists() and target.stat().st_size > 0:
                return target
            raw = (self.temp_dir or target.parent) / (
                f"{target.name}.{os.getpid()}.raw")
            key = f"srtm:{target.name}"
            label = f"{DATASETS[ds]['short']} · {_tile_label(lat, lon)}"
            _download_enter(key, label)
            try:
                if not _download(_aws_url("skadi", lat, lon), raw):
                    raw.unlink(missing_ok=True)
                    return None
            finally:
                _download_leave(key)
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
        """Ссылки на файлы наборов Planetary Computer с SAS-токеном."""
        collection = {"nasadem": "nasadem", "alos30": "alos-dem",
                      "3dep": "3dep-seamless"}[DATASETS[ds]["source"]]
        suffix = DATASETS[ds].get("id_suffix")
        query_bbox = bbox or (0, 0, 1, 1)
        # Гранулы наборов — целые градусы (N37E031 и т.п.). Ключ обязан
        # покрывать весь запрошенный блок: по начальной ячейке нельзя — окно
        # в той же ячейке, но с большим охватом получало короткий список
        # первого запроса и готовилось с дырами там, где гранул не хватало.
        cell = (math.floor(query_bbox[0]), math.floor(query_bbox[1]),
                math.ceil(query_bbox[2]), math.ceil(query_bbox[3]))
        key = self.cache_dir / (f"pc_{collection}_{cell[0]}_{cell[1]}_"
                                f"{cell[2]}_{cell[3]}.json")
        # Один запрос STAC на ячейку: параллельные потоки ждут его, а не
        # дублируют и не пишут файл одновременно.
        with self._named_lock(self._pc_locks, str(key)):
            if key.exists():
                try:
                    items = json.loads(key.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    items = []
            else:
                items = []
            if not items:
                url = ("https://planetarycomputer.microsoft.com/api/stac/v1/"
                       f"search?collections={collection}"
                       f"&bbox={','.join(str(v) for v in query_bbox)}&limit=100")
                # Поиск файлов — тоже сеть, и он небыстрый: без отметки
                # пауза выглядит зависанием сервера.
                with _net_read(f"{DATASETS[ds]['short']} · список файлов"):
                    items = _http_json(url).get("features", [])
                temporary = key.with_name(key.name + ".tmp")
                temporary.write_text(json.dumps(items), encoding="utf-8")
                temporary.replace(key)
        if suffix:
            preferred = [item for item in items
                         if item.get("id", "").endswith(suffix)]
            if preferred:
                items = preferred
        if bbox:
            # Лишние источники (соседние градусы) только замедляют warp.
            items = [item for item in items
                     if not item.get("bbox") or
                     _bbox_intersects(item["bbox"], bbox)]
        if center_only:
            center = (query_bbox[0] + 0.5, query_bbox[1] + 0.5)
            items = [item for item in items
                     if not item.get("bbox") or
                     (item["bbox"][0] <= center[0] <= item["bbox"][2] and
                      item["bbox"][1] <= center[1] <= item["bbox"][3])]
        try:
            with _net_read(f"{DATASETS[ds]['short']} · токен доступа"):
                token = _pc_token(collection)
        except Exception as exc:
            # Сервис токена недоступен — подписываем по файлам, как раньше.
            _log(f"{collection}: нет токена ({exc})")
            token = None
        sources = []
        for item in items:
            href = None
            for asset in item.get("assets", {}).values():
                candidate = asset.get("href", "")
                if candidate.lower().endswith((".tif", ".tiff")):
                    href = candidate
                    break
            if not href:
                continue
            if token:
                separator = "&" if "?" in href else "?"
                source = "/vsicurl/" + href + separator + token
            else:
                source = "/vsicurl/" + self._signed(href)
            sources.append(source)
            # Подпись гранулы: по ней видно, какой кусок набора качается.
            cell = item.get("bbox")
            if cell:
                self.remember_source(
                    source, f"{DATASETS[ds]['short']} · {_cell_label(cell)}")
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

def _allow_download(query):
    """Разрешены ли новые загрузки по зуму из запроса.

    Страница передаёт текущий зум; ниже z9 сервер использует только уже
    закэшированные данные.  Без параметра поведение прежнее.
    """
    values = query.get("z")
    if not values:
        return True
    try:
        return float(values[0]) >= RELIEF_MIN_ZOOM
    except (TypeError, ValueError):
        return True


def _tiles_for(bbox):
    """Тайлы 1°×1°, пересекающие область."""
    minx, miny, maxx, maxy = bbox
    tiles = []
    for lat in range(int(math.floor(miny)), int(math.floor(maxy)) + 1):
        for lon in range(int(math.floor(minx)), int(math.floor(maxx)) + 1):
            lon = max(-180, min(179, lon))
            tiles.append((lat, lon))
    return tiles


def _sources_for(store, ds, bbox, allow_download=True):
    """Источники DEM для области.

    Список, а не общий VRT: запросы идут параллельно, а общий файл VRT
    затирался бы соседним запросом — и на карте повторялся бы один и тот же
    участок рельефа.
    """
    return store.sources(ds, bbox, allow_download)


def _dem_grid(store, ds, bbox, pixels=None, allow_download=True):
    """DEM набора на регулярной сетке EPSG:4326.

    Возвращает (массив высот, geotransform). Размер сетки по умолчанию —
    по нативному разрешению источника, чтобы не терять детали.
    """
    minx, miny, maxx, maxy = bbox
    sources = _sources_for(store, ds, bbox, allow_download)
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
        # Float32: в Int16 NaN округляется до 0 и профиль показывает
        # ложные нулевые высоты на краях.
        outputType=gdal.GDT_Float32,
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


def _load_dem3857(cache_file):
    if not cache_file.exists():
        return None
    try:
        return np.load(cache_file)
    except (OSError, ValueError):
        cache_file.unlink(missing_ok=True)
        return None


def _dem3857(store, ds, z, x, y):
    """DEM набора в Web Mercator, тайл 256×256 float32.

    Промежуточный растр кэшируется на диске: повторный запрос того же
    тайла (другой режим, разность высот, соседний режим) источник не
    читает заново.
    """
    cache_file = (store.cache_dir / "dem3857" / f"v{CACHE_VERSION}" / ds /
                  str(z) / str(x) / f"{y}.npy")
    cached = _load_dem3857(cache_file)
    if cached is not None:
        return cached
    # Страница просит тайл дважды (повтор после смены зума, вторая вкладка).
    # Без блокировки оба потока тянули одни и те же данные из сети.
    with store._named_lock(store._render_locks, f"{ds}/{z}/{x}/{y}"):
        cached = _load_dem3857(cache_file)
        if cached is not None:
            return cached
        return _render_dem3857(store, ds, z, x, y, cache_file)


def _render_dem3857(store, ds, z, x, y, cache_file):
    lon1, lat1, lon2, lat2 = tile_bounds(z, x, y)
    # Ниже z9 новые исходники не качаются: только то, что уже лежит в кэше.
    allow_download = z >= RELIEF_MIN_ZOOM
    paths = [path for path in store.tile_sources(
        ds, (lon1, lat1, lon2, lat2), allow_download)
        if not store.is_missing(path)]
    if not paths:
        return None
    # Сетевые источники читаются по HTTP: без отметки в статусе видно
    # только «ожидание», а что именно качается — нет.
    remote = [path for path in paths if str(path).startswith("/vsicurl/")]
    labels = []
    for path in remote:
        label = store.source_label(path)
        if label and label not in labels:
            labels.append(label)
    if labels:
        label = labels[0] if len(labels) == 1 else f"{labels[0]} +{len(labels) - 1}"
        with _net_read(label):
            return _warp_dem3857(store, ds, paths, lon1, lat1, lon2, lat2,
                                 cache_file)
    return _warp_dem3857(store, ds, paths, lon1, lat1, lon2, lat2, cache_file)


def _warp_dem3857(store, ds, paths, lon1, lat1, lon2, lat2, cache_file):
    # Проверяем, что источники открываются (часть тайлов COG не опубликована),
    # но в warp передаём пути: GDAL откроет их сам, у каждого потока будет
    # свой дескриптор, и тайлы перестанут ждать друг друга на общей блокировке.
    usable = []
    for path in paths:
        dataset, _lock = store.open_source(path)
        if dataset is not None:
            usable.append(path)
    if not usable:
        return None
    bounds = _mercator_bounds(lon1, lat1, lon2, lat2)
    src_nodata = DATASETS[ds].get("src_nodata")
    options = gdal.WarpOptions(
        format="MEM",
        dstSRS="EPSG:3857",
        outputBounds=bounds,
        width=TILE_SIZE,
        height=TILE_SIZE,
        resampleAlg="bilinear",
        # Явно Float32: в целочисленный растр NaN не помещается, и GDAL
        # округляет его до 0 — по краям тайла появляется ложная высота.
        outputType=gdal.GDT_Float32,
        srcNodata=src_nodata,
        dstNodata=float("nan"),
        # Для 256×256 многопоточная перепроекция только вредит: потоки
        # конкурируют за кэш GDAL и тайл считается в разы дольше.
        multithread=False,
    )
    with RENDER_SLOTS:
        warped = gdal.Warp("", usable, options=options)
    if warped is None:
        return None
    array = warped.GetRasterBand(1).ReadAsArray().astype("float32")
    warped = None
    if src_nodata is not None:
        array[array == src_nodata] = np.nan
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Атомарно: сервер и прогрев могут писать один и тот же тайл.
        temporary = cache_file.with_name(
            f"{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temporary, "wb") as handle:
            np.save(handle, array)
        temporary.replace(cache_file)
    except OSError:
        pass
    return array


def _hillshade(values, zoom):
    """Отмывка по растру Web Mercator, как у ``gdaldem hillshade``.

    Пиксель EPSG:3857 — это ``res`` метров проекции по обеим осям, поэтому
    шаг одинаковый. Направление склона берётся от вектора вниз: раньше
    аспект считался повёрнутым на 180°, и склоны, обращённые к солнцу,
    уходили в чёрный — чёрными становились до 60 % тайла.
    """
    if values is None:
        return None
    valid = ~np.isnan(values)
    filled = np.where(valid, values, 0.0).astype("float64")
    res = 156543.03392 / (2 ** zoom)
    step = res
    dzdx = np.empty_like(filled)
    dzdx[:, 1:-1] = (filled[:, 2:] - filled[:, :-2]) / 2.0
    dzdx[:, 0] = filled[:, 1] - filled[:, 0]
    dzdx[:, -1] = filled[:, -1] - filled[:, -2]
    dzdy = np.empty_like(filled)
    dzdy[1:-1, :] = (filled[:-2, :] - filled[2:, :]) / 2.0
    dzdy[0, :] = filled[0, :] - filled[1, :]
    dzdy[-1, :] = filled[-1, :] - filled[-2, :]
    dzdx /= step
    dzdy /= step
    slope = np.arctan(np.hypot(dzdx, dzdy))
    # Азимут склона: atan2(восток, север) от вектора вниз.
    aspect = np.arctan2(-dzdx, -dzdy)
    zenith = math.radians(90.0 - 45.0)
    azimuth = math.radians(315.0)
    shade = 255.0 * (math.cos(zenith) * np.cos(slope) +
                     math.sin(zenith) * np.sin(slope) * np.cos(azimuth - aspect))
    # Небольшая подсветка теней: совсем чёрные провалы на карте читаются
    # как дыры, особенно на крутых северных склонах.
    shade = 255.0 * (0.10 + 0.90 * np.clip(shade / 255.0, 0.0, 1.0))
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
    """Маска горизонталей: полоса вокруг уровня шириной в половину шага.

    Расстояние до ближайшего уровня сравнивается с перепадом высоты на
    пиксель, поэтому линия выходит толщиной около пикселя и не расползается
    на крутом склоне. Всё считается массивами numpy: прежний поячеечный
    marching squares с отрисовкой отрезками на Python занимал до 0,7 с на
    тайл и держал GIL, из-за чего тайлы не считались параллельно.
    """
    if values is None:
        return None
    valid = np.isfinite(values)
    if not valid.any():
        return None
    data = np.where(valid, values, np.nan).astype("float64")
    lo = float(np.nanmin(data))
    hi = float(np.nanmax(data))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo or step <= 0:
        return np.zeros(values.shape, dtype="uint8")
    first = math.ceil(lo / step) * step
    phase = (data - first) / step
    distance = np.abs(phase - np.round(phase)) * step
    # Перепад высоты на пиксель — он же масштаб толщины линии.
    grad_y, grad_x = np.gradient(data)
    slope = np.hypot(grad_x, grad_y)
    # Плоское место (море, плато) горизонталей не даёт: там перепад нулевой
    # и полоса выродилась бы в сплошную заливку.
    return ((distance <= 0.5 * slope) & valid & (slope > 0)).astype("uint8") * 255


def _elevation_colors(array, sea_level=0.0, limits=None):
    """Гипсометрическая раскраска.

    limits — общие пределы (мин, макс) для всех тайлов вида: тогда картинка
    читается как одна шкала. Без них пределы берутся из самого тайла, и
    соседние тайлы окрашены по-разному.
    """
    if array is None:
        return None
    valid = ~np.isnan(array)
    value = np.where(valid, array, sea_level).astype("float64")
    if limits:
        lo, hi = float(limits[0]), float(limits[1])
    else:
        lo = min(sea_level, float(value.min()))
        hi = float(value.max())
    if hi <= lo:
        hi = lo + 1.0
    # Значения вне пределов прижимаем к краям шкалы, а не оставляем чёрными.
    norm = np.clip((value - lo) / (hi - lo), 0.0, 1.0)
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


def _limits_for_tiles(store, datasets, z, x0, x1, y0, y1):
    """Пределы высот по набору тайлов: общая шкала для всех наборов.

    Считаются по тем же массивам DEM, из которых рисуются тайлы, поэтому
    шкала совпадает с картинкой. Наборов несколько — пределы общие: иначе
    две половины шторки сравнивались бы по разным шкалам.
    """
    count = 2 ** z
    tasks = []
    for ds in datasets:
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                if 0 <= y < count:
                    wrapped = ((x % count) + count) % count
                    tasks.append((ds, z, wrapped, y))
    if not tasks:
        return None

    def one(task):
        ds, tile_z, x, y = task
        try:
            values = _dem3857(store, ds, tile_z, x, y)
        except Exception:
            # Один негодный тайл не должен рушить всю шкалу: остальные
            # считаются, пределы получаются чуть шире.
            return None
        if values is None:
            return None
        valid = ~np.isnan(values)
        if not valid.any():
            return None
        return float(values[valid].min()), float(values[valid].max())

    lo = hi = None
    with ThreadPoolExecutor(max_workers=6) as pool:
        for part in pool.map(one, tasks):
            if part is None:
                continue
            if lo is None or part[0] < lo:
                lo = part[0]
            if hi is None or part[1] > hi:
                hi = part[1]
    if lo is None:
        return None
    return _round_limits(lo, hi)


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


def _round_limits(lo, hi):
    """Округлить пределы до круглых чисел.

    Шкала не должна меняться от каждого сдвига мыши: округление до шага
    примерно в тридцатую часть размаха убирает дрожание цвета.
    """
    span = hi - lo
    step = _nice_step(span / 30.0) if span > 0 else 1.0
    low = math.floor(lo / step) * step
    high = math.ceil(hi / step) * step
    if high <= low:
        high = low + step
    return low, high


def _limits_from_query(query):
    """Пределы шкалы высот из запроса тайла."""
    values = query.get("lo", [None])[0], query.get("hi", [None])[0]
    if values[0] is None or values[1] is None:
        return None
    try:
        low = float(values[0])
        high = float(values[1])
    except (TypeError, ValueError):
        return None
    if high <= low:
        return None
    return low, high


def _log(message):
    """Сообщение в консоль сервера с датой и временем.

    С flush: при перенаправлении в файл stdout буферизуется, и строки
    появлялись бы пачками раз в несколько килобайт.
    """
    print(f"[dem-compare {datetime.now():%Y-%m-%d %H:%M:%S}] {message}",
          flush=True)


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

def tile_cache_path(tile_cache, ds, mode, z, x, y, limits=None):
    """Путь готового тайла в кэше (общий для сервера и прогрева).

    limits — общие пределы шкалы высот. Окраска тайла от них зависит, поэтому
    тайлы с разными пределами лежат в разных каталогах: иначе отдался бы
    PNG, нарисованный по чужой шкале.
    """
    base = Path(tile_cache) / f"v{TILE_CACHE_VERSION}" / ds / mode
    if limits and mode in SCALED_MODES:
        base = base / f"r{limits[0]:.1f}_{limits[1]:.1f}"
    elif mode in SCALED_MODES:
        # Пределы не передали — своя шкала на каждый тайл.
        base = base / "auto"
    return base / str(z) / str(x) / f"{y}.png"


def write_bytes_atomic(path, data):
    """Пишет файл через временное имя: читатель не увидит половину."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def render_tile(store, ds, z, x, y, mode="hillshade", limits=None):
    values = _dem3857(store, ds, z, x, y)
    if values is None:
        return None, None
    valid = ~np.isnan(values)
    lat_center = (tile_bounds(z, x, y)[1] + tile_bounds(z, x, y)[3]) / 2
    if mode == "elevation":
        rgb = _elevation_colors(values, limits=limits)
    elif mode == "contours":
        rgb = _elevation_colors(values, limits=limits)
        shade = _hillshade(values, z)
        if shade is not None:
            factor = (shade.astype("float64") / 255.0 * 0.6 + 0.4)[:, :, None]
            rgb = np.clip(rgb.astype("float64") * factor, 0, 255).astype("uint8")
        if limits:
            # Шаг по размаху видимой области: горизонтали идут ровно по
            # шкале цветов, а не по разрешению экрана.
            step = _nice_step(max(1.0, (limits[1] - limits[0]) / 10.0))
        else:
            step = _nice_step(156543.03392 / (2 ** z) * 8)
        mask = _contour_mask(values, step)
        if mask is not None:
            rgb[mask > 0] = (58, 38, 28)
    else:
        shade = _hillshade(values, z)
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


def sample_value(store, ds, lon, lat, allow_download=True):
    """Высота в точке из исходного тайла набора."""
    dataset, lock = store.open_at(ds, lon, lat, allow_download)
    if dataset is None:
        return {"elevation": None, "native_x_m": None, "native_y_m": None}
    # Общий датасет из кэша читают и другие запросы: libtiff не переносит
    # одновременный ReadAsArray и роняет процесс.
    if lock is not None:
        lock.acquire()
    try:
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
    finally:
        if lock is not None:
            lock.release()
    dataset = None
    return {"elevation": value, "native_x_m": native_x, "native_y_m": native_y}


def sample_profile(store, ds, lon1, lat1, lon2, lat2, count=200,
                   allow_download=True):
    """Профиль высот по отрезку: выборка из DEM сетки вдоль линии."""
    pad = 0.02
    bbox = (min(lon1, lon2) - pad, min(lat1, lat2) - pad,
            max(lon1, lon2) + pad, max(lat1, lat2) + pad)
    array, gt = _dem_grid(store, ds, bbox, allow_download=allow_download)
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
# Поля объекта могут идти в любом порядке: сначала группа, потом имя.
_POINT_RE = re.compile(
    r'\{[^{}]*?name:\s*"(?P<name>[^"]+)".*?'
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


def precache(store, tile_cache, points, modes=("hillshade", "elevation",
                                               "contours"),
             view=(1400, 900), jobs=8, margin=2, render_tiles=False):
    """Скачивает исходники для всех точек страницы.

    Тайлы по умолчанию не рисуются: когда исходные данные уже локально,
    один тайл считается за десятые доли секунды, а скачивание области —
    это секунды и десятки мегабайт. Поэтому прогреваем только сеть, а PNG
    страница получает по требованию. ``render_tiles=True`` дополнительно
    рисует тайлы (для проверки, что всё считается без сети).
    """
    width, height = view
    jobs = max(1, int(jobs))
    started = time.monotonic()
    print_lock = threading.Lock()

    # Параллелизм даёт пул потоков; вложенные потоки GDAL только мешают.
    if jobs > 1:
        gdal.SetConfigOption("GDAL_NUM_THREADS", "1")

    source_tasks = []
    for index, point in enumerate(points, 1):
        for key, spec in DATASETS.items():
            if spec["source"] == "local":
                # Локальный каталог уже на диске — прогревать нечего.
                continue
            low, high = zoom_range(point["zoom"], point["lat"],
                                   spec["resolution_m"])
            for z in range(low, high + 1):
                bbox = view_bbox(point["lon"], point["lat"], z, width, height,
                                 margin=margin)
                source_tasks.append((index, point, key, spec, z, bbox))

    print(f"Прогрев кэша: точек {len(points)}, наборов {len(DATASETS)}, "
          f"окно {width}×{height}, потоков {jobs}")
    if render_tiles:
        print(f"Режимы тайлов: {', '.join(modes)}")
    else:
        print("Тайлы не рисуются: они считаются на лету из локальных данных")
    print(f"\nИсходные данные: {len(source_tasks)} областей")

    def prepare(task):
        index, point, key, spec, z, bbox = task
        moment = time.monotonic()
        error = ""
        try:
            if spec["source"] in ("skadi", "sonny"):
                # Целые градусные тайлы уже локальные: окно не готовим.
                ok = bool(store.sources(key, bbox))
            else:
                ok = store.prepare_region(
                    key, bbox, f"p{index:02d}_z{z}") is not None
        except Exception as exc:
            ok = False
            error = str(exc)
        return task, ok, time.monotonic() - moment, error

    ready = set()
    total_sources = len(source_tasks)
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(prepare, task) for task in source_tasks]
        for future in as_completed(futures):
            task, ok, seconds, error = future.result()
            index, point, key, spec, z, _bbox = task
            done += 1
            if ok:
                ready.add((index, key, z))
                status = f"готово за {seconds:.1f} с"
            elif error:
                status = f"ошибка: {error}"
            else:
                status = f"нет данных за {seconds:.1f} с"
            with print_lock:
                print(f"  [{done:>3}/{total_sources}] {point['name']} · "
                      f"{spec['short']} z{z} — {status}", flush=True)

    if render_tiles:
        # Тайлы рисуем только там, где исходники действительно есть.
        tile_tasks = []
        for index, key, z in sorted(ready):
            point = points[index - 1]
            for x, y in visible_tiles(point["lon"], point["lat"], z, width,
                                      height, margin=margin):
                for mode in modes:
                    tile_tasks.append((index, point, key, z, x, y, mode))

        total_tiles = len(tile_tasks)
        print(f"\nТайлы: {total_tiles}")
        rendered = cached = empty = failed = 0
        done_tiles = 0
        tile_started = time.monotonic()

        def render(task):
            index, point, key, z, x, y, mode = task
            path = tile_cache_path(tile_cache, key, mode, z, x, y)
            if path.exists():
                return task, "cached", ""
            try:
                data, _native = render_tile(store, key, z, x, y, mode)
            except Exception as exc:
                return task, "error", str(exc)
            if data is None or len(data) < 256:
                return task, "empty", ""
            write_bytes_atomic(path, data)
            return task, "rendered", ""

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(render, task) for task in tile_tasks]
            for future in as_completed(futures):
                task, status, error = future.result()
                done_tiles += 1
                if status == "cached":
                    cached += 1
                elif status == "empty":
                    empty += 1
                elif status == "error":
                    failed += 1
                    with print_lock:
                        print(f"\n  ошибка {task[3]}/{task[4]}/{task[5]}: "
                              f"{error}")
                else:
                    rendered += 1
                if done_tiles % 50 == 0 or done_tiles == total_tiles:
                    elapsed = time.monotonic() - tile_started
                    rate = done_tiles / elapsed if elapsed else 0.0
                    with print_lock:
                        print(f"\r  {done_tiles}/{total_tiles} "
                              f"({rate:.1f} тайла/с, отрисовано {rendered}, "
                              f"из кэша {cached})", end="", flush=True)
        if total_tiles:
            print()

        elapsed = time.monotonic() - started
        print(f"\nГотово за {elapsed:.0f} с: отрисовано {rendered}, "
              f"из кэша {cached}, без данных {empty}, ошибок {failed}")
    else:
        elapsed = time.monotonic() - started
        print(f"\nГотово за {elapsed:.0f} с: исходники скачаны, "
              f"тайлы не рисовались")
    print(f"Кэш: {store.cache_dir}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    store = None
    tile_cache = None
    osm_cache = None

    def log_message(self, fmt, *args):
        message = fmt % args
        if " 404 " in message or " 500 " in message:
            _log(message)

    def _send(self, code, body, content_type, extra=None):
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for key, value in extra.items():
            self.send_header(key, value)
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Браузер закрыл вкладку или отменил тайл, который уже не нужен.
            self.close_connection = True

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
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": str(exc)}, code=500)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

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
            self._tile(parts[1], parts[2], parts[3], parts[4], mode,
                       _limits_from_query(query))
            return
        if head == "tiles":
            self._tiles(query)
            return
        if head == "diff" and len(parts) == 6:
            self._diff(parts[1], parts[2], parts[3], parts[4], parts[5])
            return
        if head == "osm" and len(parts) == 4:
            self._osm(parts[1], parts[2], parts[3])
            return
        if head == "favicon.ico":
            # Иконка уже в странице (data-URI); старые вкладки могут
            # попросить её отдельно — отвечаем без тела и без записи в лог.
            self._send(204, b"", "image/x-icon")
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
        if action == "local":
            # Каталог с рельефом задаётся из UI; сервер только индексирует.
            folder = query.get("dir", [""])[0].strip()
            key = query.get("key", [LOCAL_KEY])[0]
            if not folder:
                self._json({"error": "нужен параметр dir"}, code=400)
                return
            try:
                info = register_local_source(self.store, folder, key)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, code=400)
                return
            self._json(info)
            return
        if action == "browse":
            self._browse(query)
            return
        if action == "limits":
            self._limits(query)
            return
        if action == "osm-resolve":
            # Короткие ссылки osm.org/go/… редиректят на #map=…; браузер
            # не может прочитать адрес после кросс-доменного редиректа.
            url = query.get("url", [""])[0].strip()
            host = (urllib.parse.urlparse(url).hostname or "").lower()
            if host not in ("openstreetmap.org", "www.openstreetmap.org",
                            "osm.org", "www.osm.org"):
                self._json({"error": "не ссылка OpenStreetMap"}, code=400)
                return
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=20) as response:
                    final = response.geturl()
            except Exception as exc:
                self._json({"error": str(exc)}, code=502)
                return
            self._json({"url": final})
            return
        ds = query.get("ds", [""])[0]
        if ds not in DATASETS:
            self._json({"error": "unknown dataset"}, code=404)
            return
        if action == "value":
            lon = float(query.get("lon", ["0"])[0])
            lat = float(query.get("lat", ["0"])[0])
            result = sample_value(
                self.store, ds, lon, lat, _allow_download(query))
            result.update({"dataset": ds, "datum": DATASETS[ds]["datum"],
                           "kind": DATASETS[ds]["kind"]})
            self._json(result)
            return
        if action == "info":
            lon = float(query.get("lon", ["0"])[0])
            lat = float(query.get("lat", ["0"])[0])
            result = sample_value(
                self.store, ds, lon, lat, _allow_download(query))
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
                        "samples": sample_profile(
                            self.store, ds, lon1, lat1, lon2, lat2, count,
                            allow_download=_allow_download(query))})
            return
        self._json({"error": "unknown api"}, code=404)

    def _limits(self, query):
        """Пределы высот по видимым тайлам: общая шкала для всех наборов."""
        names = [n for n in query.get("ds", [""])[0].split(",") if n]
        names = [n for n in names if n in DATASETS]
        if not names:
            self._json({"error": "unknown dataset"}, code=404)
            return
        try:
            z = int(query.get("z", ["0"])[0])
            x0 = int(query.get("x0", ["0"])[0])
            x1 = int(query.get("x1", ["0"])[0])
            y0 = int(query.get("y0", ["0"])[0])
            y1 = int(query.get("y1", ["0"])[0])
        except ValueError:
            self._json({"error": "плохие параметры"}, code=400)
            return
        limits = _limits_for_tiles(self.store, names, z, x0, x1, y0, y1)
        if limits is None:
            self._json({"error": "нет данных"}, code=404)
            return
        self._json({"lo": limits[0], "hi": limits[1]})

    def _browse(self, query):
        """Подкаталоги для выбора локального набора прямо в интерфейсе."""
        raw = query.get("dir", [""])[0].strip()
        path = Path(raw).expanduser() if raw else Path.home()
        try:
            path = path.resolve()
        except OSError:
            path = Path.home()
        if not path.is_dir():
            path = path.parent if path.parent.is_dir() else Path.home()
        try:
            children = sorted(path.iterdir(),
                              key=lambda item: item.name.lower())
        except OSError as exc:
            self._json({"error": str(exc)}, code=400)
            return
        dirs = [{"name": child.name, "raster": _has_rasters(child)}
                for child in children
                if child.is_dir() and not child.name.startswith(".")]
        parent = str(path.parent) if path.parent != path else None
        self._json({"path": str(path), "parent": parent, "dirs": dirs})

    def _tile_payload(self, ds, z, x, y, mode, limits=None):
        """PNG тайла: общий путь для одиночного запроса и для пачки."""
        # У режимов с высотой цвет зависит от шкалы, поэтому тайлы с разными
        # пределами лежат в разных каталогах.
        cache_file = tile_cache_path(self.tile_cache, ds, mode, z, x, y, limits)
        key = f"{ds}/{mode}/{z}/{x}/{y}"
        if cache_file.exists():
            _status_leave(key, rendered=False)
            return "cached", cache_file.read_bytes(), 0.0, None
        _status_enter(key, ds, z, x, y)
        started = time.monotonic()
        try:
            result = render_tile(self.store, ds, z, x, y, mode, limits)
        finally:
            _status_leave(key, rendered=True)
        seconds = time.monotonic() - started
        if result is None or result[0] is None or len(result[0]) < 256:
            # Пустой результат — источник не отдался: не кэшируем.
            return "empty", b"", seconds, None
        data, native = result
        write_bytes_atomic(cache_file, data)
        _log(f"tile {ds}/{mode} {z}/{x}/{y} {seconds:.2f}s очередь {_queue_size()}")
        return "rendered", data, seconds, native

    def _tile(self, ds, z, x, y, mode, limits=None):
        if ds not in DATASETS:
            self._json({"error": "unknown dataset"}, code=404)
            return
        z, x, y = int(z), int(x), int(y.split(".")[0])
        status, data, seconds, native = self._tile_payload(
            ds, z, x, y, mode, limits)
        if status == "empty":
            self._send(404, b"", "text/plain",
                       {"X-Tile-Status": "empty",
                        "X-Tile-Seconds": f"{seconds:.2f}"})
            return
        headers = {"X-Tile-Status": status,
                   "X-Tile-Seconds": f"{seconds:.2f}"}
        if native:
            headers["X-Native-X"] = f"{native[0]:.2f}"
            headers["X-Native-Y"] = f"{native[1]:.2f}"
        self._send(200, data, "image/png", headers)

    def _tiles(self, query):
        """Пачка тайлов одним потоковым ответом.

        Браузер держит не больше шести соединений на хост, поэтому при
        поштучных запросах простаивала половина слотов рендера. Здесь тайлы
        рисуются в общем пуле и уходят кадрами по мере готовности, не ожидая
        самый медленный. Формат кадра — строка ``DEMFRAME x y status seconds
        length`` и сразу за ней ``length`` байт PNG; порядок кадров не важен,
        клиент раскладывает их по координатам.
        """
        ds = query.get("ds", [""])[0]
        if ds not in DATASETS:
            self._json({"error": "unknown dataset"}, code=404)
            return
        try:
            z = int(query.get("z", ["0"])[0])
        except ValueError:
            self._json({"error": "bad zoom"}, code=400)
            return
        mode = query.get("mode", ["hillshade"])[0]
        limits = _limits_from_query(query)
        tasks = []
        for raw in query.get("t", []):
            try:
                tx, ty = raw.split(",")
                tasks.append((int(tx), int(ty)))
            except ValueError:
                continue
        tasks = tasks[:TILE_BATCH_MAX]
        if not tasks:
            self._json({"error": "нужны тайлы"}, code=400)
            return
        # Content-Length неизвестен: ответ закрывается концом данных.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        futures = {
            BATCH_POOL.submit(self._tile_payload, ds, z, x, y, mode, limits):
                (x, y) for x, y in tasks
        }
        try:
            for future in as_completed(futures):
                x, y = futures[future]
                try:
                    status, data, seconds, _native = future.result()
                except Exception:
                    status, data, seconds = "empty", b"", 0.0
                header = (f"DEMFRAME {x} {y} {status} {seconds:.3f} "
                          f"{len(data)}\n").encode("ascii")
                self.wfile.write(header)
                if data:
                    self.wfile.write(data)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            for future in futures:
                future.cancel()

    def _osm(self, z, x, y):
        """Тайл подложки OpenStreetMap, с локальным кэшем."""
        z, x, y = int(z), int(x), int(y.split(".")[0])
        count = 2 ** z
        if z < 0 or z > OSM_MAX_ZOOM or not (0 <= x < count and 0 <= y < count):
            self._send(404, b"", "text/plain")
            return
        cache_file = self.osm_cache / str(z) / str(x) / f"{y}.png"
        headers = {"X-Tile-Status": "cached", "Cache-Control": "max-age=86400"}
        if cache_file.exists():
            self._send(200, cache_file.read_bytes(), "image/png", headers)
            return
        # Один и тот же тайл могут запросить соседние вкладки/потоки.
        with _osm_lock(f"{z}/{x}/{y}"):
            if cache_file.exists():
                self._send(200, cache_file.read_bytes(), "image/png", headers)
                return
            url = OSM_TILE_URL.format(z=z, x=x, y=y)
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT,
                              "Referer": "http://127.0.0.1/"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read()
            except urllib.error.HTTPError as exc:
                self._send(404 if exc.code in (403, 404) else 502,
                           b"", "text/plain")
                return
            except Exception:
                self._send(502, b"", "text/plain")
                return
            if not data:
                self._send(404, b"", "text/plain")
                return
            write_bytes_atomic(cache_file, data)
            self._send(200, data, "image/png",
                       {"X-Tile-Status": "osm",
                        "Cache-Control": "max-age=86400"})

    def _diff(self, ds_a, ds_b, z, x, y):
        z, x, y = int(z), int(x), int(y.split(".")[0])
        cache_file = (self.tile_cache / f"v{TILE_CACHE_VERSION}" / "diff" /
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
        write_bytes_atomic(cache_file, data)
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


def configure_temp_dir(path):
    """Направляет временные файлы GDAL и Python в указанный каталог."""
    folder = Path(path).expanduser() if path else Path(tempfile.gettempdir())
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(folder)
    os.environ["TEMP"] = str(folder)
    os.environ["TMP"] = str(folder)
    tempfile.tempdir = str(folder)
    # GDAL кладёт сюда промежуточные VRT и spill больших warp.
    gdal.SetConfigOption("CPL_TMPDIR", str(folder))
    return folder


def main():
    parser = argparse.ArgumentParser(description="Сервер сравнения DEM")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE))
    parser.add_argument("--temp-dir", default=None,
                        help="каталог временных файлов GDAL и промежуточных "
                             "загрузок (по умолчанию системный)")
    parser.add_argument("--local-dem", default=None,
                        help="каталог с собственным рельефом (GeoTIFF, HGT, "
                             "VRT и т. п.); добавляется набором «Свой рельеф»")
    parser.add_argument("--local-dem2", default=None,
                        help="второй каталог с рельефом — «Свой рельеф 2»")
    parser.add_argument("--precache", action="store_true",
                        help="скачать исходники и отрисовать тайлы для точек "
                             "страницы, затем выйти")
    parser.add_argument("--precache-tiles", action="store_true",
                        help="дополнительно отрисовать тайлы (по умолчанию "
                             "прогрев только скачивает исходники)")
    parser.add_argument("--precache-modes", default="hillshade,elevation,contours",
                        help="режимы тайлов при --precache-tiles, через запятую")
    parser.add_argument("--precache-view", default="1400x900",
                        help="размер окна страницы для прогрева (ШxВ)")
    parser.add_argument("--precache-margin", type=int, default=2,
                        help="запас в тайлах вокруг окна при прогреве")
    parser.add_argument("--jobs", type=int, default=8,
                        help="сколько областей и тайлов готовить параллельно")
    args = parser.parse_args()

    cache = Path(args.cache).expanduser()
    temp_dir = configure_temp_dir(args.temp_dir)
    store = SourceStore(cache, temp_dir=temp_dir)
    for key, folder in ((LOCAL_KEY, args.local_dem),
                        (LOCAL_KEY_2, args.local_dem2)):
        if not folder:
            continue
        try:
            local = register_local_source(store, folder, key)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Локальный рельеф: {exc}")
        print(f"{LOCAL_TITLES[key]}: {local['files']} файлов, "
              f"{local['directory']}")

    if args.precache:
        modes = [mode.strip() for mode in args.precache_modes.split(",")
                 if mode.strip()]
        unknown = [mode for mode in modes
                   if mode not in ("hillshade", "elevation", "contours")]
        if unknown:
            raise SystemExit(f"Неизвестный режим: {', '.join(unknown)}")
        precache(store, cache / "tiles", load_points(),
                 modes=modes or ("hillshade",),
                 view=parse_view(args.precache_view),
                 jobs=args.jobs, margin=max(0, args.precache_margin),
                 render_tiles=args.precache_tiles)
        return

    Handler.store = store
    Handler.tile_cache = cache / "tiles"
    Handler.tile_cache.mkdir(parents=True, exist_ok=True)
    Handler.osm_cache = cache / "osm"
    Handler.osm_cache.mkdir(parents=True, exist_ok=True)

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
    print(f"Временные файлы: {temp_dir}")
    print("Остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == "__main__":
    main()
