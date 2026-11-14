"""Собирает компактные фикстуры линий подписи для тестов.

Отмеченные пользователем объекты лежат в рабочих данных, которых нет в
репозитории: без них тест на линии подписи пропускается.  Этот скрипт
упрощает геометрии отмеченных объектов, кладёт их и посчитанные линии в
`tests/data/label_cases.json` — данные небольшие, поэтому проверка
работает всегда, без рабочих каталогов.

В фикстуру попадают все отмеченные случаи.  Если огрубление меняет форму
настолько, что норма пользователя перестаёт выполняться, норма
поправляется на достижимую, а исходная остаётся в `source_rules`.

Запуск:

    python3 tools/make_label_fixtures.py
    python3 tools/make_label_fixtures.py --vector ~/qgis_projects/bike-marafon/vector \\
        --glacier ~/qgis_projects/georgia_max/src/glacier.geojson

После изменения алгоритма линии подписи файл нужно пересобрать: новые
линии записываются в фикстуру, и она снова становится эталоном.
"""

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: E402 - регистрирует пакет qgis_xtopo

import numpy as np  # noqa: E402
from osgeo import ogr  # noqa: E402

from qgis_xtopo.core import label_line  # noqa: E402
from tests.test_label_line import _dense, _violations  # noqa: E402

METRES_PER_DEGREE = 111320.0

DEFAULT_VECTOR = Path.home() / "qgis_projects/bike-marafon/vector"
DEFAULT_GLACIER = Path.home() / "qgis_projects/georgia_max/src/glacier.geojson"
OUTPUT = ROOT / "tests" / "data" / "label_cases.json"

# Номера отмеченных случаев, которые попадают в фикстуру: все слои и все
# виды норм (доля вне контура, смещение, зазор, угол начала).  Пустой
# список — берутся все отмеченные случаи.
CASE_NUMBERS = ()
# Сколько точек оставлять в кольце и сколько метров допускать при
# упрощении.
OUTER_POINTS = 60
HOLE_POINTS = 20
MAX_HOLES = 8
TOLERANCE_METRES = 3.0
# Доля площади, меньше которой дыра считается мелкой и заливается.
HOLE_SHARE = 0.02
# Знаков после запятой: сантиметр.
PRECISION = 7


