"""Сверка: что попадает в проект против исходных данных OSM.

Инструмент проверяет, что подготовка проекта не теряет объекты.
Эталон: osmium вырезает область (strategy=smart, запас 0.3° — как у
плагина) и выгружает её в GeoJSON. Каждый объект раскладывается по
слоям тем же движком запросов плагина (QueryIndex); в область берутся
объекты, геометрия которых пересекает bbox.
Плагин: те же объекты, но прочитанные драйвером GDAL с грубым
фильтром. Сравниваются множества @id по каждому слою.

Запуск (нужны osmium и выгрузки Geofabrik в кэше):

    QT_QPA_PLATFORM=offscreen python3 tools/check_pbf_coverage.py

Список участков задаётся в CASES: имя, файлы выгрузок, юго-западный
угол квадрата 10x10 км.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import register_package, qgis_app  # noqa: E402

register_package()
qgis_app()

from qgis_xtopo.core import pbf_source  # noqa: E402
from qgis_xtopo.core.bbox import parse_bbox  # noqa: E402
from qgis_xtopo.core.config import Config  # noqa: E402
from qgis_xtopo.core.resources import read_query  # noqa: E402

CACHE = os.path.expanduser("~/qgis_projects/qgisxtopo-cache/geofabrik")
OSMIUM = os.path.expanduser(
    "~/git/qgis-xtopo/vendor/osmium/linux-x86_64/osmium"
)
MARGIN = 0.3

CASES = (
    # Разные континенты, острова и границы выгрузок.
    ("Армения, Ереван (Азия)", ("armenia.osm.pbf",), 44.44, 40.13),
    ("Азербайджан, Баку (Кавказ)", ("azerbaijan.osm.pbf",), 49.80, 40.36),
    ("Швейцария, Церматт (Альпы)", ("switzerland.osm.pbf",), 7.75, 46.02),
    ("Юкон, Уайтхорс (Сев. Америка)", ("yukon.osm.pbf",), -135.25, 60.05),
    ("Сев. Калифорния, Напа (Сев. Америка)", ("norcal.osm.pbf",), -122.42, 38.44),
    ("Аляска, о. Кодьяк (остров)", ("us/alaska.osm.pbf",), -153.55, 57.40),
    ("Аляска, Фэрбанкс (субарктика)", ("us/alaska.osm.pbf",), -147.80, 64.83),
    ("Грузия, Тбилиси (равнина)", ("georgia.osm.pbf",), 44.78, 41.72),
    ("Северный Кавказ, Пятигорск (горы)", ("north-caucasus-fed-district.osm.pbf",), 43.05, 43.98),
    ("Франш-Конте, Юра (Альпы)", ("franche-comte.osm.pbf",), 6.60, 47.00),
    ("Саксония, Лейпциг", ("sachsen.osm.pbf",), 12.30, 51.28),
    ("Аляска, Анкоридж", ("us/alaska.osm.pbf",), -149.90, 61.15),
    ("Корсика, остров (побережье)", ("corse.osm.pbf",), 8.60, 41.90),
    (
        "Граница Грузии и Северного Кавказа (две выгрузки)",
        ("georgia.osm.pbf", "north-caucasus-fed-district.osm.pbf"),
        44.70, 41.35,
    ),
    ("Малопольша, Краков", ("malopolskie.osm.pbf",), 21.10, 50.15),
)


def square(lon, lat, size_km=10.0):
    import math
    dlat = size_km / 111.32
    dlon = size_km / (111.32 * max(0.05, math.cos(math.radians(lat))))
    return lon, lat, lon + dlon, lat + dlat


def point_in_rect(x, y, rect):
    left, bottom, right, top = rect
    return left <= x <= right and bottom <= y <= top


def segment_hits_rect(a, b, rect):
    """Пересекает ли отрезок прямоугольник."""
    left, bottom, right, top = rect
    if point_in_rect(a[0], a[1], rect) or point_in_rect(b[0], b[1], rect):
        return True
    # Пересечение с четырьмя сторонами прямоугольника.
    corners = (
        (left, bottom), (right, bottom), (right, top), (left, top),
    )
    for index in range(4):
        c = corners[index]
        d = corners[(index + 1) % 4]
        if _segments_cross(a, b, c, d):
            return True
    return False


def _orient(a, b, c):
    value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else 2


def _segments_cross(a, b, c, d):
    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    def on(p, q, r):
        return (
            min(p[0], q[0]) - 1e-12 <= r[0] <= max(p[0], q[0]) + 1e-12
            and min(p[1], q[1]) - 1e-12 <= r[1] <= max(p[1], q[1]) + 1e-12
        )
    if o1 == 0 and on(a, b, c):
        return True
    if o2 == 0 and on(a, b, d):
        return True
    if o3 == 0 and on(c, d, a):
        return True
    if o4 == 0 and on(c, d, b):
        return True
    return False


def hits_rect(geometry, rect):
    kind = geometry.get("type")
    if kind == "Point":
        return point_in_rect(geometry["coordinates"][0], geometry["coordinates"][1], rect)
    if kind == "LineString":
        points = geometry["coordinates"]
        for index in range(len(points) - 1):
            if segment_hits_rect(points[index], points[index + 1], rect):
                return True
        return False
    if kind == "Polygon":
        rings = geometry["coordinates"]
    elif kind == "MultiPolygon":
        rings = [ring for polygon in geometry["coordinates"] for ring in polygon]
    else:
        return False
    for ring in rings:
        for index in range(len(ring) - 1):
            if segment_hits_rect(ring[index], ring[index + 1], rect):
                return True
    return False


def osmium_objects(sources, rect, index, directory):
    """Множества @id по слоям: что есть в исходных данных."""
    left, bottom, right, top = rect
    area = f"{left - MARGIN},{bottom - MARGIN},{right + MARGIN},{top + MARGIN}"
    result = {}
    for source in sources:
        extract = os.path.join(directory, "extract.osm.pbf")
        subprocess.run(
            [
                OSMIUM, "extract", "-b", area, "--strategy=smart",
                "-S", "types=any", "-o", extract, "--overwrite", source,
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        geojson = os.path.join(directory, "extract.geojson")
        subprocess.run(
            [
                OSMIUM, "export", "-f", "geojson",
                "--add-unique-id=type_id", "-o", geojson, "--overwrite", extract,
            ],
            check=True, stdout=subprocess.DEVNULL,
        )
        data = json.loads(open(geojson, encoding="utf-8").read())
        for feature in data["features"]:
            tags = feature.get("properties", {}) or {}
            identifier = str(feature.get("id", ""))
            if not identifier:
                continue
            if identifier[0] == "n":
                kind = "node"
            elif identifier[0] == "w":
                kind = "way"
            elif identifier[0] == "r":
                kind = "relation"
            else:
                continue
            geometry = feature.get("geometry") or {}
            if not hits_rect(geometry, rect):
                continue
            properties = {
                key: value for key, value in tags.items()
                if not key.startswith("@")
            }
            normalized = {
                "n": "node", "w": "way", "r": "relation",
            }[identifier[0]] + "/" + identifier[1:]
            properties["@id"] = normalized
            properties["@type"] = kind
            for name in index.matching_names(kind, properties):
                result.setdefault(name, set()).add(normalized)
    return result


def plugin_objects(dataset):
    result = {}
    for name, path in dataset.feature_files.items():
        ids = set()
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                identifier = json.loads(line)["properties"].get("@id")
                if identifier:
                    ids.add(identifier)
        result[name] = ids
    return result


QUERIES = {}
config = Config.from_file(
    os.path.expanduser("~/qgis_projects/qgisxtopo-config/config.ini")
)
names = list(config.array_queries)
for name in names:
    QUERIES[name] = read_query(name)

parser = argparse.ArgumentParser()
parser.add_argument("--case", type=int, default=None)
options = parser.parse_args()
selected = CASES if options.case is None else (CASES[options.case],)

with tempfile.TemporaryDirectory() as directory:
    for title, filenames, lon, lat in selected:
        sources = [os.path.join(CACHE, name) for name in filenames]
        missing = [item for item in sources if not os.path.exists(item)]
        if missing:
            print(f"{title}: нет выгрузок {missing}")
            continue
        box = square(lon, lat)
        rect = (box[0], box[1], box[2], box[3])
        bbox_text = f"{box[0]:.4f},{box[1]:.4f},{box[2]:.4f},{box[3]:.4f}"
        bbox = parse_bbox(bbox_text)
        index = pbf_source.QueryIndex(
            {name: QUERIES[name] for name in names}
        )
        started = time.monotonic()
        truth = osmium_objects(sources, rect, index, directory)
        truth_time = time.monotonic() - started

        cfg = Config(
            qgis_projects_dir=directory, project_name="check",
            bbox=bbox_text, array_queries=names,
            source_mode="pbf", pbf_path=sources[0],
        )
        cfg.ensure_directories()
        started = time.monotonic()
        dataset = pbf_source.prepare_dataset(
            cfg, bbox, names,
            query_texts={name: QUERIES[name] for name in names},
            sources=sources, progress=None,
        )
        plugin = plugin_objects(dataset)
        plugin_time = time.monotonic() - started
        dataset.close()

        print("=" * 72)
        print(title)
        print(
            f"  эталон (osmium): {truth_time:.0f} с, плагин: {plugin_time:.0f} с"
        )
        truth_total = sum(len(value) for value in truth.values())
        plugin_total = sum(len(value) for value in plugin.values())
        print(f"  объектов: эталон {truth_total}, плагин {plugin_total}")
        problems = []
        for name in names:
            expected = truth.get(name, set())
            actual = plugin.get(name, set())
            lost = expected - actual
            extra = actual - expected
            if lost or extra:
                problems.append((name, len(lost), len(extra), sorted(lost)[:3]))
        if not problems:
            print("  расхождений нет: множества объектов совпадают")
        for name, lost, extra, sample in problems:
            print(
                f"  {name}: потеряно {lost}, лишних {extra}"
                + (f" (например {sample})" if sample else "")
            )
        sys.stdout.flush()
