#!/usr/bin/env python3
"""Проверка положения пиков OSM (natural=peak) по рельефу выбранного источника.

Ошибочный пик — узел natural=peak, поставленный не на вершине, а в
долине, на склоне или в седловине.  В QGIS он виден как точка с
подписью из ``ele_calc`` (значение DEM в точке), но подпись оказывается
в долине, где вершины нет.

Для каждого пика скрипт считает:

* ``dem`` — высоту DEM в точке (билинейная интерполяция);
* ``max3``/``min3`` — максимум и минимум DEM в окне 3x3 (~90 м):
  вершина должна быть локальным максимумом, долина — локальным
  минимумом (яма);
* ``drop300``/``drop600``/``drop1500`` — насколько ближайшая более
  высокая точка рельефа выше самого пика (провал от вершины);
* расхождение тега OSM ``ele`` с DEM.

Итоги: CSV со всеми метриками, GeoJSON с подозрительными пиками и
текстовый отчёт со сводкой, кластерами и примерами.

Пример:

    python3 tools/check_peak_placement.py \\
        --pbf ~/qgis_projects/qgisxtopo-cache/geofabrik/georgia.osm.pbf \\
        --out /tmp/peaks

Источник рельефа выбирается ``--dem-source``: поддерживаются все наборы
плагина (``srtm``, ``cop30``, ``nasadem``, ``alos``, ``3dep``, ``manual``).
Растровые наборы скачиваются через ``core.terrain.download_cog_dem`` и
приводятся к сетке 1″ в EPSG:4326, поэтому метрики сравнимы с SRTM.
Для ``manual`` или своего файла укажите ``--dem-file``.
"""

import argparse
import csv
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests  # noqa: E402,F401  (регистрирует пакет qgis_xtopo)

from qgis_xtopo.core.bbox import BBox  # noqa: E402
from qgis_xtopo.core import dem_sources  # noqa: E402

# Размер тайла SRTM (SRTM1): 3601x3601 ячеек по 1/3600 градуса.
TILE_PIXELS = 3601
PIXEL_DEGREES = 1.0 / 3600.0

SRTM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{tile}.hgt.gz"

DEFAULT_DEM_CACHE = Path.home() / "qgis_projects" / "qgisxtopo-cache" / "srtm"

# Теги, по которым объект считается названным.
NAME_TAGS = ("name", "alt_name", "official_name", "short_name", "old_name",
             "loc_name", "int_name", "nat_name", "reg_name", "sorting_name")

# Допуск шума SRTM: вершиной считаем точку, не ниже максимума в окне.
SUMMIT_TOLERANCE = 8.0
SUMMIT_TOLERANCE_WIDE = 15.0
# Допуск, с которым точка считается стоящей на локальном максимуме.
LOCAL_MAX_TOLERANCE = 3.0

OPL_TAIL = re.compile(r"\s+x(-?[\d.]+)\s+y(-?[\d.]+)\s*$")
OPL_HEAD = re.compile(r"\s+v(\d+)\s+d\S*\s+c(\d+)\s+t(\S+)\s+i(\d+)\s+u(.*)$")


def fill_nan(array):
    """Заполняет пропуски DEM ближайшими известными значениями."""
    from scipy import ndimage

    mask = np.isnan(array)
    if not mask.any():
        return array
    indices = ndimage.distance_transform_edt(
        mask, return_distances=False, return_indices=True
    )
    return array[tuple(indices)]


def tile_name(lat, lon):
    """Имя тайла SRTM для точки."""
    north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{north}{east}"


OPL_ESCAPE = re.compile(r"%([0-9a-fA-F]{2})")


def decode_opl_value(value):
    """Раскодирует значение тега из OPL.

    osmium пишет пробел как ``%20%`` (с лишним процентом), остальные
    спецсимволы — как ``%XX``.
    """
    value = value.replace("%20%", " ").replace("%20", " ")
    return OPL_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), value)


def parse_opl(line):
    """Разбор строки OPL формата osmium в словарь.

    Строка выглядит так::

        n1234 v2 dV c5678 t2015-05-07T07:36:09Z i42 u vasya Tnatural=peak x45.1 y42.5

    Значения тегов в OPL закодированы (пробелы — ``%20``), поэтому
    строку можно разбирать по пробелам.  Имя пользователя может
    содержать пробелы, но стоит после ``i<uid>`` и до ``T``, поэтому
    теги вырезаются с конца.
    """
    tail = OPL_TAIL.search(line)
    if tail is None:
        return None
    lon = float(tail.group(1))
    lat = float(tail.group(2))
    body = line[:tail.start()]
    split = body.rfind(" T")
    if split < 0:
        return None
    tags = {}
    for item in body[split + 2:].split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            tags[decode_opl_value(key)] = decode_opl_value(value)
    head = OPL_HEAD.search(body[:split])
    result = {"lon": lon, "lat": lat, "tags": tags}
    if head:
        result["version"] = int(head.group(1))
        result["changeset"] = int(head.group(2))
        result["timestamp"] = head.group(3)
        result["uid"] = int(head.group(4))
        result["user"] = head.group(5)
    return result


@dataclass
class Peak:
    osm_id: str
    lon: float
    lat: float
    tags: dict
    source_file: str = ""
    version: int = 0
    changeset: int = 0
    timestamp: str = ""
    uid: int = 0
    user: str = ""

    @property
    def name(self):
        for key in NAME_TAGS:
            value = self.tags.get(key)
            if value:
                return value
        for key, value in self.tags.items():
            if key.startswith("name:") and value:
                return value
        return ""

    @property
    def ele(self):
        for key in ("ele", "height"):
            value = self.tags.get(key)
            if value:
                try:
                    return float(value)
                except ValueError:
                    return None
        return None

    @property
    def category(self):
        """Категория для отчёта.

        named — есть название, не проверяем;
        ele_only — без названия, но с ele (проверяем отдельно);
        anonymous — без названия и без ele (основная группа).
        """
        if self.name:
            return "named"
        if self.ele is not None:
            return "ele_only"
        return "anonymous"

    @property
    def anonymous(self):
        return self.category == "anonymous"

    @property
    def natural(self):
        return self.tags.get("natural", "")


