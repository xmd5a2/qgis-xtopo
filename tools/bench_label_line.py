#!/usr/bin/env python3
"""Замер скорости построения линий подписи вдоль полигонов.

Скрипт повторяет замеры, которыми проверялась замена GRASS-скелета:
считает время ``label_line`` на целых слоях проекта, а по флагу
``--grass`` сравнивает со старой цепочкой Processing
(``fix`` → ``polygonstolines`` → ``buffer`` ×2 → ``simplify`` →
``grass7:v.voronoi.skeleton`` → ``simplify`` → ``smooth`` → ``dissolve``).

Примеры::

    python3 tools/bench_label_line.py \\
        --vector ~/qgis_projects/bike-marafon/vector

    # ледники: у них концы ищутся по длине фигуры (main_flow)
    python3 tools/bench_label_line.py --vector ... \\
        --layer glacier --main-flow

    # сравнение со старой цепочкой GRASS (нужен установленный GRASS)
    python3 tools/bench_label_line.py --vector ... --grass

Каталог по умолчанию можно задать переменной
``QGIS_XTOPO_BENCH_VECTOR``. Рабочие данные только читаются; в репозиторий
ничего не копируется.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from osgeo import ogr

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import register_package  # noqa: E402

register_package()

from qgis_xtopo.core.label_line import label_line  # noqa: E402

ogr.UseExceptions()

DEFAULT_VECTOR = os.environ.get(
    "QGIS_XTOPO_BENCH_VECTOR",
    os.path.expanduser("~/qgis_projects/bike-marafon/vector"),
)
DEFAULT_LAYERS = ("wood", "wetland", "water", "bay_polygon", "strait")

OLD_STEPS = (
    ("fix", {}),
    ("polygonstolines", {}),
    ("buffer", {"distance": 0.002}),
    ("buffer", {"distance": -0.002}),
    ("simplify", {"tolerance": 0.0002}),
    ("skeleton", {"smoothness": 20, "thin": 1}),
    ("simplify", {"tolerance": 0.002}),
    ("smooth", {"iterations": 10}),
    ("dissolve", {}),
)


def _length(line):
    points = np.asarray(line, dtype="float64")
    if len(points) < 2:
        return 0.0
    return float(np.sum(np.hypot(*np.diff(points, axis=0).T)))


def _load(path):
    dataset = ogr.Open(str(path))
    if dataset is None:
        raise FileNotFoundError(path)
    layer = dataset.GetLayer(0)
    geometries = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None and not geometry.IsEmpty():
            geometries.append(geometry.Clone())
    return geometries


def _benchmark(path, main_flow):
    geometries = _load(path)
    started = time.monotonic()
    lengths = []
    empty = 0
    for geometry in geometries:
        line = label_line(geometry, main_flow=main_flow)
        if len(line) < 2:
            empty += 1
        lengths.append(_length(line))
    elapsed = time.monotonic() - started
    return {
        "count": len(geometries),
        "elapsed": elapsed,
        "empty": empty,
        "median": float(np.median(lengths)) if lengths else 0.0,
        "maximum": float(max(lengths)) if lengths else 0.0,
    }


def _print_bench(rows):
    print(f"{'слой':14} {'объектов':>9} {'время, с':>9} "
          f"{'мс/объект':>10} {'пустых':>7} {'медиана, °':>11} "
          f"{'максимум, °':>12}")
    for name, result in rows:
        per_object = result["elapsed"] / max(result["count"], 1) * 1000
        print(f"{name:14} {result['count']:9} {result['elapsed']:9.2f} "
              f"{per_object:10.2f} {result['empty']:7} "
              f"{result['median']:11.5f} {result['maximum']:12.5f}")


def _run(runner, algorithm, parameters):
    result = runner.run(algorithm, parameters)
    for key, value in result.items():
        if str(key).lower() == "output":
            return value
    return parameters.get("INPUT")


def _grass_chain(source, work, runner):
    """Время прежней цепочки подготовки линий подписи со скелетом GRASS."""
    current = str(source)
    counter = [0]

    def temp_path(suffix):
        counter[0] += 1
        return work / f"{source.stem}_{suffix}_{counter[0]}.gpkg"

    started = time.monotonic()
    for operation, parameters in OLD_STEPS:
        if operation == "fix":
            current = _run(runner, "native:fixgeometries", {
                "INPUT": current, "OUTPUT": temp_path("fixed")})
        elif operation == "polygonstolines":
            current = _run(runner, "native:polygonstolines", {
                "INPUT": current, "OUTPUT": temp_path("lines")})
        elif operation == "buffer":
            current = _run(runner, "native:buffer", {
                "INPUT": current, "DISTANCE": parameters["distance"],
                "SEGMENTS": 5, "END_CAP_STYLE": 0, "JOIN_STYLE": 0,
                "MITER_LIMIT": 2, "DISSOLVE": parameters.get("dissolve", False),
                "OUTPUT": temp_path("buffered")})
        elif operation == "simplify":
            current = _run(runner, "native:simplifygeometries", {
                "INPUT": current, "METHOD": 0,
                "TOLERANCE": parameters["tolerance"],
                "OUTPUT": temp_path("simpl")})
        elif operation == "skeleton":
            current = _run(runner, "grass7:v.voronoi.skeleton", {
                "input": current,
                "smoothness": parameters.get("smoothness", 0.25),
                "thin": parameters.get("thin", -1), "-s": True,
                "output": temp_path("skel")})
        elif operation == "smooth":
            current = _run(runner, "native:smoothgeometry", {
                "INPUT": current, "ITERATIONS": parameters.get("iterations", 5),
                "OFFSET": parameters.get("offset", 0.25),
                "MAX_ANGLE": parameters.get("max_angle", 180),
                "OUTPUT": temp_path("smoothed")})
        elif operation == "dissolve":
            current = _run(runner, "native:dissolve", {
                "INPUT": current, "FIELD": ["id"], "SEPARATE_DISJOINT": False,
                "OUTPUT": temp_path("dissolved")})
    return time.monotonic() - started


def _print_grass(rows):
    print(f"{'слой':14} {'объектов':>9} {'GRASS, с':>10} "
          f"{'новый, с':>9} {'ускорение':>10}")
    for name, grass_time, result in rows:
        speedup = grass_time / max(result["elapsed"], 1e-9)
        print(f"{name:14} {result['count']:9} {grass_time:10.2f} "
              f"{result['elapsed']:9.2f} {speedup:9.1f}x")


def main():
    parser = argparse.ArgumentParser(
        description="Замер скорости линий подписи вдоль полигонов.")
    parser.add_argument("--vector", default=DEFAULT_VECTOR,
                        help="каталог со слоями .sqlite")
    parser.add_argument("--layer", action="append", dest="layers",
                        help="слой для замера (можно повторять)")
    parser.add_argument("--main-flow", action="store_true",
                        help="искать концы по длине фигуры (ледники)")
    parser.add_argument("--grass", action="store_true",
                        help="сравнить со старой цепочкой GRASS")
    parser.add_argument("--work", default=None,
                        help="каталог промежуточных файлов GRASS")
    args = parser.parse_args()

    vector = Path(args.vector).expanduser()
    layers = args.layers or list(DEFAULT_LAYERS)
    rows = []
    for name in layers:
        path = vector / f"{name}.sqlite"
        print(f"{name}: замер…", flush=True)
        rows.append((name, _benchmark(path, args.main_flow)))
    print()
    _print_bench(rows)

    if not args.grass:
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from tests import qgis_app

    qgis_app()
    from qgis_xtopo.core.pipeline import ProcessingRunner

    runner = ProcessingRunner()
    work = Path(args.work).expanduser() if args.work else Path(
        tempfile.mkdtemp(prefix="qgis_xtopo_label_bench_"))
    work.mkdir(parents=True, exist_ok=True)
    grass_rows = []
    try:
        for name, result in rows:
            source = vector / f"{name}.sqlite"
            print(f"{name}: старая цепочка GRASS…", flush=True)
            try:
                grass_time = _grass_chain(source, work, runner)
            except Exception as error:  # noqa: BLE001 - GRASS может отсутствовать
                print(f"  не удалось: {error}")
                break
            grass_rows.append((name, grass_time, result))
    finally:
        if args.work is None:
            shutil.rmtree(work, ignore_errors=True)
    if grass_rows:
        print()
        _print_grass(grass_rows)


if __name__ == "__main__":
    main()
