#!/usr/bin/env python3
"""Проверка положения пиков OSM (natural=peak) по рельефу SRTM.

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
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage

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
    if not math.isnan(distance) and distance > thresholds["summit_distance"]:
        flags.append(
            f"ближайший локальный максимум дальше {distance:.0f} м"
        )
    if not math.isnan(metrics.ridge_distance):
        flags.append(f"до ближайшего гребня {metrics.ridge_distance:.0f} м")

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

    # Ошибка — пик далеко от ближайшего гребня.  Небольшое смещение
    # (до 100 м) допустимо: пики в OSM часто стоят на отрогах, чуть в
    # стороне от линии гребня.  Расстояние до локального максимума
    # пишется в метрики, но в вердикт не входит: на широких хребтах
    # вершина законно бывает в километре.
    far_from_ridge = (
        not math.isnan(metrics.ridge_distance)
        and metrics.ridge_distance > thresholds["ridge_distance"]
    )
    # Запасной признак: рельеф заметно выше уже в пределах 90 м, а на
    # 600 м поперёк склона — ещё выше.  Это крутой склон, а не вершина.
    steep = (
        not math.isnan(metrics.max3)
        and not math.isnan(metrics.cross600)
        and metrics.max3 - metrics.dem >= thresholds["slope_drop"]
        and metrics.cross600 > thresholds["slope_cross"]
    )
    if steep:
        flags.append(
            f"рельеф выше на {metrics.max3 - metrics.dem:.0f} м уже в 90 м"
        )
    verdict = "error" if (far_from_ridge or steep) else "ok"
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


def write_reports(peaks, results, output_dir, radii, thresholds):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "peaks.csv"
    header = [
        "osm_id", "lon", "lat", "category", "name", "ele", "dem", "max3", "min3", "max5",
        "on_summit", "on_summit_wide", "in_pit", "summit_distance", "summit_drop",
        "cross300", "cross600", "ridge_distance", "verdict", "flags", "timestamp", "user", "changeset", "source", "file",
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
        build_report(peaks, results, radii, thresholds),
        encoding="utf-8",
    )
    return csv_path, geojson_path, report_path


def build_report(peaks, results, radii, thresholds):
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
    lines.append("Проверка положения пиков OSM по рельефу SRTM")
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
        "Ошибка: ближайший гребень дальше "
        f"{thresholds['ridge_distance']:.0f} м, либо рельеф выше пика на "
        f"{thresholds['slope_drop']:.0f} м уже в 90 м при поперечном "
        f"профиле больше {thresholds['slope_cross']:.0f} м.  Выпуклость "
        "гребня ищется на нескольких масштабах (60–500 м), поэтому широкие "
        "и пологие хребты тоже распознаются."
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


def _measure_init(cache_dir, radii, thresholds, download):
    _WORKER["dem"] = DemTiles(cache_dir, download=download)
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
        help="каталог кэша тайлов SRTM",
    )
    parser.add_argument(
        "--no-download", action="store_true",
        help="не скачивать недостающие тайлы рельефа",
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
        "--drop", type=float, default=25.0,
        help="минимальный провал от вершины для подозрения, м",
    )
    parser.add_argument(
        "--deep-drop", type=float, default=60.0,
        help="провал от вершины, за которым считаем ошибкой, м",
    )
    parser.add_argument(
        "--summit-distance", type=float, default=200.0,
        help="предельное расстояние до ближайшего локального максимума, м",
    )
    parser.add_argument(
        "--ridge-distance", type=float, default=60.0,
        help="предельное расстояние до ближайшего гребня, м",
    )
    parser.add_argument(
        "--slope-drop", type=float, default=40.0,
        help="превышение рельефа в 90 м для признака крутого склона, м",
    )
    parser.add_argument(
        "--slope-cross", type=float, default=50.0,
        help="поперечный профиль в 600 м для признака крутого склона, м",
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
    arguments = parser.parse_args(argv)

    natural_values = [item.strip() for item in arguments.natural.split(",") if item.strip()]
    thresholds = {
        "radius": arguments.radius,
        "deep_radius": arguments.deep_radius,
        "drop": arguments.drop,
        "deep_drop": arguments.deep_drop,
        "ridge_cross": arguments.ridge_cross,
        "summit_distance": arguments.summit_distance,
        "ridge_distance": arguments.ridge_distance,
        "slope_drop": arguments.slope_drop,
        "slope_cross": arguments.slope_cross,
        "ridge_radius": arguments.ridge_radius,
    }
    radii = sorted({arguments.radius, arguments.deep_radius, 1500})

    osmium_bin = find_osmium(arguments.osmium)
    print(f"Чтение пиков из {len(arguments.pbf)} файла(ов)")
    peaks = read_peaks(arguments.pbf, natural_values, osmium_bin)
    print(f"Найдено пиков: {len(peaks)}")

    jobs = arguments.jobs or (os.cpu_count() or 1)
    jobs = max(1, min(jobs, len(peaks)))
    print(f"Расчёт рельефа в {jobs} процессе(ах)", flush=True)
    results = []
    if jobs == 1:
        dem = DemTiles(arguments.dem_cache, download=not arguments.no_download)
        for peak in peaks:
            results.append((peak, measure(peak, dem, radii, thresholds), "ok", [], None))
    else:
        with ProcessPoolExecutor(
            max_workers=jobs,
            initializer=_measure_init,
            initargs=(
                arguments.dem_cache, radii, thresholds,
                not arguments.no_download,
            ),
        ) as pool:
            measured = list(pool.map(_measure_one, peaks, chunksize=64))
        results = [
            (peak, metrics, "ok", [], None)
            for peak, metrics in zip(peaks, measured)
        ]

    print("Сравнение с соседними пиками", flush=True)
    neighbours = neighbour_stats(results)
    results = [
        (peak, metrics, *classify(peak, metrics, thresholds, neighbours.get(peak.osm_id)))
        for peak, metrics, _verdict, _flags, _mismatch in results
    ]

    csv_path, geojson_path, report_path = write_reports(
        peaks, results, arguments.out, radii, thresholds
    )
    print(f"Отчёты: {csv_path}, {geojson_path}, {report_path}")
    print(build_report(peaks, results, radii, thresholds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