def find_osmium(explicit=None):
    """Путь к бинарю osmium: аргумент, переменная окружения, vendor."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("OSMIUM")
    if env:
        candidates.append(Path(env))
    root = Path(__file__).resolve().parent.parent / "vendor" / "osmium"
    for platform in ("linux-x86_64", "win-x86_64"):
        candidates.append(root / platform / "osmium")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("osmium")
    if found:
        return found
    raise SystemExit(
        "osmium не найден: укажите путь через --osmium или переменную OSMIUM"
    )


def read_peaks(pbf_files, natural_values, osmium_bin):
    """Читает узлы natural=* из PBF через osmium tags-filter."""
    expression = "n/natural=" + ",".join(natural_values)
    peaks = []
    seen = set()

    def extract(path):
        command = [
            osmium_bin, "tags-filter", "-f", "opl", str(path), expression,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                f"osmium tags-filter не отработал для {path}:\n{result.stderr}"
            )
        return result.stdout

    paths = []
    for path in pbf_files:
        path = Path(path).expanduser()
        if not path.exists():
            print(f"WARNING: файл не найден, пропущен: {path}", file=sys.stderr)
            continue
        paths.append(path)
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(paths)))) as pool:
        outputs = list(pool.map(extract, paths))

    for path, output in zip(paths, outputs):
        for line in output.splitlines():
            record = parse_opl(line)
            if record is None:
                continue
            osm_id = line.split(" ", 1)[0][1:]
            # Выгрузки Geofabrik перекрываются на границах, поэтому один
            # и тот же узел встречается в нескольких файлах.
            if osm_id in seen:
                continue
            seen.add(osm_id)
            peaks.append(Peak(
                osm_id=osm_id,
                lon=record["lon"],
                lat=record["lat"],
                tags=record["tags"],
                source_file=path.name,
                version=record.get("version", 0),
                changeset=record.get("changeset", 0),
                timestamp=record.get("timestamp", ""),
                uid=record.get("uid", 0),
                user=record.get("user", ""),
            ))
    return peaks


def read_waterways(pbf_files, osmium_bin):
    """Читает линии водных объектов (stream, river) для подложки карты.

    osmium tags-filter оставляет в выводе узлы, на которые ссылаются
    подходящие линии, поэтому геометрию можно собрать из OPL.
    """
    lines = []
    for path in pbf_files:
        path = Path(path).expanduser()
        if not path.exists():
            continue
        command = [
            osmium_bin, "tags-filter", "-f", "opl", str(path),
            "w/waterway=stream,river,canal",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            continue
        nodes = {}
        ways = []
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                record = parse_opl(line)
                if record is not None:
                    nodes[line.split(" ", 1)[0][1:]] = (record["lon"], record["lat"])
            elif line.startswith("w"):
                # В OPL список узлов ways — один токен вида "Nn1,n2,n3".
                for token in line.split(" "):
                    if token.startswith("N") and "," in token:
                        refs = [
                            item[1:] for item in token[1:].split(",")
                            if item.startswith("n")
                        ]
                        ways.append(refs)
                        break
        for refs in ways:
            coords = [nodes[ref] for ref in refs if ref in nodes]
            if len(coords) > 1:
                lines.append(coords)
    return lines


class DemTiles:
    """Доступ к тайлам SRTM из кэша (скачивание при необходимости)."""

    def __init__(self, cache_dir=DEFAULT_DEM_CACHE, download=True):
        self.cache_dir = Path(cache_dir).expanduser()
        self.download = download
        self._tiles = {}
        self._order = []

    def path(self, name):
        return self.cache_dir / name[:3] / f"{name}.hgt"

    def ensure(self, name):
        """Скачивает тайл, если его нет в кэше."""
        target = self.path(name)
        if target.exists():
            return target
        if not self.download:
            return None
        url = SRTM_URL.format(dir=name[:3], tile=name)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".hgt.gz")
        print(f"Загрузка тайла рельефа {name}", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                with temporary.open("wb") as handle:
                    shutil.copyfileobj(response, handle)
            with gzip.open(temporary, "rb") as source:
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        except Exception as exc:  # noqa: BLE001 - тайл не критичен для анализа
            print(f"WARNING: не удалось скачать {name}: {exc}", file=sys.stderr)
            return None
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def load(self, name):
        """Матрица высот тайла (float32, пропуски — nan)."""
        if name in self._tiles:
            return self._tiles[name]
        path = self.ensure(name)
        if path is None:
            return None
        array = np.fromfile(str(path), dtype=">i2")
        if array.size != TILE_PIXELS * TILE_PIXELS:
            print(f"WARNING: неожиданный размер тайла {name}", file=sys.stderr)
            return None
        array = array.reshape(TILE_PIXELS, TILE_PIXELS).astype("float32")
        array[array <= -1000] = np.nan
        self._tiles[name] = array
        self._order.append(name)
        while len(self._order) > 6:
            self._tiles.pop(self._order.pop(0), None)
        return array

    def _tile_window(self, name, lon_min, lon_max, lat_min, lat_max):
        """Часть тайла в заданных границах (None — тайла нет)."""
        array = self.load(name)
        if array is None:
            return None
        lat0 = int(name[1:3])
        if name[0] == "S":
            lat0 = -lat0
        lon0 = int(name[4:7])
        if name[3] == "W":
            lon0 = -lon0
        # Пиксель тайла: столбец растёт на восток, строка — на юг.
        col_min = int(round((lon_min - lon0) / PIXEL_DEGREES))
        col_max = int(round((lon_max - lon0) / PIXEL_DEGREES))
        row_min = int(round((lat0 + 1 - lat_max) / PIXEL_DEGREES))
        row_max = int(round((lat0 + 1 - lat_min) / PIXEL_DEGREES))
        col_min = max(0, min(TILE_PIXELS - 1, col_min))
        col_max = max(0, min(TILE_PIXELS - 1, col_max))
        row_min = max(0, min(TILE_PIXELS - 1, row_min))
        row_max = max(0, min(TILE_PIXELS - 1, row_max))
        if col_max < col_min or row_max < row_min:
            return None
        return array[row_min:row_max + 1, col_min:col_max + 1]

    def window(self, lon, lat, half_px_x, half_px_y):
        """Прямоугольник DEM вокруг точки.

        Возвращает массив, в котором строка 0 — южный край, столбец 0 —
        западный, либо None, если данных нет.
        """
        lon_min = lon - half_px_x * PIXEL_DEGREES
        lon_max = lon + half_px_x * PIXEL_DEGREES
        lat_min = lat - half_px_y * PIXEL_DEGREES
        lat_max = lat + half_px_y * PIXEL_DEGREES
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
                if overlap_lon_max < overlap_lon_min or overlap_lat_max < overlap_lat_min:
                    continue
                piece = self._tile_window(
                    name,
                    overlap_lon_min,
                    overlap_lon_max,
                    overlap_lat_min,
                    overlap_lat_max,
                )
                if piece is None:
                    continue
                col_offset = int(round((overlap_lon_min - lon_min) / PIXEL_DEGREES))
                row_offset = int(round((overlap_lat_min - lat_min) / PIXEL_DEGREES))
                # Строки тайла идут с севера на юг, а в окне строка 0 —
                # южный край, поэтому кусок переворачивается.
                out[
                    row_offset:row_offset + piece.shape[0],
                    col_offset:col_offset + piece.shape[1],
                ] = piece[::-1]
        return out


class DemRaster:
    """Доступ к окну DEM из локального GeoTIFF (EPSG:4326, шаг 1″).

    Растровые наборы плагина (Copernicus, NASADEM, ALOS, 3DEP) и ручной
    каталог приводятся к той же сетке, что и тайлы SRTM, поэтому метрики
    считаются одинаково для любого источника.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._dataset = None

    def _open(self):
        if self._dataset is None:
            from osgeo import gdal

            gdal.UseExceptions()
            dataset = gdal.Open(str(self.path))
            if dataset is None:
                raise RuntimeError(f"Не удалось открыть рельеф: {self.path}")
            self._dataset = dataset
        return self._dataset

    def window(self, lon, lat, half_px_x, half_px_y):
        """Прямоугольник DEM вокруг точки (строка 0 — южный край)."""
        dataset = self._open()
        transform = dataset.GetGeoTransform()
        step_x = abs(transform[1])
        step_y = abs(transform[5])
        width = 2 * half_px_x + 1
        height = 2 * half_px_y + 1
        lon_min = lon - half_px_x * PIXEL_DEGREES
        lat_min = lat - half_px_y * PIXEL_DEGREES
        lat_max = lat_min + (height - 1) * PIXEL_DEGREES
        out = np.full((height, width), np.nan, dtype="float32")

        x0 = int(round((lon_min - transform[0]) / step_x))
        y0 = int(round((transform[3] - lat_max) / step_y))
        x1 = x0 + width
        y1 = y0 + height
        sx0, sy0 = max(0, x0), max(0, y0)
        sx1 = min(dataset.RasterXSize, x1)
        sy1 = min(dataset.RasterYSize, y1)
        if sx1 <= sx0 or sy1 <= sy0:
            return out

        band = dataset.GetRasterBand(1)
        piece = band.ReadAsArray(sx0, sy0, sx1 - sx0, sy1 - sy0).astype("float32")
        nodata = band.GetNoDataValue()
        if nodata is not None:
            piece[piece == nodata] = np.nan
        # Строки растра идут с севера на юг, а в окне — с юга на север.
        piece = piece[::-1]
        out_x = sx0 - x0
        out_y = (height - 1) - (sy1 - 1 - y0)
        out[out_y:out_y + piece.shape[0], out_x:out_x + piece.shape[1]] = piece
        return out


def peaks_bbox(peaks, padding=0.05):
    """Охват всех пиков с запасом под окна рельефа."""
    lons = [peak.lon for peak in peaks]
    lats = [peak.lat for peak in peaks]
    return BBox(
        max(-180.0, min(lons) - padding),
        max(-90.0, min(lats) - padding),
        min(180.0, max(lons) + padding),
        min(90.0, max(lats) + padding),
    )


def prepare_dem_raster(source, bbox, cache_dir, workdir, dem_file=None):
    """Готовит растр DEM в EPSG:4326 с шагом 1″ и возвращает путь.

    Для растровых наборов окно скачивает ``core.terrain.download_cog_dem``
    (с кэшем плагина), ручной каталог передаётся через ``dem_file``.
    """
    from osgeo import gdal

    from qgis_xtopo.core import terrain

    gdal.UseExceptions()
    if dem_file is not None:
        source_path = Path(dem_file).expanduser()
        if not source_path.exists():
            raise RuntimeError(f"Нет файла рельефа: {source_path}")
    else:
        source_path = terrain.download_cog_dem(
            source, bbox, workdir, cache_dir=cache_dir,
            progress=lambda message: print(message, flush=True),
        )

    target = Path(workdir) / "dem_1s.tif"
    width = max(1, int(round((bbox.lon_max - bbox.lon_min) / PIXEL_DEGREES)))
    height = max(1, int(round((bbox.lat_max - bbox.lat_min) / PIXEL_DEGREES)))
    options = gdal.WarpOptions(
        format="GTiff",
        dstSRS="EPSG:4326",
        outputBounds=(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max),
        width=width,
        height=height,
        resampleAlg="bilinear",
        outputType=gdal.GDT_Int16,
        dstNodata=-32768,
        creationOptions=["COMPRESS=LZW", "BIGTIFF=YES"],
    )
    result = gdal.Warp(str(target), str(source_path), options=options)
    if result is None:
        raise RuntimeError(f"Не удалось подготовить рельеф {source}")
    result = None
    return target


def make_dem(spec):
    """Объект доступа к рельефу по описанию источника."""
    if spec.get("path"):
        return DemRaster(spec["path"])
    return DemTiles(spec["cache_dir"], download=spec["download"])


def half_pixels(metres, lat):
    """Полуразмер окна DEM в пикселях для радиуса в метрах."""
    metres_per_degree_lat = 111320.0
    metres_per_degree_lon = max(20000.0, metres_per_degree_lat * math.cos(math.radians(lat)))
    half_y = metres / metres_per_degree_lat / PIXEL_DEGREES
    half_x = metres / metres_per_degree_lon / PIXEL_DEGREES
    return max(1, int(round(half_x))), max(1, int(round(half_y)))


