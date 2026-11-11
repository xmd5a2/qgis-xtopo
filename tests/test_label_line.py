"""Линия подписи вдоль полигона: форма, гладкость, границы.

Рабочие данные пользователя в репозитории не лежат. Синтетические контуры
проверяются всегда, а случаи с листов проверки включаются переменными
окружения:

* ``QGIS_XTOPO_TEST_VECTOR`` — каталог ``vector`` проекта (wood, water,
  wetland, bay_polygon, glacier, strait);
* ``QGIS_XTOPO_TEST_GLACIER`` — ``glacier.geojson`` с ледниками Кавказа;
* ``QGIS_XTOPO_TEST_AUTOMAP`` — каталог ``vector`` проекта automap_test
  (швейцарские ледники, дубликат Feegletscher).
"""

import math
import os
import unittest

import numpy as np
from osgeo import ogr

from qgis_xtopo.core import label_line

ogr.UseExceptions()

METRES_PER_DEGREE = 111320.0
VECTOR_ENV = "QGIS_XTOPO_TEST_VECTOR"
GLACIER_ENV = "QGIS_XTOPO_TEST_GLACIER"
AUTOMAP_ENV = "QGIS_XTOPO_TEST_AUTOMAP"

CORNER_NAMES = {
    "tl": "левый верхний",
    "bl": "левый нижний",
    "tr": "правый верхний",
    "br": "правый нижний",
}


def _ring(points):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in points:
        ring.AddPoint_2D(x, y)
    ring.AddPoint_2D(points[0][0], points[0][1])
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


def banana(outer=0.02, inner=0.012):
    """Дуга в нижней половине круга: изогнутая «банановая» фигура."""
    points = []
    for index in range(25):
        angle = math.radians(180 + 180 * index / 24)
        points.append((outer * math.cos(angle), outer * math.sin(angle)))
    for index in range(24, -1, -1):
        angle = math.radians(180 + 180 * index / 24)
        points.append((inner * math.cos(angle), inner * math.sin(angle)))
    return _ring(points)


def bar_with_arm():
    """Длинная горизонтальная полоса с боковым отрогом вниз."""
    return _ring([
        (-0.03, 0.0), (0.03, 0.0), (0.03, 0.004), (-0.012, 0.004),
        (-0.012, -0.02), (-0.018, -0.02), (-0.018, 0.004), (-0.03, 0.004),
    ])


def multipolygon_with_thin_part():
    """Большой компактный контур и длинная тонкая часть рядом."""
    square = _ring([
        (0.0, 0.0), (0.02, 0.0), (0.02, 0.02), (0.0, 0.02),
    ])
    thin = _ring([
        (0.03, 0.005), (0.08, 0.005), (0.08, 0.007), (0.03, 0.007),
    ])
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    multi.AddGeometry(square)
    multi.AddGeometry(thin)
    return multi, square


def curved_band(bulge=0.02):
    """Дуга с широкой нижней лопастью, как у Груэбу."""
    top = []
    bottom = []
    for index in range(61):
        x = -0.03 + 0.06 * index / 60
        y_top = 0.012 - 10 * x * x
        width = 0.006 + bulge * math.exp(-(x / 0.008) ** 2)
        top.append((x, y_top))
        bottom.append((x, y_top - width))
    return _ring(top + bottom[::-1])


def _dense(line, step=0.0001):
    points = np.asarray(line, dtype="float64")
    result = [points[0]]
    for index in range(1, len(points)):
        length = float(np.hypot(*(points[index] - points[index - 1])))
        pieces = max(1, int(math.ceil(length / step)))
        for piece in range(1, pieces + 1):
            result.append(points[index - 1]
                          + (points[index] - points[index - 1])
                          * (piece / pieces))
    return np.asarray(result)


def _max_turn(line):
    points = np.asarray(line, dtype="float64")
    if len(points) < 3:
        return 0.0
    first = points[1:-1] - points[:-2]
    second = points[2:] - points[1:-1]
    norms = (np.linalg.norm(first, axis=1)
             * np.linalg.norm(second, axis=1))
    cosine = np.divide((first * second).sum(axis=1), norms,
                       out=np.ones(len(first)), where=norms > 0)
    angles = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
    return float(angles.max())


