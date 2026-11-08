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

Для ALOS, Copernicus и NASADEM автоматически включается профиль порогов:
их поверхность сдвигает часть метрик относительно SRTM, и базовые пороги
ловят верные пики.  Профиль можно отключить ``--no-source-profile``.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tests  # noqa: E402,F401  (регистрирует пакет qgis_xtopo)

from qgis_xtopo.core.bbox import BBox  # noqa: E402
from qgis_xtopo.core import dem_sources  # noqa: E402
from qgis_xtopo.core.peak_placement import (  # noqa: E402
    NAME_TAGS, PIXEL_DEGREES, SOURCE_THRESHOLD_PROFILES,
    classify, measure, neighbour_stats,
)

# Размер тайла SRTM (SRTM1): 3601x3601 ячеек по 1/3600 градуса.
TILE_PIXELS = 3601

SRTM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{dir}/{tile}.hgt.gz"

DEFAULT_DEM_CACHE = Path.home() / "qgis_projects" / "qgisxtopo-cache" / "srtm"

# Базовые значения аргументов, которые меняет профиль.  Явно заданный
# пользователем порог (отличный от базового) профиль не перекрывает.
PROFILE_ARGUMENT_DEFAULTS = {
    "no_crest_450": 40.0,
    "far_drop": 240.0,
    "near_max_d": 300.0,
    "stream_distance": 200.0,
    "ele_below": 4.0,
    "summit_far_neighbour": 600.0,
    "valley_summit_min": 0.0,
}

OPL_TAIL = re.compile(r"\s+x(-?[\d.]+)\s+y(-?[\d.]+)\s*$")
OPL_HEAD = re.compile(r"\s+v(\d+)\s+d\S*\s+c(\d+)\s+t(\S+)\s+i(\d+)\s+u(.*)$")


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


# Радиусы (в метрах), на которых ищется выпуклость гребня.  Мелкие
# ловят острые гребни, крупные — широкие и пологие.
RIDGE_SCALES = (60.0, 90.0, 150.0, 240.0, 360.0, 500.0)


# Соседние ячейки в порядке обхода для стока по D8.
FLOW_NEIGHBOURS = (
    (-1, -1), (-1, 0), (-1, 1), (0, -1),
    (0, 1), (1, -1), (1, 0), (1, 1),
)


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
    valley_suffix = ""
    if thresholds["valley_summit_min"] > 0:
        valley_suffix = (
            ", а ближайшая вершина рельефа — дальше "
            f"{thresholds['valley_summit_min']:.0f} м"
        )
    lines.append(
        "Ошибка: пик не на вершине и не на хребте.  Правило — набор пар "
        "признаков: ошибка отмечается, если сработала хотя бы одна пара.  "
        "Одиночное условие срабатывает и на настоящих вершинах.  "
        "Срабатывает любое из:\n"
        "  * дальше "
        f"{thresholds['far_divide']:.0f} м от строгого водораздела и "
        f"поперёк склона в 900 м рельеф выше на "
        f"{thresholds['valley_cross900']:.0f} м{valley_suffix};\n"
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
        f"{thresholds['summit_far_neighbour']:.0f} м, а более высокий "
        "пик OSM — дальше "
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


def apply_source_profile(arguments):
    """Подставляет профиль порогов для растровых DSM (ALOS, Copernicus)."""
    if arguments.no_source_profile:
        return
    profile = SOURCE_THRESHOLD_PROFILES.get(arguments.dem_source)
    if not profile:
        return
    for name, value in profile.items():
        if getattr(arguments, name) == PROFILE_ARGUMENT_DEFAULTS[name]:
            setattr(arguments, name, value)


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
        help="источник рельефа, как в плагине (по умолчанию srtm); "
             "для alos/cop30/nasadem включается устойчивый профиль порогов",
    )
    parser.add_argument(
        "--dem-file", default=None,
        help="готовый растр рельефа (для manual или вместо загрузки)",
    )
    parser.add_argument(
        "--no-source-profile", action="store_true",
        help="не подставлять профиль порогов для ALOS/Copernicus/NASADEM",
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
        "--valley-summit", dest="valley_summit_min", type=float,
        default=0.0,
        help="минимальное расстояние до ближайшей вершины рельефа для "
             "признака дна долины, м (0 — не проверять)",
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
        "--neighbour-summit", dest="summit_far_neighbour", type=float,
        default=600.0,
        help="то же для пары «вершины рядом нет, а более высокий пик OSM "
             "далеко», м",
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
    apply_source_profile(arguments)

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
        "valley_summit_min": arguments.valley_summit_min,
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
        "summit_far_neighbour": arguments.summit_far_neighbour,
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