@dataclass
class Metrics:
    dem: float = float("nan")
    max3: float = float("nan")
    min3: float = float("nan")
    max5: float = float("nan")
    drop: dict = field(default_factory=dict)
    summit_distance: float = float("nan")
    summit_drop: float = float("nan")
    cross300: float = float("nan")
    cross600: float = float("nan")
    ridge_distance: float = float("nan")
    divide_distance: float = float("nan")
    sharp_distance: float = float("nan")
    sharp_100_distance: float = float("nan")
    sharp_300_distance: float = float("nan")
    ridge_strict_distance: float = float("nan")
    divide_strict_distance: float = float("nan")
    sharp_300_strict_distance: float = float("nan")
    sharp_150_strict_distance: float = float("nan")
    sharp_450_distance: float = float("nan")
    near_max_distance: float = float("nan")
    near_max_drop: float = float("nan")
    ring_higher: float = float("nan")
    ring_700_higher: float = float("nan")
    ring_1200_higher: float = float("nan")
    stream_distance: float = float("nan")
    ring_150_lower: float = float("nan")
    ring_600_lower: float = float("nan")
    ring_200_lower: float = float("nan")
    ring_300_lower: float = float("nan")
    ring_450_lower: float = float("nan")
    cross900: float = float("nan")
    curvature: float = float("nan")
    relief: float = float("nan")
    neighbour_nearest: float = float("nan")
    ring_600_relative: float = float("nan")
    ele_diff: float = float("nan")
    ele_below: float = float("nan")

    @property
    def on_summit(self):
        """Точка стоит на локальном максимуме DEM в пределах ~90 м."""
        if math.isnan(self.dem) or math.isnan(self.max3):
            return False
        return self.max3 - self.dem <= SUMMIT_TOLERANCE

    @property
    def on_summit_wide(self):
        """Точка стоит на вершине с точностью до ~150 м."""
        if math.isnan(self.dem) or math.isnan(self.max5):
            return False
        return self.max5 - self.dem <= SUMMIT_TOLERANCE_WIDE

    @property
    def in_pit(self):
        """Точка стоит в локальном минимуме DEM — яма или дно долины."""
        if math.isnan(self.dem) or math.isnan(self.min3) or math.isnan(self.max3):
            return False
        return self.dem - self.min3 <= 3.0 and self.max3 - self.dem > 3.0

    def drop_at(self, radius):
        value = self.drop.get(radius)
        return float("nan") if value is None else value


def find_summit(window, lat, dem_value, tolerance=50.0):
    """Ближайший локальный максимум DEM.

    Локальный максимум — ячейка не ниже всех восьми соседей.  Он не
    обязан быть выдающейся вершиной: на хребте это может быть бугор
    рядом с точкой.  Допускаются максимумы ниже точки на tolerance
    метров — на хребте соседний бугор часто чуть ниже.

    Возвращает (расстояние в метрах, превышение над точкой, строка,
    столбец) либо None.
    """
    maximum = ndimage.maximum_filter(window, size=3, mode="nearest")
    is_peak = window >= maximum - 1e-6
    rows, columns = window.shape
    row_center = rows // 2
    column_center = columns // 2
    rows_index, columns_index = np.nonzero(is_peak)
    best = None
    metres_lat = 111320.0
    metres_lon = metres_lat * math.cos(math.radians(lat))
    for row, column in zip(rows_index, columns_index):
        height = float(window[row, column])
        if height < dem_value - tolerance:
            continue
        dy = (row - row_center) * metres_lat / 3600.0
        dx = (column - column_center) * metres_lon / 3600.0
        distance = math.hypot(dx, dy)
        if best is None or distance < best[0]:
            best = (distance, height - dem_value, int(row), int(column))
    return best


def find_near_max(smoothed, lat, height, tolerance=60.0):
    """Ближайший локальный максимум сглаженной поверхности.

    Отличается от find_summit тем, что работает по сглаженному DEM:
    одиночные шумовые выбросы SRTM не создают лишних «вершин».
    Возвращает (расстояние, превышение над точкой) либо None.
    """
    maximum = ndimage.maximum_filter(smoothed, size=3, mode="nearest")
    is_peak = smoothed >= maximum - 1e-6
    rows, columns = smoothed.shape
    row_center = rows // 2
    column_center = columns // 2
    rows_index, columns_index = np.nonzero(is_peak)
    best = None
    metres_lat = 111320.0
    metres_lon = metres_lat * math.cos(math.radians(lat))
    for row, column in zip(rows_index, columns_index):
        value = float(smoothed[row, column])
        if value < height - tolerance:
            continue
        dy = (row - row_center) * metres_lat / 3600.0
        dx = (column - column_center) * metres_lon / 3600.0
        distance = math.hypot(dx, dy)
        if best is None or distance < best[0]:
            best = (distance, value - height)
    return best


def cross_profile(smoothed, lat, radius):
    """Поперечный профиль склона для каждой ячейки окна.

    Для каждой ячейки берётся линия падения склона и две точки на
    расстоянии radius поперёк неё.  Возвращает превышение более высокой
    из них над ячейкой: отрицательное значение — гребень (обе стороны
    ниже), положительное — долина или склон.
    """
    gradient_y, gradient_x = np.gradient(smoothed)
    norm = np.hypot(gradient_x, gradient_y) + 1e-9
    across_x = gradient_y / norm
    across_y = -gradient_x / norm
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    row_grid, column_grid = np.indices(smoothed.shape)
    sides = []
    for sign in (1, -1):
        sample_rows = row_grid + sign * across_y * radius / metres_lat
        sample_columns = column_grid + sign * across_x * radius / metres_lon
        sides.append(ndimage.map_coordinates(
            smoothed, [sample_rows, sample_columns], order=1, mode="nearest"
        ))
    return np.maximum(sides[0] - smoothed, sides[1] - smoothed)


# Радиусы (в метрах), на которых ищется выпуклость гребня.  Мелкие
# ловят острые гребни, крупные — широкие и пологие.
RIDGE_SCALES = (60.0, 90.0, 150.0, 240.0, 360.0, 500.0)


def ridge_mask(window, lat, ridge_cross=-10.0, radius=None):
    """Ячейки-гребни: поперёк склона рельеф ниже с обеих сторон.

    Выпуклость проверяется на нескольких масштабах: ячейка считается
    гребнем, если хотя бы на одном из них обе стороны поперёк склона
    ниже.  Иначе широкие и пологие гребни оставались бы незамеченными.
    """
    smoothed = ndimage.gaussian_filter(window, 1.0)
    scales = (radius,) if radius else RIDGE_SCALES
    cross = None
    for scale in scales:
        current = cross_profile(smoothed, lat, scale)
        cross = current if cross is None else np.minimum(cross, current)
    mask = cross <= ridge_cross
    # Закрываем разрывы от шума, чтобы гребень был линией, а не точками.
    return ndimage.binary_closing(mask, structure=np.ones((3, 3)))


# Соседние ячейки в порядке обхода для стока по D8.
FLOW_NEIGHBOURS = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1),
    (0, 1), (1, -1), (1, 0), (1, 1),
)


def flow_accumulation(surface, lat):
    """Площадь водосбора каждой ячейки по D8, в числе ячеек.

    Ячейка, в которую не притекает вода, имеет площадь 1.  Настоящий
    хребет — это водораздел: с него вода только стекает, поэтому
    площадь минимальна.  Склон ниже гребня собирает воду с части
    склона и имеет площадь заметно больше.
    """
    rows, columns = surface.shape
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    steepest = np.zeros(surface.shape, dtype=np.int8)
    slope = np.zeros(surface.shape)
    for index, (dy, dx) in enumerate(FLOW_NEIGHBOURS):
        distance = math.hypot(dy * metres_lat, dx * metres_lon)
        neighbour = np.full(surface.shape, np.nan)
        source_rows = slice(max(0, dy), rows + min(0, dy))
        source_columns = slice(max(0, dx), columns + min(0, dx))
        target_rows = slice(max(0, -dy), rows + min(0, -dy))
        target_columns = slice(max(0, -dx), columns + min(0, -dx))
        neighbour[target_rows, target_columns] = surface[
            source_rows, source_columns
        ]
        drop = np.nan_to_num((surface - neighbour) / distance, nan=-1e9)
        better = drop > slope
        slope = np.where(better, drop, slope)
        steepest = np.where(better, index, steepest)
    area = np.ones(surface.shape, dtype=np.float64)
    order = np.argsort(surface, axis=None)[::-1]
    flat_steepest = steepest.ravel()
    flat_area = area.ravel()
    for cell in order:
        dy, dx = FLOW_NEIGHBOURS[int(flat_steepest[cell])]
        row, column = divmod(int(cell), columns)
        target_row = row + dy
        target_column = column + dx
        if 0 <= target_row < rows and 0 <= target_column < columns:
            flat_area[target_row * columns + target_column] += flat_area[cell]
    return area


def divide_mask(window, lat, threshold=3, sigma=2.0):
    """Ячейки водоразделов: собирают не больше threshold ячеек.

    Поверхность сглаживается, иначе шум SRTM плодит ложные водоразделы
    на ровном месте.
    """
    surface = ndimage.gaussian_filter(window, sigma)
    return flow_accumulation(surface, lat) <= threshold


