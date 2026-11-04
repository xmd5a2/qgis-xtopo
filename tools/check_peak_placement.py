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
            tags[key] = value
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
    for path in pbf_files:
        path = Path(path).expanduser()
        if not path.exists():
            print(f"WARNING: файл не найден, пропущен: {path}", file=sys.stderr)
            continue
        command = [
            osmium_bin, "tags-filter", "-f", "opl", str(path), expression,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                f"osmium tags-filter не отработал для {path}:\n{result.stderr}"
            )
        for line in result.stdout.splitlines():
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


def measure(peak, dem, radii):
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
    return metrics


def classify(peak, metrics, thresholds):
    """Вердикт по пику.

    Основная группа проверки — пики без названия: у них ошибка
    положения встречается вдвое чаще, а названные пики почти всегда
    поставлены на настоящую вершину (их геометрическое срабатывание —
    чаще погрешность самого рельефа).

    error — безымянный пик явно не на вершине;
    error_named — то же, но у пика есть название (редко, стоит взглянуть);
    named_suspect — названный пик не проходит проверку положения
    (вероятнее всего, погрешность рельефа или широко��ая вершина);
    suspect — безымянный пик с умеренными признаками ошибки;
    mismatch — расхождение тега ele с DEM;
    ok — остальное.
    """
    flags = []
    drop = metrics.drop_at(thresholds["radius"])
    deep = metrics.drop_at(thresholds["deep_radius"])

    named = bool(peak.name)
    if not named:
        flags.append("без названия")

    if not math.isnan(deep) and deep >= thresholds["deep_drop"]:
        flags.append(
            f"ниже вершины на {deep:.0f} м в радиусе {thresholds['deep_radius']} м"
        )
    elif not math.isnan(drop) and drop >= thresholds["drop"]:
        flags.append(
            f"ниже вершины на {drop:.0f} м в радиусе {thresholds['radius']} м"
        )
    if metrics.in_pit:
        flags.append("локальный минимум DEM (дно долины или яма)")
    elif not metrics.on_summit:
        flags.append("не локальный максимум DEM в пределах 90 м")
    elif not metrics.on_summit_wide:
        flags.append("не локальный максимум DEM в пределах 150 м")

    ele = peak.ele
    mismatch = None
    if ele is not None and not math.isnan(metrics.dem):
        mismatch = ele - metrics.dem
        if abs(mismatch) >= thresholds["ele_mismatch"]:
            flags.append(f"ele={ele:.0f} расходится с DEM на {mismatch:+.0f} м")

    deep_drop = max(
        [value for value in (drop, deep) if not math.isnan(value)] or [0.0]
    )
    wrong_place = metrics.in_pit or (
        deep_drop >= thresholds["deep_drop"] and not metrics.on_summit_wide
    )
    if wrong_place:
        verdict = "error_named" if named else "error"
    elif mismatch is not None and abs(mismatch) >= thresholds["ele_mismatch"]:
        verdict = "mismatch"
    elif named:
        verdict = "named_suspect"
    elif not metrics.on_summit or deep_drop >= thresholds["drop"]:
        verdict = "suspect"
    else:
        verdict = "ok"
    return verdict, flags, mismatch


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
        "osm_id", "lon", "lat", "name", "named", "ele", "dem", "max3", "min3",
        "max5", "on_summit", "on_summit_wide", "in_pit", "verdict", "flags",
        "ele_minus_dem", "timestamp", "user", "changeset", "source", "file",
    ]
    for radius in radii:
        header.append(f"drop{radius}")
    lines = [",".join(header)]
    for peak, metrics, verdict, flags, mismatch in results:
        row = [
            peak.osm_id,
            f"{peak.lon:.7f}",
            f"{peak.lat:.7f}",
            peak.name.replace('"', '""'),
            "1" if peak.name else "0",
            "" if peak.ele is None else f"{peak.ele:.0f}",
            f"{metrics.dem:.0f}",
            f"{metrics.max3:.0f}",
            f"{metrics.min3:.0f}",
            f"{metrics.max5:.0f}",
            "1" if metrics.on_summit else "0",
            "1" if metrics.on_summit_wide else "0",
            "1" if metrics.in_pit else "0",
            verdict,
            "; ".join(flags).replace('"', '""'),
            "" if mismatch is None else f"{mismatch:.0f}",
            peak.timestamp,
            peak.user,
            str(peak.changeset),
            peak.tags.get("source", ""),
            peak.source_file,
        ]
        for radius in radii:
            value = metrics.drop_at(radius)
            row.append("" if math.isnan(value) else f"{value:.0f}")
        lines.append(",".join(row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    features = []
    for peak, metrics, verdict, flags, mismatch in results:
        if verdict == "ok":
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
                "name": peak.name,
                "ele": peak.ele,
                "dem": round(metrics.dem, 1),
                "max3": round(metrics.max3, 1),
                "min3": round(metrics.min3, 1),
                "max5": round(metrics.max5, 1),
                "on_summit": metrics.on_summit,
                "on_summit_wide": metrics.on_summit_wide,
                "in_pit": metrics.in_pit,
                "flags": "; ".join(flags),
                "ele_minus_dem": None if mismatch is None else round(mismatch, 1),
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
    named = [item for item in results if item[0].name]
    unnamed = [item for item in results if not item[0].name]
    errors = [item for item in results if item[2] == "error"]
    errors_named = [item for item in results if item[2] == "error_named"]
    suspects = [item for item in results if item[2] == "suspect"]
    named_suspects = [item for item in results if item[2] == "named_suspect"]
    mismatches = [item for item in results if item[2] == "mismatch"]
    without_dem = sum(1 for item in results if math.isnan(item[1].dem))

    lines = []
    lines.append("Проверка положения пиков OSM по рельефу SRTM")
    lines.append("=" * 62)
    lines.append(f"Всего пиков: {total}")
    if without_dem:
        lines.append(f"  без данных DEM (тайл не скачан): {without_dem}")
    lines.append(f"  с названием: {len(named)}   без названия: {len(unnamed)}")
    lines.append("")
    lines.append("Вердикты: " + ", ".join(
        f"{name}={count}" for name, count in sorted(verdicts.items())
    ))
    lines.append("")

    lines.append("Доля пиков, стоящих не на вершине")
    lines.append("-" * 62)
    lines.append("Без названия — основная группа: такие узлы чаще всего")
    lines.append("и есть ошибочные пики в долинах.")
    lines.append("")
    lines.append(f"{'группа':<34}{'пиков':>8}{'не на вершине':>16}")
    for label, group in (
        ("без названия", unnamed),
        ("с названием", named),
        ("все", results),
    ):
        wrong = sum(1 for item in group if item[2] in ("error", "error_named"))
        share = wrong / len(group) * 100 if group else 0
        lines.append(f"{label:<34}{len(group):>8}{share:>13.1f} %")
    lines.append("")
    lines.append(f"Ошибок без названия: {len(errors)}")
    lines.append(f"Ошибок с названием (стоит взглянуть): {len(errors_named)}")
    lines.append(f"Подозрительных без названия: {len(suspects)}")
    lines.append(f"Названных, не прошедших проверку (скорее всего погрешность "
                 f"рельефа): {len(named_suspects)}")
    lines.append(f"Расхождений ele с DEM: {len(mismatches)}")
    lines.append("")

    if errors:
        lines.append("Ошибки без названия: срез по источнику данных")
        lines.append("-" * 62)
        sources = Counter(
            item[0].tags.get("source", "(нет source)") for item in errors
        )
        for source, count in sources.most_common(10):
            lines.append(f"  {count:>5}  {source}")
        lines.append("")
        lines.append("Ошибки без названия: срез по дате правки")
        lines.append("-" * 62)
        dates = Counter(item[0].timestamp[:10] for item in errors)
        for date, count in dates.most_common(10):
            lines.append(f"  {count:>5}  {date}")
        lines.append("")

    if errors_named:
        lines.append("Ошибки с названием (список для выборочной проверки)")
        lines.append("-" * 62)
        for peak, metrics, _verdict, flags, _mismatch in errors_named[:20]:
            lines.append(
                f"  node/{peak.osm_id:<12} {peak.lat:8.4f} N {peak.lon:9.4f} E  "
                f"{peak.name[:28]:<28} DEM={metrics.dem:6.0f} м"
            )
        lines.append("")

    if errors:
        lines.append("Все ошибки: срез по стране (файл OSM)")
        lines.append("-" * 62)
        files = Counter(item[0].source_file for item in errors + errors_named)
        for source, count in files.most_common(10):
            lines.append(f"  {count:>5}  {source}")
        lines.append("")

    suspects_all = errors + errors_named + suspects
    if suspects_all:
        clusters = cluster_points(suspects_all)
        clusters.sort(key=len, reverse=True)
        lines.append(f"Кластеры подозрительных пиков (всего кластеров: {len(clusters)})")
        lines.append("-" * 62)
        for group in clusters[:15]:
            if len(group) < 2:
                continue
            lon = sum(item[0].lon for item in group) / len(group)
            lat = sum(item[0].lat for item in group) / len(group)
            users = Counter(item[0].user or "(без пользователя)" for item in group)
            dates = Counter(item[0].timestamp[:10] for item in group)
            lines.append(
                f"  {len(group):>4} пиков  {lat:8.4f} N  {lon:9.4f} E   "
                f"даты: {', '.join(f'{d}×{c}' for d, c in dates.most_common(3))}"
            )
            lines.append(f"        авторы: {', '.join(f'{u}×{c}' for u, c in users.most_common(3))}")
        lines.append("")

    lines.append("Самые явные ошибки (ниже вершины больше всего)")
    lines.append("-" * 62)
    ranked = sorted(
        errors + errors_named,
        key=lambda item: item[1].drop_at(thresholds["deep_radius"]),
        reverse=True,
    )
    for peak, metrics, _verdict, flags, _mismatch in ranked[:30]:
        drop = metrics.drop_at(thresholds["deep_radius"])
        lines.append(
            f"  node/{peak.osm_id:<12} {peak.lat:8.4f} N {peak.lon:9.4f} E  "
            f"DEM={metrics.dem:6.0f} м  ниже вершины на {drop:6.0f} м  "
            f"{peak.timestamp[:10]}  {peak.name[:20]:<20} {'; '.join(flags)}"
        )
    lines.append("")
    return "\n".join(lines)


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
        "--ele-mismatch", type=float, default=50.0,
        help="допустимое расхождение тега ele и DEM, м",
    )
    arguments = parser.parse_args(argv)

    natural_values = [item.strip() for item in arguments.natural.split(",") if item.strip()]
    thresholds = {
        "radius": arguments.radius,
        "deep_radius": arguments.deep_radius,
        "drop": arguments.drop,
        "deep_drop": arguments.deep_drop,
        "ele_mismatch": arguments.ele_mismatch,
    }
    radii = sorted({arguments.radius, arguments.deep_radius, 1500})

    osmium_bin = find_osmium(arguments.osmium)
    print(f"Чтение пиков из {len(arguments.pbf)} файла(ов)")
    peaks = read_peaks(arguments.pbf, natural_values, osmium_bin)
    print(f"Найдено пиков: {len(peaks)}")

    dem = DemTiles(arguments.dem_cache, download=not arguments.no_download)
    results = []
    for index, peak in enumerate(peaks, 1):
        metrics = measure(peak, dem, radii)
        verdict, flags, mismatch = classify(peak, metrics, thresholds)
        results.append((peak, metrics, verdict, flags, mismatch))
        if index % 1000 == 0:
            print(f"Проверено {index}/{len(peaks)}", flush=True)

    csv_path, geojson_path, report_path = write_reports(
        peaks, results, arguments.out, radii, thresholds
    )
    print(f"Отчёты: {csv_path}, {geojson_path}, {report_path}")
    print(build_report(peaks, results, radii, thresholds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