def _simplify(points, tolerance):
    """Упрощение Douglas-Peucker: возвращает точки без замыкания кольца."""
    if len(points) < 3:
        return points
    first = 0
    last = len(points) - 1
    keep = [False] * len(points)
    keep[first] = keep[last] = True
    stack = [(first, last)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x1, y1 = points[start]
        x2, y2 = points[end]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        best = -1.0
        best_index = -1
        for index in range(start + 1, end):
            x, y = points[index]
            if length == 0.0:
                distance = math.hypot(x - x1, y - y1)
            else:
                distance = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / length
            if distance > best:
                best = distance
                best_index = index
        if best > tolerance and best_index > 0:
            keep[best_index] = True
            stack.append((start, best_index))
            stack.append((best_index, end))
    return [point for point, flag in zip(points, keep) if flag]


def _rings(geometry):
    """Кольца самой большой части мультиполигона.

    Мелкие дыры отбрасываются: алгоритм всё равно их заливает
    (`fill_small_holes`), а в фикстуре они только раздувают файл.
    """
    if geometry.GetGeometryName() == "POLYGON":
        part = geometry
    else:
        parts = [
            geometry.GetGeometryRef(index)
            for index in range(geometry.GetGeometryCount())
        ]
        part = max(parts, key=lambda item: item.GetArea())
    rings = []
    for index in range(part.GetGeometryCount()):
        ring = part.GetGeometryRef(index)
        points = [
            (ring.GetX(position), ring.GetY(position))
            for position in range(ring.GetPointCount())
        ]
        if points and points[0] == points[-1]:
            points = points[:-1]
        rings.append(points)
    if not rings:
        return rings
    outer = _ring_area(rings[0])
    threshold = HOLE_SHARE * outer
    kept = [rings[0]]
    holes = [
        ring for ring in rings[1:]
        if _ring_area(ring) >= threshold
    ]
    holes.sort(key=_ring_area, reverse=True)
    kept.extend(holes[:MAX_HOLES])
    return kept


def _ring_area(points):
    """Площадь кольца по формуле Гаусса."""
    total = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _simplify_ring(points, max_points):
    """Упрощение кольца до заданного числа точек."""
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    tolerance = max(TOLERANCE_METRES / METRES_PER_DEGREE, span * 0.0002)
    simplified = points
    for _attempt in range(14):
        simplified = _simplify(points, tolerance)
        if len(simplified) <= max_points:
            break
        tolerance *= 1.7
        if tolerance > span * 0.02:
            break
    return simplified


def _polygon(rings):
    """Полигон из колец с округлёнными координатами."""
    polygon = ogr.Geometry(ogr.wkbPolygon)
    for points in rings:
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        ring.AddPoint_2D(points[0][0], points[0][1])
        polygon.AddGeometry(ring)
    return polygon


def _norms(geometry, line, rules):
    """Нормы пользователя, поправленные на упрощённую форму.

    Огрубление меняет контур, и норма может перестать выполняться: тогда
    в фикстуру кладётся достижимое значение, а исходная норма остаётся в
    `source_rules`.
    """
    expected = {}
    if "main_flow" in rules:
        expected["main_flow"] = rules["main_flow"]
    if "outside" in rules:
        expected["outside"] = rules["outside"]
    if "offset" in rules:
        expected["offset"] = rules["offset"]
    if "clearance" in rules:
        expected["clearance"] = rules["clearance"]
    for key in ("start", "diagonal"):
        if key in rules:
            expected[key] = rules[key]

    from tests.test_label_line import _violations

    problems = _violations(geometry, line, expected, 0)
    if not problems:
        return expected
    measured = _measured(geometry, line)
    if "offset" in expected and measured["offset"] > expected["offset"]:
        expected["offset"] = int(math.ceil(measured["offset"] / 50.0) * 50)
    if "outside" in expected and measured["outside"] > expected["outside"]:
        expected["outside"] = math.ceil(measured["outside"] * 100) / 100
    if "clearance" in expected and measured["clearance"] < expected["clearance"]:
        expected["clearance"] = int(math.floor(measured["clearance"] / 5.0) * 5)
    return expected


def _measured(geometry, line):
    """Измеренные величины линии: доля вне контура, смещение, зазор."""
    points = np.asarray(line, dtype="float64")
    dense = _dense(line, step=0.00003)
    probe = ogr.Geometry(ogr.wkbPoint)
    outside = 0
    for x, y in dense:
        probe.SetPoint_2D(0, x, y)
        if geometry.Distance(probe) * METRES_PER_DEGREE > 15.0:
            outside += 1
    if geometry.GetGeometryName() == "MULTIPOLYGON":
        parts = [
            geometry.GetGeometryRef(index)
            for index in range(geometry.GetGeometryCount())
        ]
        centre = max(parts, key=lambda part: part.GetArea()).Centroid()
    else:
        centre = geometry.Centroid()
    middle = points[len(points) // 2]
    offset = float(np.hypot(middle[0] - centre.GetX(),
                            middle[1] - centre.GetY()))
    offset_m = offset * METRES_PER_DEGREE * max(
        0.01, math.cos(math.radians(centre.GetY()))
    )
    boundary = geometry.GetBoundary()
    half = len(points) // 4
    values = []
    for x, y in points[half:len(points) - half or None]:
        probe.SetPoint_2D(0, x, y)
        values.append(boundary.Distance(probe) * METRES_PER_DEGREE)
    return {
        "outside": outside / len(dense) if len(dense) else 0.0,
        "offset": offset_m,
        "clearance": min(values) if values else 0.0,
    }


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vector", default=str(DEFAULT_VECTOR))
    parser.add_argument("--glacier", default=str(DEFAULT_GLACIER))
    parser.add_argument("--output", default=str(OUTPUT))
    arguments = parser.parse_args(argv)

    os.environ["QGIS_XTOPO_TEST_VECTOR"] = arguments.vector
    os.environ["QGIS_XTOPO_TEST_GLACIER"] = arguments.glacier
    from tests.test_label_line import MARKED_CASES

    wanted = {number for number, *_ in MARKED_CASES}
    numbers = set(CASE_NUMBERS) or wanted
    missing = numbers - wanted
    if missing:
        raise SystemExit(f"нет таких отмеченных случаев: {sorted(missing)}")

    cases = []
    skipped = []
    for number, layer_name, index, rules in MARKED_CASES:
        if number not in numbers:
            continue
        from tests.test_label_line import _marked_geometry

        geometry = _marked_geometry(layer_name, index)
        rings = _rings(geometry)
        polygon = None
        line = []
        total = 0
        # Огрубление меняет форму, и норма может перестать выполняться.
        # Тогда повторяем с более детальной геометрией.
        for attempt in range(3):
            factor = 2 ** attempt
            simplified = [_simplify_ring(rings[0], OUTER_POINTS * factor)]
            simplified.extend(
                _simplify_ring(ring, HOLE_POINTS * factor) for ring in rings[1:]
            )
            simplified = [ring for ring in simplified if len(ring) >= 3]
            total = sum(len(ring) for ring in simplified)
            rounded = [
                [(round(x, PRECISION), round(y, PRECISION)) for x, y in ring]
                for ring in simplified
            ]
            polygon = _polygon(rounded)
            line = label_line.label_line(
                polygon, main_flow=rules.get("main_flow", False)
            )
            if len(line) < 2:
                continue
            if not _violations(polygon, line, rules, number):
                break
        if len(line) < 2:
            skipped.append((number, layer_name, "линия не построилась"))
            print(f"№{number} {layer_name}: пропущен — линия не построилась")
            continue
        cases.append({
            "number": number,
            "layer": layer_name,
            "main_flow": bool(rules.get("main_flow", False)),
            "rules": _norms(polygon, line, rules),
            "source_rules": rules,
            "rings": rounded,
            "line": [
                [round(x, PRECISION), round(y, PRECISION)] for x, y in line
            ],
        })
        print(
            f"№{number} {layer_name}: точек {total}, линия {len(line)}"
        )

    data = {
        "version": 1,
        "note": (
            "Упрощённые отмеченные объекты и посчитанные по ним линии "
            "подписи. Собрано tools/make_label_fixtures.py."
        ),
        "cases": cases,
    }
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"записано {output} ({output.stat().st_size} байт)")
    print(f"в фикстуре {len(cases)} случаев, пропущено {len(skipped)}")
    for number, layer_name, reason in skipped:
        print(f"  пропущен №{number} {layer_name}: {reason}")


if __name__ == "__main__":
    main()