def mask_distance(mask, lat, fallback=float("nan")):
    """Расстояние от центра окна до ближайшей ячейки маски, м."""
    if not mask.any():
        return fallback
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    distances = ndimage.distance_transform_edt(
        ~mask, sampling=(metres_lat, metres_lon)
    )
    return float(distances[distances.shape[0] // 2, distances.shape[1] // 2])


def divide_distance(window, lat, threshold=3, sigma=2.0):
    """Расстояние до ближайшей ячейки водораздела, м."""
    return mask_distance(divide_mask(window, lat, threshold, sigma), lat)


def ring_higher_fraction(surface, lat, radius=200.0, samples=180):
    """Доля кольца вокруг точки, где рельеф выше неё.

    На вершине доля нулевая, на хребте выше только дуга вверх по
    гребню, на склоне — примерно половина кольца, в долине — почти всё.
    """
    rows, columns = surface.shape
    row_center, column_center = rows // 2, columns // 2
    height = float(surface[row_center, column_center])
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    angles = np.linspace(0, 2 * math.pi, samples, endpoint=False)
    rows_ring = row_center + np.sin(angles) * radius / metres_lat
    columns_ring = column_center + np.cos(angles) * radius / metres_lon
    values = ndimage.map_coordinates(
        surface, [rows_ring, columns_ring], order=1, mode="nearest"
    )
    return float(np.mean(values > height + 1.0))


def ring_lower_drop(surface, lat, radius=150.0, samples=180):
    """Насколько точка выше самой низкой точки кольца вокруг неё, м.

    Большое значение означает, что точка стоит на краю обрыва, а не в
    седловине.
    """
    rows, columns = surface.shape
    row_center, column_center = rows // 2, columns // 2
    height = float(surface[row_center, column_center])
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
    angles = np.linspace(0, 2 * math.pi, samples, endpoint=False)
    rows_ring = row_center + np.sin(angles) * radius / metres_lat
    columns_ring = column_center + np.cos(angles) * radius / metres_lon
    values = ndimage.map_coordinates(
        surface, [rows_ring, columns_ring], order=1, mode="nearest"
    )
    return float(height - np.min(values))


def measure(peak, dem, radii, thresholds):
    """Метрики положения пика по рельефу.

    Считается одно окно по радиусу 1500 м, из него берутся и локальные
    максимумы (3x3 и 5x5), и максимум в радиусе 600 м.  Максимумы
    считаются по слегка сглаженной поверхности: одиночные шумовые
    выбросы SRTM иначе завышают «провал от вершины».
    """
    metrics = Metrics()
    outer = max(radii) if radii else 1500
    half_x, half_y = half_pixels(outer, peak.lat)
    window = dem.window(peak.lon, peak.lat, half_x, half_y)
    if window is None or np.isnan(window).all():
        return metrics
    window = fill_nan(window)
    rows, columns = window.shape
    row_center = rows // 2
    column_center = columns // 2

    # Точка попадает в угол четырёх ячеек DEM, поэтому высота в точке —
    # среднее этих четырёх ячеек (билинейная интерполяция).
    corner = window[
        row_center - 1:row_center + 1, column_center - 1:column_center + 1
    ]
    metrics.dem = float(np.mean(corner))
    metrics.min3 = float(np.min(
        window[
            row_center - 1:row_center + 2, column_center - 1:column_center + 2
        ]
    ))
    smoothed = ndimage.gaussian_filter(window, sigma=1.0)
    metrics.max3 = float(np.max(smoothed[
        row_center - 1:row_center + 2, column_center - 1:column_center + 2
    ]))
    metrics.max5 = float(np.max(smoothed[
        max(0, row_center - 2):row_center + 3, max(0, column_center - 2):column_center + 3
    ]))

    for radius in radii:
        if radius >= outer:
            metrics.drop[radius] = float(np.max(smoothed) - metrics.dem)
            continue
        inner_x, inner_y = half_pixels(radius, peak.lat)
        metrics.drop[radius] = float(
            np.max(
                smoothed[
                    row_center - inner_y:row_center + inner_y + 1,
                    column_center - inner_x:column_center + inner_x + 1,
                ]
            )
            - metrics.dem
        )
    # Поперечный профиль склона: на хребте рельеф ниже по обе стороны от
    # линии падения, в долине — выше, на склоне — выше с одной стороны.
    # Считается для каждой ячейки окна, чтобы найти ближайший гребень.
    metrics.cross300 = float(
        cross_profile(smoothed, peak.lat, 300)[row_center, column_center]
    )
    metrics.cross600 = float(
        cross_profile(smoothed, peak.lat, 600)[row_center, column_center]
    )
    ridges = ridge_mask(
        window, peak.lat, thresholds["ridge_cross"],
        thresholds["ridge_radius"] or None,
    )
    metres_lat = 111320.0 / 3600.0
    metres_lon = 111320.0 * math.cos(math.radians(peak.lat)) / 3600.0
    if ridges.any():
        distances = ndimage.distance_transform_edt(
            ~ridges, sampling=(metres_lat, metres_lon)
        )
        metrics.ridge_distance = float(distances[row_center, column_center])
    else:
        metrics.ridge_distance = float(outer)
    # Тот же гребень, но с более строгим порогом: обе стороны ниже
    # сразу на 20 м.  Отсекает пологие выпуклости, которые гребнем
    # считать нельзя.
    strict_ridges = ridge_mask(window, peak.lat, -20.0, None)
    metrics.ridge_strict_distance = mask_distance(
        strict_ridges, peak.lat, float(outer)
    )

    surface = ndimage.gaussian_filter(window, thresholds["divide_sigma"])
    area = flow_accumulation(surface, peak.lat)
    divides = area <= thresholds["divide_acc"]
    metrics.divide_distance = mask_distance(divides, peak.lat, float(outer))
    # Строгий водораздел: в ячейку не собирается вовсе (площадь 1).
    metrics.divide_strict_distance = mask_distance(
        area <= 1, peak.lat, float(outer)
    )
    # Острый гребень: поперёк склона рельеф заметно ниже уже на 150 м.
    # Отдельно от широких гребней: пик должен стоять и у такого гребня.
    sharp = cross_profile(
        smoothed, peak.lat, thresholds["sharp_radius"]
    ) <= thresholds["sharp_cross"]
    metrics.sharp_distance = mask_distance(sharp, peak.lat, float(outer))
    # Тот же признак на других масштабах: острый перепад на 100 м и на
    # 300 м поперёк склона.
    for scale, name in ((100.0, "sharp_100_distance"),
                        (300.0, "sharp_300_distance")):
        profile = cross_profile(smoothed, peak.lat, scale)
        setattr(
            metrics, name,
            mask_distance(
                profile <= thresholds["sharp_cross"], peak.lat, float(outer)
            ),
        )
    # Крутой гребень: перепад 25 м на 300 м, перепад 25 м на 150 м и
    # гребень на 450 м.
    metrics.sharp_300_strict_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 300.0) <= -25.0,
        peak.lat, float(outer),
    )
    metrics.sharp_150_strict_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 150.0) <= -25.0,
        peak.lat, float(outer),
    )
    metrics.sharp_450_distance = mask_distance(
        cross_profile(smoothed, peak.lat, 450.0) <= -15.0,
        peak.lat, float(outer),
    )
    metrics.ring_higher = ring_higher_fraction(
        smoothed, peak.lat, thresholds["ring_radius"]
    )
    metrics.ring_700_higher = ring_higher_fraction(
        smoothed, peak.lat, thresholds["ring_700_radius"]
    )
    metrics.ring_1200_higher = ring_higher_fraction(smoothed, peak.lat, 1200.0)
    # Расстояние до ближайшей ячейки с заметным водосбором — ручья или
    # тальца.  Пик рядом с ручьём стоит в долине, а не на гребне.
    metrics.stream_distance = mask_distance(
        area > 20, peak.lat, float(outer)
    )
    metrics.ring_150_lower = ring_lower_drop(
        smoothed, peak.lat, thresholds["ring_150_radius"]
    )
    metrics.ring_600_lower = ring_lower_drop(smoothed, peak.lat, 600.0)
    metrics.ring_200_lower = ring_lower_drop(smoothed, peak.lat, 200.0)
    metrics.ring_300_lower = ring_lower_drop(smoothed, peak.lat, 300.0)
    metrics.ring_450_lower = ring_lower_drop(smoothed, peak.lat, 450.0)
    # Местный рельеф: размах высот в окне.  В Гималаях он в разы больше,
    # чем на Кавказе, поэтому часть признаков нормируется на него.
    metrics.relief = float(np.nanmax(smoothed) - np.nanmin(smoothed))
    metrics.ring_600_relative = metrics.ring_600_lower / max(
        metrics.relief, 1.0
    )
    # Поперечный профиль на 900 м: в Гималаях нужен большой масштаб,
    # иначе высокие склоны выглядят как провалы.
    metrics.cross900 = float(
        cross_profile(smoothed, peak.lat, 900.0)[row_center, column_center]
    )
    # Кривизна рельефа в точке: вогнутость (дно долины) положительна.
    metrics.curvature = float(
        ndimage.laplace(smoothed)[row_center, column_center]
    )

    near_max = find_near_max(smoothed, peak.lat, metrics.dem)
    if near_max is not None:
        metrics.near_max_distance = near_max[0]
        metrics.near_max_drop = near_max[1]

    # Расхождение высоты из OSM с рельефом.  У настоящей вершины оно
    # невелико; если же ele совсем не совпадает с DEM, пик поставлен
    # не на ту вершину.
    if peak.ele:
        metrics.ele_diff = abs(peak.ele - metrics.dem) / peak.ele * 100.0
        metrics.ele_below = (peak.ele - metrics.dem) / peak.ele * 100.0

    summit = find_summit(window, peak.lat, metrics.dem)
    if summit is not None:
        metrics.summit_distance = summit[0]
        metrics.summit_drop = summit[1]
    else:
        # Отдельной вершины в окне нет: считаем, что ближайшая — за
        # пределом поиска, а превышение — максимум окна.
        metrics.summit_distance = float(outer)
        metrics.summit_drop = float(np.max(smoothed) - metrics.dem)
    return metrics