class LabelLineShapeTest(unittest.TestCase):
    def test_banana_follows_the_curve(self):
        geometry = banana()
        line = label_line.label_line(geometry)
        self.assertGreaterEqual(len(line), 2)
        dense = _dense(line)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in dense:
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) > 1e-5:
                outside += 1
        self.assertLess(outside / len(dense), 0.02)
        # середина линии уходит вниз, как и сама дуга, а не идёт по хорде
        middle = np.asarray(line)[len(line) // 2]
        self.assertLess(middle[1], -0.005)

    def test_line_is_smooth(self):
        line = label_line.label_line(banana())
        self.assertLess(_max_turn(line), 45.0)

    def test_line_starts_on_the_left(self):
        line = label_line.label_line(banana())
        self.assertLess(line[0][0], line[-1][0])

    def test_bar_line_stays_in_the_bar(self):
        geometry = bar_with_arm()
        line = label_line.label_line(geometry)
        self.assertGreaterEqual(len(line), 2)
        probe = ogr.Geometry(ogr.wkbPoint)
        for x, y in _dense(line):
            probe.SetPoint_2D(0, x, y)
            self.assertLess(geometry.Distance(probe), 1e-5)

    def test_main_flow_keeps_the_bar(self):
        geometry = bar_with_arm()
        line = label_line.label_line(geometry, main_flow=True)
        self.assertGreaterEqual(len(line), 2)
        # отрог вниз не должен уводить подпись из полосы
        self.assertGreater(min(point[1] for point in line), -0.006)

    def test_multipolygon_uses_the_largest_part(self):
        geometry, largest = multipolygon_with_thin_part()
        line = label_line.label_line(geometry)
        self.assertGreaterEqual(len(line), 2)
        probe = ogr.Geometry(ogr.wkbPoint)
        for x, y in line:
            probe.SetPoint_2D(0, x, y)
            # тонкая часть лежит правее 0.03: линия туда попадать не должна
            self.assertLess(largest.Distance(probe), 1e-6)

    def test_curved_band_stays_inside(self):
        geometry = curved_band()
        line = label_line.label_line(geometry, main_flow=True)
        self.assertGreaterEqual(len(line), 2)
        dense = _dense(line)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in dense:
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) > 1e-6:
                outside += 1
        self.assertLess(outside / len(dense), 0.02)
        # линия заходит в нижнюю лопасть, а не идёт по верхней хорде
        self.assertLess(min(point[1] for point in line), -0.001)

    def test_degenerate_geometry_gives_no_line(self):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001)):
            ring.AddPoint_2D(x, y)
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        # слишком мелкий объект: линия либо есть, либо пустая, но без ошибок
        line = label_line.label_line(polygon)
        self.assertIsInstance(line, list)


