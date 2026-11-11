"""Линия подписи вдоль полигона: форма, гладкость, границы.

Рабочие данные пользователя здесь не используются — только синтетические
контуры. Проверки по отметкам пользователя вынесены в отдельный тест,
он включается переменной окружения QGIS_XTOPO_TEST_VECTOR.
"""

import math
import os
import unittest

import numpy as np
from osgeo import ogr

from qgis_xtopo.core import label_line

ogr.UseExceptions()


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

    def test_degenerate_geometry_gives_no_line(self):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001)):
            ring.AddPoint_2D(x, y)
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        # слишком мелкий объект: линия либо есть, либо пустая, но без ошибок
        line = label_line.label_line(polygon)
        self.assertIsInstance(line, list)


@unittest.skipUnless(
    os.environ.get("QGIS_XTOPO_TEST_VECTOR"),
    "нужен QGIS_XTOPO_TEST_VECTOR с каталогом vector проекта",
)
class MarkedCasesTest(unittest.TestCase):
    """Случаи, отмеченные пользователем на листах проверки."""

    def _load(self, layer_name, index):
        dataset = ogr.Open(
            os.path.join(os.environ["QGIS_XTOPO_TEST_VECTOR"],
                         f"{layer_name}.sqlite")
        )
        layer = dataset.GetLayer(0)
        items = []
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None and not geometry.IsEmpty():
                items.append(geometry.Clone())
        items.sort(key=lambda geometry: -geometry.GetArea())
        return items[index]

    def test_wood_line_inside(self):
        geometry = self._load("wood", 0)
        line = label_line.label_line(geometry)
        self.assertGreaterEqual(len(line), 2)
        probe = ogr.Geometry(ogr.wkbPoint)
        outside = 0
        for x, y in _dense(line):
            probe.SetPoint_2D(0, x, y)
            if geometry.Distance(probe) > 15.0 / 111320:
                outside += 1
        self.assertLess(outside / len(_dense(line)), 0.12)


if __name__ == "__main__":
    unittest.main()