def neighbour_stats(results, radius=2000.0):
    """Сравнение каждого пика с соседними пиками OSM.

    Ошибочный пик обычно оказывается ниже соседей: рядом стоят
    настоящие вершины, а он — в долине между ними.  Возвращает словарь
    osm_id -> (число соседей в радиусе, максимальная разница высот DEM
    с соседом, расстояние до ближайшего более высокого соседа).
    """
    grid = defaultdict(list)
    for peak, metrics, _verdict, _flags, _mismatch in results:
        if math.isnan(metrics.dem):
            continue
        grid[(int(peak.lon / 0.05), int(peak.lat / 0.05))].append((peak, metrics.dem))
    stats = {}
    for peak, metrics, _verdict, _flags, _mismatch in results:
        if math.isnan(metrics.dem):
            continue
        key = (int(peak.lon / 0.05), int(peak.lat / 0.05))
        neighbours = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(grid.get((key[0] + dx, key[1] + dy), ()))
        count = 0
        drop = 0.0
        nearest = float("nan")
        for other, other_dem in neighbours:
            if other is peak:
                continue
            distance = math.hypot(
                (other.lon - peak.lon) * 111320.0 * math.cos(math.radians(peak.lat)),
                (other.lat - peak.lat) * 111320.0,
            )
            if distance > radius:
                continue
            count += 1
            difference = other_dem - metrics.dem
            if difference > 10 and (math.isnan(nearest) or distance < nearest):
                nearest = distance
            if difference > drop:
                drop = difference
        stats[peak.osm_id] = (count, drop, nearest)
    return stats


def classify(peak, metrics, thresholds, neighbour=None):
    """Вердикт по пику.

    Проверяются только пики без названия и без ele: у остальных
    положение не трогаем.  Критерий ошибки — пик далеко от ближайшей
    локальной вершины рельефа и заметно ниже неё.

    error — безымянный пик явно не на вершине;
    suspect — безымянный пик с умеренными признаками ошибки;
    skipped — у пика есть название или ele (не проверяем);
    ok — безымянный пик стоит на вершине.
    """
    flags = []
    drop = metrics.drop_at(thresholds["radius"])
    deep = metrics.drop_at(thresholds["deep_radius"])
    # Расстояние до ближайшего соседнего пика OSM, который выше данного.
    neighbour_nearest = float("nan")
    if neighbour is not None:
        neighbour_nearest = neighbour[2]

    if peak.category == "named":
        return "skipped", ["не проверяется (с названием)"], None

    if not math.isnan(deep) and deep >= thresholds["deep_drop"]:
        flags.append(
            f"ниже вершины на {deep:.0f} м в радиусе {thresholds['deep_radius']} м"
        )
    if metrics.in_pit:
        flags.append("локальный минимум DEM (дно долины или яма)")
    elif not metrics.on_summit:
        flags.append("не локальный максимум DEM в пределах 90 м")
    elif not metrics.on_summit_wide:
        flags.append("не локальный максимум DEM в пределах 150 м")

    distance = metrics.summit_distance
    summit_drop = metrics.summit_drop
    if not math.isnan(distance):
        flags.append(
            f"ближайшая вершина рельефа в {distance:.0f} м"
            f", выше на {summit_drop:.0f} м"
        )
    if not math.isnan(metrics.cross600):
        if metrics.cross600 > thresholds["ridge_cross"]:
            flags.append(
                "не на хребте: поперёк склона выше на "
                f"{metrics.cross600:.0f} м (600 м)"
            )
        else:
            flags.append(
                "на хребте: поперёк склона ниже на "
                f"{-metrics.cross600:.0f} м (600 м)"
            )
    if not math.isnan(distance) and distance > thresholds["summit_limit"]:
        flags.append(
            f"ближайший локальный максимум дальше {distance:.0f} м"
        )
    if not math.isnan(metrics.ridge_distance):
        flags.append(f"до ближайшего гребня {metrics.ridge_distance:.0f} м")
    if not math.isnan(metrics.divide_distance):
        flags.append(
            f"до ближайшего водораздела {metrics.divide_distance:.0f} м"
        )
    if not math.isnan(metrics.sharp_distance):
        flags.append(f"до острого гребня {metrics.sharp_distance:.0f} м")
    if not math.isnan(metrics.ring_higher):
        flags.append(f"выше точки {metrics.ring_higher:.0%} кольца в 200 м")

    if neighbour is not None:
        _count, neighbour_drop, nearest = neighbour
        if neighbour_drop >= 30:
            where = (
                f", ближайший выше в {nearest:.0f} м"
                if not math.isnan(nearest)
                else ""
            )
            flags.append(
                f"ниже соседних пиков OSM на {neighbour_drop:.0f} м{where}"
            )

    # Вердикт: пик считается ошибочным, если он не на вершине и не на
    # хребте.  Правило — набор признаков, каждый из которых требует
    # выполнения двух условий: одиночное условие слишком часто
    # срабатывает на настоящих вершинах (например, «нет острого гребня
    # в 100 м» — у настоящей круглой вершины его и не бывает).
    # Все признаки считаются по рельефу SRTM.

    def far(value, limit, less=False):
        """Значение отличается от порога в нужную сторону."""
        if math.isnan(value):
            return False
        return value < limit if less else value > limit

    # Правило — набор конъюнкций: ошибка отмечается, если сработала хотя бы
    # одна.  Каждая конъюнкция требует двух признаков сразу: одиночное
    # условие срабатывает и на настоящих вершинах.
    reasons = []
    # 1. Далеко от строгого водораздела и поперёк склона в 900 м рельеф
    #    заметно выше — пик на дне долины.
    if (far(metrics.divide_strict_distance, thresholds["far_divide"])
            and far(metrics.cross900, thresholds["valley_cross900"])):
        reasons.append(
            "далеко от водораздела и поперёк склона выше на "
            f"{metrics.cross900:.0f} м (900 м)"
        )
    # 2. Нет острого гребня: перепад 25 м на 150 м и 15 м на 450 м.
    if (far(metrics.sharp_150_strict_distance,
            thresholds["no_steep_crest"])
            and far(metrics.sharp_450_distance, thresholds["no_crest_450"])):
        reasons.append("рядом нет острого гребня")
    # 3. Рельеф вокруг пологий, настоящей вершины рядом нет.
    if (far(metrics.sharp_150_strict_distance, thresholds["smooth_150"])
            and far(metrics.summit_distance, thresholds["summit_far"])):
        reasons.append(
            "пологое место без вершины, ближайшая вершина рельефа в "
            f"{metrics.summit_distance:.0f} м"
        )
    # 4. Много более высокого рельефа в 1200 м и провал в 300 м — пик
    #    в окружении других вершин, а не на гребне.
    if (far(metrics.ring_1200_higher, thresholds["valley_ring_wide"])
            and far(metrics.ring_300_lower, thresholds["pit_drop"])):
        reasons.append("вокруг много более высокого рельефа")
    # 5. Пик не на гребне и рельеф рядом намного ниже точки.
    if (far(metrics.ridge_strict_distance, thresholds["off_ridge"])
            and far(metrics.ring_200_lower, thresholds["ridge_drop"])):
        reasons.append("не на гребне и рельеф рядом ниже")
    # 6. Рельеф сильно ниже точки в 200 м, а ближайшая вершина далеко.
    if (far(metrics.ring_200_lower, thresholds["far_drop"])
            and far(metrics.near_max_distance, thresholds["near_max_d"])):
        reasons.append("рельеф рядом намного ниже, вершина далеко")
    # 7. Провал в 600 м больше половины местного рельефа и рядом ручей —
    #    пик в долине.
    if (far(metrics.ring_600_relative, thresholds["ring_600_relative"])
            and far(metrics.stream_distance, thresholds["stream_distance"],
                    less=True)):
        reasons.append("в долине, рядом ручей")
    # 8. Высота из OSM заметно выше рельефа и глубокий провал — пик
    #     сдвинут вниз от настоящей вершины.
    if (far(metrics.ele_below, thresholds["ele_below"])
            and far(metrics.ring_450_lower, thresholds["shift_drop"])):
        reasons.append(
            f"высота OSM выше рельефа на {metrics.ele_below:.0f} %"
        )
    # 9. Настоящей вершины рядом нет, но есть более высокий пик OSM
    #     далеко — пик стоит на пологом участке под ним.
    if (far(metrics.summit_distance, thresholds["summit_far"])
            and far(neighbour_nearest, thresholds["neighbour_nearest"])):
        reasons.append(
            "вершины рядом нет, а более высокий пик OSM в "
            f"{neighbour_nearest:.0f} м"
        )
    # 10. Точка в глубокой долине: много кольца выше и большой провал.
    if (far(metrics.ring_700_higher, thresholds["valley_ring_soft"])
            and far(metrics.ring_450_lower, thresholds["valley_drop_high"])):
        reasons.append("в окружении много более высокого рельефа")
    # 11. Высота из OSM и рельеф расходятся больше чем на 10 % — пик
    #     поставлен не на ту вершину.
    if far(metrics.ele_diff, thresholds["ele_diff"]):
        reasons.append(
            f"высота OSM и рельеф расходятся на {metrics.ele_diff:.0f} %"
        )

    flags.extend(reasons)
    verdict = "error" if reasons else "ok"
    return verdict, flags, None