# Случаи с листов проверки: номер, слой, индекс по убыванию площади и нормы.
MARKED_CASES = [
    # первый лист
    (1, "wood", 0, {"outside": 0.12, "offset": 1200}),
    (2, "wood", 1, {"outside": 0.12, "offset": 800}),
    (3, "wood", 2, {"outside": 0.05, "offset": 800}),
    (4, "wood", 3, {"outside": 0.12, "offset": 1400}),
    (5, "wood", 8, {"outside": 0.15, "offset": 1800}),
    (6, "wood", 15, {"outside": 0.05, "offset": 500}),
    (7, "wood", 30, {"outside": 0.05, "offset": 350}),
    (8, "wood", 60, {"outside": 0.05, "offset": 300}),
    (9, "water", 0, {"outside": 0.05, "offset": 900}),
    (10, "water", 1, {"outside": 0.05, "offset": 400}),
    (11, "water", 2, {"outside": 0.05, "offset": 300}),
    (12, "water", 5, {"outside": 0.05, "offset": 300}),
    (13, "water", 12, {"outside": 0.05, "offset": 300}),
    (14, "water", 40, {"outside": 0.05, "offset": 150}),
    (15, "wetland", 0, {"outside": 0.05, "offset": 250}),
    (16, "wetland", 1, {"outside": 0.05, "offset": 300}),
    (17, "wetland", 5, {"outside": 0.05, "offset": 150}),
    (18, "bay_polygon", 0, {"outside": 0.05, "offset": 3000}),
    (19, "bay_polygon", 1, {"outside": 0.05, "offset": 1600}),
    (20, "glacier", 0, {"outside": 0.05, "offset": 200}),
    (21, "strait", 0, {"outside": 0.05, "offset": 150}),
    (22, "strait", 1, {"outside": 0.05, "offset": 250}),
    (23, "strait", 5, {"outside": 0.05, "offset": 150}),
    # второй лист
    (101, "wood", 5, {"outside": 0.10, "offset": 700}),
    (102, "wood", 10, {"outside": 0.05, "offset": 700}),
    (103, "wood", 20, {"outside": 0.05, "offset": 1000}),
    (104, "wood", 25, {"outside": 0.05, "offset": 700}),
    (105, "wood", 40, {"outside": 0.10, "offset": 400}),
    (106, "wood", 50, {"outside": 0.05, "offset": 150}),
    (107, "wood", 70, {"outside": 0.05, "offset": 350}),
    (108, "wood", 100, {"outside": 0.05, "offset": 300}),
    (109, "wood", 200, {"outside": 0.05, "offset": 200}),
    (110, "wood", 400, {"outside": 0.05, "offset": 150, "clearance": 10}),
    (111, "water", 3, {"outside": 0.05, "offset": 150}),
    (112, "water", 7, {"outside": 0.05, "offset": 120, "clearance": 10}),
    (113, "water", 10, {"outside": 0.05, "offset": 200}),
    (114, "water", 20, {"outside": 0.05, "offset": 200}),
    (115, "water", 30, {"outside": 0.05, "offset": 150}),
    (116, "water", 50, {"outside": 0.05, "offset": 150, "start": "bl"}),
    (117, "water", 80, {"outside": 0.05, "offset": 100}),
    (118, "water", 120, {"outside": 0.05, "offset": 100}),
    (119, "wetland", 2, {"outside": 0.05, "offset": 700}),
    (120, "wetland", 8, {"outside": 0.05, "offset": 250}),
    (121, "wetland", 15, {"outside": 0.05, "offset": 150}),
    (122, "wetland", 25, {"outside": 0.05, "offset": 150}),
    (123, "strait", 3, {"outside": 0.05, "offset": 150}),
    (124, "strait", 7, {"outside": 0.05, "offset": 150, "clearance": 15}),
    (125, "water", 200, {"outside": 0.05, "offset": 80}),
    # лист 3: 10 объектов, отмечены пользователем как верные
    (201, "wood", 12, {"outside": 0.15, "offset": 650}),
    (202, "wood", 35, {"outside": 0.05, "offset": 300}),
    (203, "wood", 45, {"outside": 0.07, "offset": 300}),
    (204, "wood", 75, {"outside": 0.07, "offset": 350}),
    (205, "wood", 150, {"outside": 0.05, "offset": 300}),
    (206, "water", 4, {"outside": 0.05, "offset": 550}),
    (207, "water", 9, {"outside": 0.05, "offset": 100}),
    (208, "water", 60, {"outside": 0.05, "offset": 100}),
    (209, "wetland", 3, {"outside": 0.05, "offset": 200}),
    (210, "strait", 2, {"outside": 0.05, "offset": 200}),
    # ледники Кавказа, отмечены как верные
    (301, "glacier_caucasus", 0,
     {"outside": 0.05, "offset": 2100, "main_flow": True}),
    (302, "glacier_caucasus", 1,
     {"outside": 0.08, "offset": 2700, "main_flow": True, "start": "br"}),
    (303, "glacier_caucasus", 2,
     {"outside": 0.05, "offset": 1600, "main_flow": True}),
    (304, "glacier_caucasus", 3,
     {"outside": 0.05, "offset": 850, "main_flow": True}),
    (305, "glacier_caucasus", 4,
     {"outside": 0.13, "offset": 600, "main_flow": True}),
    (306, "glacier_caucasus", 5,
     {"outside": 0.05, "offset": 650, "main_flow": True}),
    (307, "glacier_caucasus", 6,
     {"outside": 0.05, "offset": 200, "main_flow": True}),
    (308, "glacier_caucasus", 7,
     {"outside": 0.05, "offset": 350, "main_flow": True}),
    (309, "glacier_caucasus", 8,
     {"outside": 0.06, "offset": 550, "main_flow": True, "start": "bl"}),
    (310, "glacier_caucasus", 9,
     {"outside": 0.05, "offset": 350, "main_flow": True}),
]

GLACIER_NAMES = [
    "Дыхсу", "Безенги", "ледник Цанери", "Крумкол", "Цаннер",
    "ледник Шхельда", "ледник Терскол", "Лайла", "ледник Башкара",
    "Джанкуат",
]


