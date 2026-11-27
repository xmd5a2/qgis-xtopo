"""Процесс обработки изолиний: тайл, часть разрезания или склейка.

Контуры, вычитание воды и разрезание по ледникам держат GIL: потоки на
этой стадии не только не ускоряют расчёт, но и замедляют его.  Поэтому
работа идёт отдельными процессами, а результаты пишутся в SQLite-файлы.
Родитель склеивает их в порядке номеров тайлов и частей — так номера
линий и ссылки ``SRC`` остаются прежними.

Скрипт запускается обычным Python из QGIS и не создаёт QgsApplication:
нужны только GDAL/OGR.
"""

import importlib.util
import json
import sys
import traceback
from pathlib import Path

_TERRAIN = None


def _load_terrain():
    """Импортирует ``qgis_xtopo.core.terrain`` и в исходниках, и в QGIS.

    Установленный плагин лежит в каталоге ``qgis_xtopo`` и импортируется
    обычным образом.  В репозитории каталог называется ``qgis-xtopo``,
    поэтому пакет регистрируется вручную — так же, как это делают тесты.
    """
    global _TERRAIN
    if _TERRAIN is not None:
        return _TERRAIN
    package_dir = Path(__file__).resolve().parents[1]
    if package_dir.name == "qgis_xtopo":
        parent = str(package_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
    else:
        spec = importlib.util.spec_from_file_location(
            "qgis_xtopo",
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["qgis_xtopo"] = module
        spec.loader.exec_module(module)
    from qgis_xtopo.core import terrain

    _TERRAIN = terrain
    return terrain


def _write_layer(path, layer, fields):
    """Пишет слой тайла в SQLite без пространственного индекса.

    Индекс строится только у итоговых файлов: на промежуточном слое он
    замедляет вставку и не нужен — слой сразу читается разрезанием по
    ледникам.  Возвращает число записанных линий.
    """
    from osgeo import ogr

    # Скрипт запускается обычным Python, поэтому пакет импортируется
    # только после _load_terrain().
    from qgis_xtopo.core import spatialite

    spatial_ref = layer.GetSpatialRef()
    driver = ogr.GetDriverByName("SQLite")
    path.unlink(missing_ok=True)
    dataset = driver.CreateDataSource(
        str(path), options=list(spatialite.CREATION_OPTIONS)
    )
    target = dataset.CreateLayer(
        "isolines", spatial_ref, ogr.wkbLineString,
        options=["SPATIAL_INDEX=NO"],
    )
    for name, kind in fields:
        target.CreateField(ogr.FieldDefn(name, kind))
    target.StartTransaction()
    written = 0
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        geometry = geometry.Clone()
        # Исходные контуры трёхмерные, а промежуточный слой — обычные
        # линии: разрезание по воде и ледникам считается по XY.
        geometry.FlattenTo2D()
        item = ogr.Feature(target.GetLayerDefn())
        for name, _kind in fields:
            item.SetField(name, feature.GetField(name))
        item.SetGeometry(geometry)
        target.CreateFeature(item)
        item = None
        written += 1
    target.CommitTransaction()
    dataset = None
    return written


def _count_features(path):
    from osgeo import ogr

    dataset = ogr.Open(str(path))
    if dataset is None:
        return 0
    count = dataset.GetLayer(0).GetFeatureCount()
    dataset = None
    return count


def _run_contour(job):
    from osgeo import ogr

    terrain = _load_terrain()
    gdal, _ogr, _osr = terrain._gdal()
    # Как и в основном процессе, не ждём fsync на каждой транзакции.
    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    # Общий кэш GDAL рассчитан на один процесс; процессов тайлов много,
    # а окно одного тайла небольшое — 256 МБ на процесс достаточно.
    gdal.SetConfigOption("GDAL_CACHEMAX", str(256))

    source = job["source"]
    tile = job["tile"]
    step = job["step"]
    feet = bool(job.get("feet"))
    smooth = bool(job.get("smooth"))
    simplify = job["simplify"]
    index = job["index"]
    temp_dir = Path(job["temp_dir"])
    vector_dir = Path(job["vector_dir"])
    water_paths = [Path(path) for path in job["water_paths"]]

    dataset, detail_count = terrain._contour_tile(
        (source, tile, step), simplify, feet, smooth
    )
    detailed = dataset.GetLayer(0)
    overview = dataset.GetLayer(1) if simplify > 0 else None
    spatial_ref = detailed.GetSpatialRef()
    overview_count = overview.GetFeatureCount() if overview is not None else 0

    touched = set()
    # Тайлы уже идут отдельными процессами: внутренние потоки здесь
    # только перегружали бы ядра.
    water_cut = terrain._subtract_water_from_layer(
        detailed, water_paths, spatial_ref, touched_out=touched,
        max_workers=1,
    )
    if overview is not None:
        terrain._subtract_water_from_layer(
            overview, water_paths, spatial_ref, only_keys=touched,
            max_workers=1,
        )

    detail_path = temp_dir / f"isolines_tile{index}.sqlite"
    overview_path = temp_dir / f"isolines_tile{index}_overview.sqlite"
    detail_rows = _write_layer(
        detail_path, detailed,
        (("ID", ogr.OFTInteger), ("ELEV", ogr.OFTReal)),
    )
    overview_rows = 0
    if overview is not None:
        overview_rows = _write_layer(
            overview_path, overview,
            (("ID", ogr.OFTInteger), ("ELEV", ogr.OFTReal),
             ("SRC", ogr.OFTInteger)),
        )
    dataset = None

    return {
        "index": index,
        "detail": detail_count,
        "overview": overview_count,
        "detail_rows": detail_rows,
        "overview_rows": overview_rows,
        "water_cut": water_cut,
    }


def _run_split(job):
    terrain = _load_terrain()
    gdal, _ogr, _osr = terrain._gdal()
    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    gdal.SetConfigOption("GDAL_CACHEMAX", str(256))

    source = Path(job["source"])
    suffix = job["suffix"]
    temp_dir = Path(job["temp_dir"])
    vector_dir = Path(job["vector_dir"])
    config = type(
        "Config", (), {"vector_dir": vector_dir, "temp_dir": temp_dir}
    )
    if terrain._generate_glacier_isolines(
        config, source, progress=None, suffix=suffix,
        fid_range=job.get("fid_range"),
    ) is None:
        raise RuntimeError("glacier split failed")
    return {
        "index": job["index"],
        "kind": job.get("kind", ""),
        "regular": _count_features(
            vector_dir / f"isolines_regular{suffix}.sqlite"
        ),
        "glacier": _count_features(
            vector_dir / f"isolines_glacier{suffix}.sqlite"
        ),
    }


def _run_merge(job):
    terrain = _load_terrain()
    gdal, _ogr, _osr = terrain._gdal()
    gdal.SetConfigOption("OGR_SQLITE_SYNCHRONOUS", "OFF")
    for task in job["tasks"]:
        destination = Path(task["destination"])
        sources = [Path(path) for path in task["sources"]]
        if not sources:
            destination.unlink(missing_ok=True)
            continue
        src_offsets = task.get("src_offsets")
        if task.get("full"):
            raw = Path(task["raw"])
            terrain._merge_tile_layers(
                raw, sources, task["offsets"], src_offsets,
            )
            terrain._write_full_layer(raw, destination)
            raw.unlink(missing_ok=True)
        else:
            terrain._merge_tile_layers(
                destination, sources, task["offsets"], src_offsets,
            )
    return {"index": job["index"]}


def _run(job):
    mode = job.get("mode", "contour")
    if mode == "contour":
        return _run_contour(job)
    if mode == "split":
        return _run_split(job)
    if mode == "merge":
        return _run_merge(job)
    raise ValueError(f"unknown job mode: {mode}")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        try:
            _load_terrain()
        except Exception:  # noqa: BLE001 - родитель выберет другой Python
            traceback.print_exc()
            return 1
        return 0
    if len(sys.argv) != 2:
        return 2
    try:
        job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = _run(job)
    except Exception:  # noqa: BLE001 - родитель покажет хвост журнала
        traceback.print_exc()
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