def cluster_points(items, cell_degrees=0.02):
    """Группировка пиков в кластеры по соседству (сетка + слияние).

    На входе — пары (peak, ...); возвращает списки пиков.
    """
    cells = defaultdict(list)
    for item in items:
        peak = item[0]
        key = (
            int(math.floor(peak.lon / cell_degrees)),
            int(math.floor(peak.lat / cell_degrees)),
        )
        cells[key].append(item)
    clusters = []
    visited = set()
    for key in cells:
        if key in visited:
            continue
        stack = [key]
        visited.add(key)
        group = []
        while stack:
            current = stack.pop()
            group.extend(cells[current])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (current[0] + dx, current[1] + dy)
                    if neighbour in cells and neighbour not in visited:
                        visited.add(neighbour)
                        stack.append(neighbour)
        clusters.append(group)
    return clusters


def write_reports(peaks, results, output_dir, radii, thresholds, source_label="SRTM"):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "peaks.csv"
    header = [
        "osm_id", "lon", "lat", "category", "name", "ele", "dem", "max3", "min3", "max5",
        "on_summit", "on_summit_wide", "in_pit", "summit_distance", "summit_drop",
        "cross300", "cross600", "ridge_distance", "divide_distance",
        "sharp_distance", "sharp_100_distance", "sharp_300_distance",
        "ridge_strict_distance", "divide_strict_distance",
        "sharp_300_strict_distance", "sharp_450_distance",
        "sharp_150_strict_distance",
        "near_max_distance", "near_max_drop",
        "ring_higher", "ring_700_higher", "ring_150_lower", "ring_600_lower",
        "ring_200_lower", "ring_300_lower", "ring_450_lower",
        "ring_1200_higher", "stream_distance",
        "cross900", "curvature", "relief", "ring_600_relative",
        "ele_diff", "ele_below", "neighbour_nearest",
        "verdict", "flags", "timestamp", "user", "changeset", "source", "file",
    ]
    for radius in radii:
        header.append(f"drop{radius}")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for peak, metrics, verdict, flags, _mismatch in results:
            row = [
                peak.osm_id,
                f"{peak.lon:.7f}",
                f"{peak.lat:.7f}",
                peak.category,
                peak.name,
                "" if peak.ele is None else f"{peak.ele:.0f}",
                f"{metrics.dem:.0f}",
                f"{metrics.max3:.0f}",
                f"{metrics.min3:.0f}",
                f"{metrics.max5:.0f}",
                "1" if metrics.on_summit else "0",
                "1" if metrics.on_summit_wide else "0",
                "1" if metrics.in_pit else "0",
                f"{metrics.summit_distance:.0f}",
                f"{metrics.summit_drop:.0f}",
                f"{metrics.cross300:.0f}",
                f"{metrics.cross600:.0f}",
                f"{metrics.ridge_distance:.0f}",
                f"{metrics.divide_distance:.0f}",
                f"{metrics.sharp_distance:.0f}",
                f"{metrics.sharp_100_distance:.0f}",
                f"{metrics.sharp_300_distance:.0f}",
                f"{metrics.ridge_strict_distance:.0f}",
                f"{metrics.divide_strict_distance:.0f}",
                f"{metrics.sharp_300_strict_distance:.0f}",
                f"{metrics.sharp_450_distance:.0f}",
                f"{metrics.sharp_150_strict_distance:.0f}",
                f"{metrics.near_max_distance:.0f}",
                f"{metrics.near_max_drop:.0f}",
                f"{metrics.ring_higher:.2f}",
                f"{metrics.ring_700_higher:.2f}",
                f"{metrics.ring_150_lower:.0f}",
                f"{metrics.ring_600_lower:.0f}",
                f"{metrics.ring_200_lower:.0f}",
                f"{metrics.ring_300_lower:.0f}",
                f"{metrics.ring_450_lower:.0f}",
                f"{metrics.ring_1200_higher:.2f}",
                f"{metrics.stream_distance:.0f}",
                f"{metrics.cross900:.0f}",
                f"{metrics.curvature:.2f}",
                f"{metrics.relief:.0f}",
                f"{metrics.ring_600_relative:.2f}",
                f"{metrics.ele_diff:.1f}",
                f"{metrics.ele_below:.1f}",
                f"{metrics.neighbour_nearest:.0f}",
                verdict,
                "; ".join(flags),
                peak.timestamp,
                peak.user,
                str(peak.changeset),
                peak.tags.get("source", ""),
                peak.source_file,
            ]
            for radius in radii:
                value = metrics.drop_at(radius)
                row.append("" if math.isnan(value) else f"{value:.0f}")
            # Число колонок в строке должно совпадать с шапкой: иначе
            # значения разъезжаются по столбцам и CSV врёт.
            if len(row) != len(header):
                raise ValueError(
                    f"в строке {len(row)} значений, в шапке {len(header)}"
                )
            writer.writerow(row)

    features = []
    for peak, metrics, verdict, flags, mismatch in results:
        if verdict not in ("error", "suspect"):
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(peak.lon, 7), round(peak.lat, 7)],
            },
            "properties": {
                "osm_id": f"node/{peak.osm_id}",
                "verdict": verdict,
                "category": peak.category,
                "name": peak.name,
                "ele": peak.ele,
                "dem": round(metrics.dem, 1),
                "max3": round(metrics.max3, 1),
                "min3": round(metrics.min3, 1),
                "max5": round(metrics.max5, 1),
                "on_summit": metrics.on_summit,
                "on_summit_wide": metrics.on_summit_wide,
                "in_pit": metrics.in_pit,
                "summit_distance": round(metrics.summit_distance, 1),
                "summit_drop": round(metrics.summit_drop, 1),
                "cross300": round(metrics.cross300, 1),
                "cross600": round(metrics.cross600, 1),
                "ridge_distance": round(metrics.ridge_distance, 1),
                "divide_distance": round(metrics.divide_distance, 1),
                "sharp_distance": round(metrics.sharp_distance, 1),
                "sharp_100_distance": round(metrics.sharp_100_distance, 1),
                "sharp_300_distance": round(metrics.sharp_300_distance, 1),
                "ridge_strict_distance": round(metrics.ridge_strict_distance, 1),
                "divide_strict_distance": round(
                    metrics.divide_strict_distance, 1),
                "sharp_300_strict_distance": round(
                    metrics.sharp_300_strict_distance, 1),
                "sharp_150_strict_distance": round(
                    metrics.sharp_150_strict_distance, 1),
                "sharp_450_distance": round(metrics.sharp_450_distance, 1),
                "near_max_distance": round(metrics.near_max_distance, 1),
                "near_max_drop": round(metrics.near_max_drop, 1),
                "ring_600_lower": round(metrics.ring_600_lower, 1),
                "ring_200_lower": round(metrics.ring_200_lower, 1),
                "ring_300_lower": round(metrics.ring_300_lower, 1),
                "ring_450_lower": round(metrics.ring_450_lower, 1),
                "ring_1200_higher": round(metrics.ring_1200_higher, 3),
                "stream_distance": round(metrics.stream_distance, 1),
                "cross900": round(metrics.cross900, 1),
                "curvature": round(metrics.curvature, 3),
                "relief": round(metrics.relief, 1),
                "ring_600_relative": round(metrics.ring_600_relative, 3),
                "ele_diff": round(metrics.ele_diff, 2),
                "ele_below": round(metrics.ele_below, 2),
                "neighbour_nearest": (
                    "" if math.isnan(metrics.neighbour_nearest)
                    else round(metrics.neighbour_nearest, 1)
                ),
                "ring_700_higher": round(metrics.ring_700_higher, 3),
                "ring_150_lower": round(metrics.ring_150_lower, 1),
                "ring_higher": round(metrics.ring_higher, 3),
                "flags": "; ".join(flags),
                "timestamp": peak.timestamp,
                "user": peak.user,
                "changeset": peak.changeset,
                "source": peak.tags.get("source", ""),
                "file": peak.source_file,
            },
        })
    geojson_path = output_dir / "suspects.geojson"
    geojson_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    report_path = output_dir / "report.txt"
    report_path.write_text(
        build_report(peaks, results, radii, thresholds, source_label),
        encoding="utf-8",
    )
    return csv_path, geojson_path, report_path