def _marked_geometry(layer_name, index):
    """Объект из рабочего слоя; данные читаются только при запуске теста."""
    if layer_name == "glacier_caucasus":
        return _marked_glaciers()[index]
    path = os.path.join(os.environ[VECTOR_ENV], f"{layer_name}.sqlite")
    dataset = ogr.Open(path)
    if dataset is None:
        raise FileNotFoundError(path)
    layer = dataset.GetLayer(0)
    items = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None and not geometry.IsEmpty():
            items.append(geometry.Clone())
    items.sort(key=lambda geometry: -geometry.GetArea())
    return items[index]


def _marked_glaciers():
    path = os.environ[GLACIER_ENV]
    dataset = ogr.Open(path)
    if dataset is None:
        raise FileNotFoundError(path)
    by_name = {}
    for feature in dataset.GetLayer(0):
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.GetGeometryName() not in (
                "POLYGON", "MULTIPOLYGON"):
            continue
        for field in ("name:ru", "name", "name:en"):
            value = feature.GetField(field)
            if value and value not in by_name:
                by_name[value] = geometry.Clone()
                break
    return [by_name[name] for name in GLACIER_NAMES if name in by_name]


def _check_marked_case(case):
    """Проверяет один отмеченный случай; выполняется в отдельном процессе."""
    number, layer_name, index, rules = case
    geometry = _marked_geometry(layer_name, index)
    line = label_line.label_line(
        geometry, main_flow=rules.get("main_flow", False)
    )
    if len(line) < 2:
        return number, [f"№{number}: линии нет"]
    points = np.asarray(line, dtype="float64")
    dense = _dense(line, step=0.00003)
    probe = ogr.Geometry(ogr.wkbPoint)

    outside = 0
    for x, y in dense:
        probe.SetPoint_2D(0, x, y)
        if geometry.Distance(probe) * METRES_PER_DEGREE > 15.0:
            outside += 1
    share = outside / len(dense)
    failures = []
    if share > rules.get("outside", 0.05) + 1e-9:
        failures.append(
            f"№{number}: вне контура {share * 100:.0f}% "
            f"(норма {rules['outside'] * 100:.0f}%)"
        )

    # У мультиполигона подпись ставится на самую большую часть, поэтому
    # смещение считаем от её центра, а не от центра всех частей.
    if geometry.GetGeometryName() == "MULTIPOLYGON":
        parts = [geometry.GetGeometryRef(i)
                 for i in range(geometry.GetGeometryCount())]
        centre = max(parts, key=lambda part: part.GetArea()).Centroid()
    else:
        centre = geometry.Centroid()
    middle = points[len(points) // 2]
    offset = float(np.hypot(middle[0] - centre.GetX(),
                            middle[1] - centre.GetY()))
    offset_m = offset * METRES_PER_DEGREE * max(
        0.01, np.cos(np.radians(centre.GetY()))
    )
    if "offset" in rules and offset_m > rules["offset"]:
        failures.append(
            f"№{number}: смещение от центра {offset_m:.0f} м "
            f"(норма {rules['offset']} м)"
        )

    if "clearance" in rules:
        boundary = geometry.GetBoundary()
        half = len(points) // 4
        values = []
        for x, y in points[half:len(points) - half or None]:
            probe.SetPoint_2D(0, x, y)
            values.append(boundary.Distance(probe) * METRES_PER_DEGREE)
        low = min(values) if values else 0.0
        if low < rules["clearance"]:
            failures.append(
                f"№{number}: зазор до контура {low:.0f} м "
                f"(норма {rules['clearance']} м)"
            )

    x0, x1, y0, y1 = geometry.GetEnvelope()

    def corner_of(point):
        return ("t" if point[1] > (y0 + y1) / 2 else "b") + (
            "r" if point[0] > (x0 + x1) / 2 else "l"
        )

    if "diagonal" in rules:
        ends = {corner_of(points[0]), corner_of(points[-1])}
        wanted = set(rules["diagonal"].split("-"))
        if ends != wanted:
            failures.append(
                f"№{number}: концы линии в {sorted(ends)}, "
                f"нужны {sorted(wanted)}"
            )

    if "start" in rules:
        actual = corner_of(points[0])
        if actual != rules["start"]:
            failures.append(
                f"№{number}: начало линии в {CORNER_NAMES[actual]} углу "
                f"(нужен {CORNER_NAMES[rules['start']]})"
            )
    return number, failures


@unittest.skipUnless(
    os.environ.get(VECTOR_ENV) and os.environ.get(GLACIER_ENV),
    "нужны QGIS_XTOPO_TEST_VECTOR и QGIS_XTOPO_TEST_GLACIER",
)
class MarkedCasesTest(unittest.TestCase):
    """Случаи, отмеченные пользователем на листах проверки."""

    def test_marked_cases(self):
        from concurrent.futures import ProcessPoolExecutor

        workers = min(8, len(MARKED_CASES))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_check_marked_case, MARKED_CASES))
        failures = []
        for number, problems in sorted(results):
            failures.extend(problems)
        self.assertEqual(failures, [], "\n".join(failures))