def build_report(peaks, results, radii, thresholds, source_label="SRTM"):
    verdicts = Counter(item[2] for item in results)
    total = len(results)
    anonymous = [item for item in results if item[0].category == "anonymous"]
    ele_only = [item for item in results if item[0].category == "ele_only"]
    skipped = [item for item in results if item[2] == "skipped"]
    errors = [item for item in results if item[2] == "error"]
    without_dem = sum(1 for item in results if math.isnan(item[1].dem))

    def errors_in(group):
        return [item for item in group if item[2] == "error"]

    lines = []
    lines.append(f"Проверка положения пиков OSM по рельефу {source_label}")
    lines.append("=" * 62)
    lines.append(f"Всего пиков: {total}")
    if without_dem:
        lines.append(f"  без данных DEM (тайл не скачан): {without_dem}")
    lines.append(f"  с названием (не проверяем): {len(skipped)}")
    lines.append(f"  без названия и без ele: {len(anonymous)}")
    lines.append(f"  без названия, но с ele: {len(ele_only)}")
    lines.append("")
    lines.append("Вердикты: " + ", ".join(
        f"{name}={count}" for name, count in sorted(verdicts.items())
    ))
    lines.append("")

    lines.append("Результат по категориям")
    lines.append("-" * 62)
    lines.append(f"{'категория':<28}{'пиков':>8}{'ошибок':>8}{'доля':>8}")
    for label, group in (
        ("без названия и без ele", anonymous),
        ("без названия, с ele", ele_only),
        ("все проверенные", anonymous + ele_only),
    ):
        wrong = len(errors_in(group))
        share = wrong / len(group) * 100 if group else 0
        lines.append(f"{label:<28}{len(group):>8}{wrong:>8}{share:>7.1f}%")
    lines.append("")
    lines.append(
        "Ошибка: пик не на вершине и не на хребте.  Правило — набор пар "
        "признаков: ошибка отмечается, если сработала хотя бы одна пара.  "
        "Одиночное условие срабатывает и на настоящих вершинах.  "
        "Срабатывает любое из:\n"
        "  * дальше "
        f"{thresholds['far_divide']:.0f} м от строгого водораздела и "
        f"поперёк склона в 900 м рельеф выше на "
        f"{thresholds['valley_cross900']:.0f} м;\n"
        "  * нет острого гребня: перепад 25 м на 150 м дальше "
        f"{thresholds['no_steep_crest']:.0f} м и перепад 15 м на 450 м "
        f"дальше {thresholds['no_crest_450']:.0f} м;\n"
        "  * перепад 25 м на 150 м дальше "
        f"{thresholds['smooth_150']:.0f} м и ближайшая вершина рельефа "
        f"дальше {thresholds['summit_far']:.0f} м — вокруг пологое место, "
        "а вершины рядом нет;\n"
        "  * выше точки больше "
        f"{thresholds['valley_ring_wide']:.0%} кольца в 1200 м и провал в "
        f"300 м больше {thresholds['pit_drop']:.0f} м;\n"
        "  * пик не на гребне (обе стороны ниже на 20 м) и провал в 200 м "
        f"больше {thresholds['ridge_drop']:.0f} м;\n"
        "  * провал в 200 м больше "
        f"{thresholds['far_drop']:.0f} м и ближайшая вершина дальше "
        f"{thresholds['near_max_d']:.0f} м;\n"
        "  * провал в 600 м больше "
        f"{thresholds['ring_600_relative']:.0%} местного рельефа и ручей "
        f"ближе {thresholds['stream_distance']:.0f} м — пик в долине;\n"
        "  * высота OSM выше рельефа более чем на "
        f"{thresholds['ele_below']:.0f} процентов и провал в 450 м больше "
        f"{thresholds['shift_drop']:.0f} м;\n"
        "  * ближайшая вершина рельефа дальше "
        f"{thresholds['summit_far']:.0f} м, а более высокий пик OSM — дальше "
        f"{thresholds['neighbour_nearest']:.0f} м;\n"
        "  * выше точки больше "
        f"{thresholds['valley_ring_soft']:.0%} кольца в "
        f"{thresholds['ring_700_radius']:.0f} м и провал в 450 м больше "
        f"{thresholds['valley_drop_high']:.0f} м;\n"
        "  * высота OSM и рельеф расходятся более чем на "
        f"{thresholds['ele_diff']:.0f} процентов — пик поставлен не на ту "
        "вершину.\n"
        f"Признаки — по рельефу {source_label} и высоте из тега OSM ele."
    )
    lines.append("")

    if errors:
        lines.append("Ошибки: срез по источнику данных")
        lines.append("-" * 62)
        sources = Counter(
            item[0].tags.get("source", "(нет source)") for item in errors
        )
        for source, count in sources.most_common(10):
            lines.append(f"  {count:>5}  {source}")
        lines.append("")
        lines.append("Ошибки: срез по дате правки")
        lines.append("-" * 62)
        dates = Counter(item[0].timestamp[:10] for item in errors)
        for date, count in dates.most_common(10):
            lines.append(f"  {count:>5}  {date}")
        lines.append("")
        lines.append("Ошибки: срез по стране (файл OSM)")
        lines.append("-" * 62)
        files = Counter(item[0].source_file for item in errors)
        for source, count in files.most_common(10):
            lines.append(f"  {count:>5}  {source}")
        lines.append("")

    if errors:
        clusters = cluster_points(errors)
        clusters.sort(key=len, reverse=True)
        lines.append(f"Кластеры ошибок (всего: {len(clusters)})")
        lines.append("-" * 62)
        for group in clusters[:15]:
            if len(group) < 2:
                continue
            lon = sum(item[0].lon for item in group) / len(group)
            lat = sum(item[0].lat for item in group) / len(group)
            dates = Counter(item[0].timestamp[:10] for item in group)
            lines.append(
                f"  {len(group):>4} пиков  {lat:8.4f} N  {lon:9.4f} E   "
                f"даты: {', '.join(f'{d}×{c}' for d, c in dates.most_common(3))}"
            )
        lines.append("")

    lines.append("Самые явные ошибки (дальше всего от гребня)")
    lines.append("-" * 62)
    ranked = sorted(errors, key=lambda item: item[1].ridge_distance, reverse=True)
    for peak, metrics, _verdict, _flags, _mismatch in ranked[:30]:
        lines.append(
            f"  node/{peak.osm_id:<12} {peak.lat:8.4f} N {peak.lon:9.4f} E  "
            f"{peak.category:<10} DEM={metrics.dem:6.0f} м  "
            f"гребень в {metrics.ridge_distance:5.0f} м  {peak.timestamp[:10]}"
        )
    lines.append("")
    return "\n".join(lines)


_WORKER = {}


def _measure_init(dem_spec, radii, thresholds):
    _WORKER["dem"] = make_dem(dem_spec)
    _WORKER["radii"] = radii
    _WORKER["thresholds"] = thresholds