@unittest.skipUnless(
    os.environ.get(AUTOMAP_ENV),
    "нужен QGIS_XTOPO_TEST_AUTOMAP с каталогом vector проекта",
)
class AutomapCasesTest(unittest.TestCase):
    """Швейцарские ледники из проекта automap_test."""

    def _load(self, layer_name, oid):
        path = os.path.join(os.environ[AUTOMAP_ENV], f"{layer_name}.sqlite")
        dataset = ogr.Open(path)
        self.assertIsNotNone(dataset, path)
        layer = dataset.GetLayer(0)
        for feature in layer:
            if feature.GetField("@id") == oid:
                return feature.GetGeometryRef().Clone()
        self.fail(f"{oid} нет в {layer_name}")

    def test_gruebu_line_is_lower_than_grass(self):
        geometry = self._load("glacier", "relation/14862257")
        old = self._load("glacier_names", "relation/14862257")
        line = label_line.label_line(geometry, main_flow=True)
        self.assertGreaterEqual(len(line), 2)

        dense = _dense(line, step=0.00003)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in dense:
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) * METRES_PER_DEGREE > 15.0:
                outside += 1
        self.assertLess(outside / len(dense), 0.05)

        old_points = []
        for index in range(old.GetGeometryCount()):
            part = old.GetGeometryRef(index)
            old_points += [part.GetPoint_2D(i)
                           for i in range(part.GetPointCount())]
        old_min = min(point[1] for point in old_points)
        new_min = min(point[1] for point in line)
        # новая линия должна уходить в нижнюю лопасть заметно ниже старой
        self.assertLess(new_min, old_min - 0.0005)

    def test_allalingletscher_follows_upper_branch(self):
        geometry = self._load("glacier", "relation/77250")
        old = self._load("glacier_names", "relation/77250")
        line = label_line.label_line(geometry, main_flow=True)
        self.assertGreaterEqual(len(line), 2)

        dense = _dense(line, step=0.00003)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in dense:
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) * METRES_PER_DEGREE > 15.0:
                outside += 1
        self.assertLess(outside / len(dense), 0.05)

        old_points = []
        for index in range(old.GetGeometryCount()):
            part = old.GetGeometryRef(index)
            old_points += [part.GetPoint_2D(i)
                           for i in range(part.GetPointCount())]
        old_points = np.asarray(old_points)

        def y_at(points, x):
            order = np.argsort(points[:, 0])
            points = points[order]
            for index in range(1, len(points)):
                if points[index - 1, 0] <= x <= points[index, 0]:
                    span = points[index, 0] - points[index - 1, 0]
                    share = (x - points[index - 1, 0]) / (span or 1e-12)
                    return points[index - 1, 1] + share * (
                        points[index, 1] - points[index - 1, 1])
            return None

        new_y = y_at(np.asarray(line), 7.906)
        old_y = y_at(old_points, 7.906)
        self.assertIsNotNone(new_y)
        self.assertIsNotNone(old_y)
        # главная ветка идёт выше старой GRASS-линии и надписи 3346 м
        self.assertGreater(new_y, old_y + 0.005)
        self.assertGreater(new_y, 46.035)

    def test_stelligletscher_runs_to_upper_right_corner(self):
        geometry = self._load("glacier", "way/34561345")
        line = label_line.label_line(geometry, main_flow=True)
        self.assertGreaterEqual(len(line), 2)

        dense = _dense(line, step=0.00003)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in dense:
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) * METRES_PER_DEGREE > 15.0:
                outside += 1
        self.assertLess(outside / len(dense), 0.05)

        points = np.asarray(line)
        # старт — у нижнего края основной лопасти, а не на левом языке
        self.assertLess(points[0][1], 46.1515)
        self.assertGreater(points[0][0], 7.742)
        # конец — в правом верхнем углу ледника
        self.assertGreater(points[-1][0], 7.748)
        self.assertGreater(points[-1][1], 46.1595)
        # линия проходит вдоль всего ледника, а не срезает его по прямой
        self.assertGreater(float(np.sum(np.hypot(*np.diff(points, axis=0).T))),
                           0.014)


if __name__ == "__main__":
    unittest.main()