def _measure_one(peak):
    return measure(peak, _WORKER["dem"], _WORKER["radii"], _WORKER["thresholds"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pbf", action="append", default=[], required=True,
        help="файл OSM PBF (можно указать несколько раз)",
    )
    parser.add_argument(
        "--natural", default="peak",
        help="значения natural через запятую (по умолчанию peak)",
    )
    parser.add_argument(
        "--out", default="peak_check",
        help="каталог для отчётов (CSV, GeoJSON, текст)",
    )
    parser.add_argument(
        "--dem-cache", default=str(DEFAULT_DEM_CACHE),
        help="каталог кэша рельефа: тайлы SRTM и окна растровых наборов",
    )
    parser.add_argument(
        "--dem-source", default="srtm",
        choices=[item.key for item in dem_sources.SOURCES],
        help="источник рельефа, как в плагине (по умолчанию srtm)",
    )
    parser.add_argument(
        "--dem-file", default=None,
        help="готовый растр рельефа (для manual или вместо загрузки)",
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="не скачивать недостающие тайлы SRTM",
    )
    parser.add_argument(
        "--osmium", default=None, help="путь к бинарю osmium",
    )
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="число процессов для расчёта рельефа (0 — по числу ядер)",
    )
    parser.add_argument(
        "--radius", type=int, default=300,
        help="радиус окна для проверки вершины, м (по умолчанию 300)",
    )
    parser.add_argument(
        "--deep-radius", type=int, default=600,
        help="радиус «глубокой» проверки, м (по умолчанию 600)",
    )
    parser.add_argument(
        "--deep-drop", type=float, default=60.0,
        help="провал от вершины, за которым считаем ошибкой, м",
    )
    parser.add_argument(
        "--summit-limit", type=float, default=200.0,
        help="предельное расстояние до ближайшего локального максимума, м",
    )
    parser.add_argument(
        "--sharp-radius", type=float, default=150.0,
        help="масштаб поперечного профиля для острого гребня, м",
    )
    parser.add_argument(
        "--sharp-cross", type=float, default=-15.0,
        help="поперечный профиль острого гребня, м",
    )
    parser.add_argument(
        "--ring-radius", type=float, default=200.0,
        help="радиус кольца для доли более высокого рельефа, м",
    )
    parser.add_argument(
        "--ring-700-radius", type=float, default=700.0,
        help="радиус кольца для признака «глубокая долина», м",
    )
    parser.add_argument(
        "--ring-150-radius", type=float, default=150.0,
        help="радиус кольца для признака «край обрыва», м",
    )
    parser.add_argument(
        "--divide-acc", type=int, default=3,
        help="площадь водосбора (в ячейках), ниже которой ячейка "
             "считается водоразделом",
    )
    parser.add_argument(
        "--divide-sigma", type=float, default=2.0,
        help="сглаживание DEM перед расчётом стока, пикселей",
    )
    parser.add_argument(
        "--ridge-radius", type=int, default=0,
        help="один радиус поиска гребня, м (0 — несколько масштабов)",
    )
    parser.add_argument(
        "--ridge-cross", type=float, default=-10.0,
        help="поперечный профиль (600 м), при котором ячейка считается "
             "гребнем, м",
    )
    # Пороги нового правила: каждое условие требует двух признаков сразу.
    parser.add_argument(
        "--off-ridge", type=float, default=40.0,
        help="расстояние до строгого гребня (обе стороны ниже на 20 м), "
             "выше которого пик считается не на гребне, м",
    )
    parser.add_argument(
        "--no-steep-crest", type=float, default=250.0,
        help="расстояние до крутого гребня (перепад 25 м на 300 м), м",
    )
    parser.add_argument(
        "--pit-drop", type=float, default=120.0,
        help="перепад до самой низкой точки в 300 м для признака ямы, м",
    )
    parser.add_argument(
        "--far-drop", type=float, default=240.0,
        help="перепад до самой низкой точки в 200 м, при котором вершина "
             "далеко, м",
    )
    parser.add_argument(
        "--far-divide", type=float, default=50.0,
        help="расстояние до строгого водораздела, м",
    )
    parser.add_argument(
        "--deep-ring-drop", type=float, default=450.0,
        help="перепад до самой низкой точки в 600 м, м",
    )
    parser.add_argument(
        "--no-crest-450", type=float, default=40.0,
        help="расстояние до гребня (перепад 15 м на 450 м), м",
    )
    # Пороги нового правила (набор конъюнкций).
    parser.add_argument(
        "--valley-cross900", type=float, default=120.0,
        help="поперечный профиль в 900 м для признака дна долины, м",
    )
    parser.add_argument(
        "--valley-drop-high", type=float, default=350.0,
        help="провал до низа кольца в 450 м для глубокой долины, м",
    )
    parser.add_argument(
        "--shift-drop", type=float, default=500.0,
        help="провал до низа кольца в 450 м, при котором пик считается "
             "сдвинутым вниз от вершины, м",
    )
    parser.add_argument(
        "--valley-ring-soft", type=float, default=0.45,
        help="доля кольца в 700 м выше точки, при которой пик окружён "
             "более высоким рельефом",
    )
    parser.add_argument(
        "--near-max-d", type=float, default=300.0,
        help="расстояние до ближайшего локального максимума, м",
    )
    parser.add_argument(
        "--ele-below", type=float, default=4.0,
        help="на сколько процентов высота OSM выше рельефа, чтобы пик "
             "считался сдвинутым",
    )
    parser.add_argument(
        "--ring-600-relative", type=float, default=0.5,
        help="провал в 600 м в долях местного рельефа",
    )
    parser.add_argument(
        "--valley-ring-wide", type=float, default=0.65,
        help="доля кольца в 1200 м выше точки, при которой вокруг пика "
             "много более высокого рельефа",
    )
    parser.add_argument(
        "--stream-distance", type=float, default=200.0,
        help="расстояние до ручья, ближе которого пик считается в долине, м",
    )
    parser.add_argument(
        "--smooth-150", type=float, default=90.0,
        help="расстояние до крутого гребня (перепад 25 м на 150 м), при "
             "котором рельеф вокруг считается пологим, м",
    )
    parser.add_argument(
        "--summit-far", type=float, default=600.0,
        help="расстояние до ближайшей вершины рельефа, при котором вершины "
             "рядом нет, м",
    )
    parser.add_argument(
        "--ridge-drop", type=float, default=170.0,
        help="перепад до самой низкой точки в 200 м для признака «не на "
             "гребне и рельеф рядом ниже», м",
    )
    parser.add_argument(
        "--neighbour-nearest", type=float, default=1400.0,
        help="расстояние до ближайшего более высокого пика OSM, м",
    )
    parser.add_argument(
        "--ele-diff", type=float, default=10.0,
        help="расхождение высоты OSM и рельефа, при котором пик считается "
             "поставленным не на ту вершину, в процентах",
    )
    arguments = parser.parse_args(argv)

    natural_values = [item.strip() for item in arguments.natural.split(",") if item.strip()]
    thresholds = {
        "radius": arguments.radius,
        "deep_radius": arguments.deep_radius,
        "deep_drop": arguments.deep_drop,
        "ridge_cross": arguments.ridge_cross,
        "summit_limit": arguments.summit_limit,
        "sharp_radius": arguments.sharp_radius,
        "sharp_cross": arguments.sharp_cross,
        "ring_radius": arguments.ring_radius,
        "ring_700_radius": arguments.ring_700_radius,
        "ring_150_radius": arguments.ring_150_radius,
        "divide_acc": arguments.divide_acc,
        "divide_sigma": arguments.divide_sigma,
        "ridge_radius": arguments.ridge_radius,
        "off_ridge": arguments.off_ridge,
        "no_steep_crest": arguments.no_steep_crest,
        "pit_drop": arguments.pit_drop,
        "far_drop": arguments.far_drop,
        "far_divide": arguments.far_divide,
        "deep_drop": arguments.deep_ring_drop,
        "no_crest_450": arguments.no_crest_450,
        "valley_cross900": arguments.valley_cross900,
        "valley_drop_high": arguments.valley_drop_high,
        "shift_drop": arguments.shift_drop,
        "valley_ring_soft": arguments.valley_ring_soft,
        "near_max_d": arguments.near_max_d,
        "ele_below": arguments.ele_below,
        "ring_600_relative": arguments.ring_600_relative,
        "ele_diff": arguments.ele_diff,
        "smooth_150": arguments.smooth_150,
        "summit_far": arguments.summit_far,
        "ridge_drop": arguments.ridge_drop,
        "neighbour_nearest": arguments.neighbour_nearest,
        "valley_ring_wide": arguments.valley_ring_wide,
        "stream_distance": arguments.stream_distance,
    }
    radii = sorted({arguments.radius, arguments.deep_radius, 1500})

    osmium_bin = find_osmium(arguments.osmium)
    print(f"Чтение пиков из {len(arguments.pbf)} файла(ов)")
    peaks = read_peaks(arguments.pbf, natural_values, osmium_bin)
    print(f"Найдено пиков: {len(peaks)}")

    if arguments.dem_source == "manual" and not arguments.dem_file:
        parser.error("для --dem-source manual укажите --dem-file")
    dem_spec = {
        "cache_dir": arguments.dem_cache,
        "download": not arguments.no_download,
        "path": None,
    }
    workdir = None
    if arguments.dem_file or arguments.dem_source != "srtm":
        workdir = tempfile.mkdtemp(prefix="peak_dem_")
        dem_path = prepare_dem_raster(
            arguments.dem_source, peaks_bbox(peaks),
            arguments.dem_cache, workdir, arguments.dem_file,
        )
        dem_spec["path"] = str(dem_path)
        print(f"Рельеф: {arguments.dem_source} ({dem_path})", flush=True)

    jobs = arguments.jobs or (os.cpu_count() or 1)
    jobs = max(1, min(jobs, len(peaks)))
    print(f"Расчёт рельефа в {jobs} процессе(ах)", flush=True)
    results = []
    if jobs == 1:
        dem = make_dem(dem_spec)
        for peak in peaks:
            results.append((peak, measure(peak, dem, radii, thresholds), "ok", [], None))
    else:
        with ProcessPoolExecutor(
            max_workers=jobs,
            initializer=_measure_init,
            initargs=(dem_spec, radii, thresholds),
        ) as pool:
            measured = list(pool.map(_measure_one, peaks, chunksize=64))
        results = [
            (peak, metrics, "ok", [], None)
            for peak, metrics in zip(peaks, measured)
        ]

    print("Сравнение с соседними пиками", flush=True)
    neighbours = neighbour_stats(results)
    for peak, metrics, _verdict, _flags, _mismatch in results:
        neighbour = neighbours.get(peak.osm_id)
        if neighbour is not None:
            metrics.neighbour_nearest = neighbour[2]
    results = [
        (peak, metrics, *classify(peak, metrics, thresholds, neighbours.get(peak.osm_id)))
        for peak, metrics, _verdict, _flags, _mismatch in results
    ]

    source_label = dem_sources.source(arguments.dem_source).short
    if arguments.dem_file:
        source_label = Path(arguments.dem_file).name
    csv_path, geojson_path, report_path = write_reports(
        peaks, results, arguments.out, radii, thresholds, source_label
    )
    if workdir is not None:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"Отчёты: {csv_path}, {geojson_path}, {report_path}")
    print(build_report(peaks, results, radii, thresholds, source_label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
