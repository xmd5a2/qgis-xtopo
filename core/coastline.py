"""Build an ocean polygon from OSM coastline ways.

The old Docker pipeline split the project bbox with the coastline and
used a one-sided buffer to find which pieces lie on the water side.
This module does the same with QGIS Processing algorithms, then merges
the ocean with inland water so the project keeps a single
``water_dissolved`` layer.
"""

from pathlib import Path


class CoastlineError(RuntimeError):
    pass


def _line_geometries(geometry):
    if geometry is None or geometry.IsEmpty():
        return []
    name = geometry.GetGeometryName()
    if name in ("LINESTRING", "LINEARRING"):
        return [geometry.Clone()]
    if name in ("POLYGON", "MULTIPOLYGON"):
        boundary = geometry.GetBoundary()
        return _line_geometries(boundary)
    if name in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
        result = []
        for index in range(geometry.GetGeometryCount()):
            result.extend(_line_geometries(geometry.GetGeometryRef(index)))
        return result
    return []


def _is_closed_ring(geometry):
    """Замыкается ли линия в кольцо."""
    if geometry is None or geometry.IsEmpty():
        return False
    if geometry.GetGeometryName() not in ("LINESTRING", "LINEARRING"):
        return False
    count = geometry.GetPointCount()
    if count < 3:
        return False
    first = geometry.GetPoint(0)
    last = geometry.GetPoint(count - 1)
    return abs(first[0] - last[0]) < 1e-12 and abs(first[1] - last[1]) < 1e-12


def _write_lines(source, destination, open_only=False):
    """Write only line geometries; closed coastline ways arrive as polygons.

    ``open_only`` drops the closed rings: those are islands and they must
    not take part in the search for the water side of the coastline.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    driver = ogr.GetDriverByName("GeoJSON")
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    layer = output.CreateLayer("coastline", geom_type=ogr.wkbLineString)
    try:
        source_layer = dataset.GetLayer(0)
        for feature in source_layer:
            for geometry in _line_geometries(feature.GetGeometryRef()):
                if open_only and _is_closed_ring(geometry):
                    continue
                item = ogr.Feature(layer.GetLayerDefn())
                item.SetGeometry(geometry)
                layer.CreateFeature(item)
    finally:
        output = None
        dataset = None
    return destination


def _has_features(path):
    try:
        from osgeo import ogr
    except ImportError:
        return True
    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return False
    try:
        layer = dataset.GetLayer(0)
        return layer is not None and layer.GetNextFeature() is not None
    except Exception:
        return True
    finally:
        dataset = None


def _output(result, parameters):
    for key, value in result.items():
        if str(key).lower() == "output":
            return value
    return parameters.get("OUTPUT") or parameters.get("output")


def _run(runner, algorithm, parameters):
    result = runner.run(algorithm, parameters)
    output = _output(result, parameters)
    if output is None:
        raise CoastlineError(f"{algorithm} returned no output")
    if isinstance(output, str) and not output.startswith(
        ("memory:", "ogr:", "postgres:", "pointcloud:", "mesh:")
    ):
        if not Path(output).exists():
            raise CoastlineError(f"{algorithm} produced no file")
    return output


def _temp(config, name):
    path = config.temp_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def prepare_coastline(config, bbox, progress=None, cancel=None):
    """Create or update ``water_dissolved.sqlite`` with the ocean."""
    from .pipeline import ProcessingRunner, PipelineError, save_vector

    notify = progress or (lambda message: None)
    lines = config.vector_dir / "coastline.sqlite"
    crop = config.project_dir / "crop.geojson"
    if not lines.exists() or not _has_features(lines):
        notify("Береговая линия: нет данных")
        return False
    if not crop.exists():
        notify("WARNING: Береговая линия: не найден охват проекта")
        return False

    runner = ProcessingRunner(progress=progress, cancel=cancel)
    try:
        notify("Береговая линия: подготовка линий")
        line_source = _write_lines(lines, config.temp_dir / "coastline_lines.geojson")
        open_source = _write_lines(
            lines, config.temp_dir / "coastline_open_lines.geojson", open_only=True
        )
        dissolved = _run(runner, "native:dissolve", {
            "INPUT": str(line_source),
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
            "OUTPUT": _temp(config, "coastline_dissolved.geojson"),
        })
        simplified = _run(runner, "native:simplifygeometries", {
            "INPUT": dissolved,
            "METHOD": 0,
            "TOLERANCE": 0.00005,
            "OUTPUT": _temp(config, "coastline_simplified.geojson"),
        })

        notify("Береговая линия: разделение охвата")
        split = _run(runner, "native:splitwithlines", {
            "INPUT": str(crop),
            "LINES": simplified,
            "OUTPUT": _temp(config, "coastline_split.geojson"),
        })

        notify("Береговая линия: определение стороны океана")
        # Сторону океана показывают только незамкнутые линии берега:
        # замкнутые кольца — это острова, и по ним сторона выходит неверной
        # (в дельте Дуная океаном оказывался крошечный обломок у края
        # области, а море не заливалось вовсе).
        open_dissolved = _run(runner, "native:dissolve", {
            "INPUT": str(open_source),
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
            "OUTPUT": _temp(config, "coastline_open_dissolved.geojson"),
        })
        open_simplified = _run(runner, "native:simplifygeometries", {
            "INPUT": open_dissolved,
            "METHOD": 0,
            "TOLERANCE": 0.00005,
            "OUTPUT": _temp(config, "coastline_open_simplified.geojson"),
        })
        one_side = _run(runner, "native:singlesidedbuffer", {
            "INPUT": open_simplified,
            "DISTANCE": 0.0002,
            "SIDE": 1,
            "SEGMENTS": 5,
            "JOIN_STYLE": 0,
            "MITER_LIMIT": 2,
            "OUTPUT": _temp(config, "coastline_side.geojson"),
        })
        thin = _run(runner, "native:buffer", {
            "INPUT": one_side,
            "DISTANCE": -0.0001,
            "SEGMENTS": 5,
            "END_CAP_STYLE": 0,
            "JOIN_STYLE": 0,
            "MITER_LIMIT": 2,
            "DISSOLVE": False,
            "OUTPUT": _temp(config, "coastline_side_thin.geojson"),
        })
        parts = _run(runner, "native:multiparttosingleparts", {
            "INPUT": thin,
            "OUTPUT": _temp(config, "coastline_side_parts.geojson"),
        })
        clipped = _run(runner, "native:intersection", {
            "INPUT": parts,
            "OVERLAY": str(crop),
            "OUTPUT": _temp(config, "coastline_side_clipped.geojson"),
        })
        # Точка берётся строго внутри отступа по водной стороне, а часть
        # области выбирается по «содержит»: вершина на самой линии берега
        # принадлежит сразу двум частям, и океаном выбирался обломок.
        vertices = _run(runner, "native:pointonsurface", {
            "INPUT": clipped,
            "OUTPUT": _temp(config, "coastline_side_vertices.geojson"),
        })

        ocean = _run(runner, "native:extractbylocation", {
            "INPUT": split,
            "INTERSECT": vertices,
            "PREDICATE": [1],
            "OUTPUT": _temp(config, "coastline_ocean.geojson"),
        })
        if not _has_features(ocean):
            notify("WARNING: Береговая линия: океан не построен")
            return False
        ocean = _run(runner, "native:fixgeometries", {
            "INPUT": ocean,
            "OUTPUT": _temp(config, "coastline_ocean_fixed.geojson"),
        })
        ocean = _run(runner, "native:fieldcalculator", {
            "INPUT": ocean,
            "FIELD_NAME": "natural",
            "FIELD_TYPE": 2,
            "FIELD_LENGTH": 20,
            "FIELD_PRECISION": 0,
            "FORMULA": "'water'",
            "OUTPUT": _temp(config, "coastline_ocean_water.geojson"),
        })

        island = config.vector_dir / "island.sqlite"
        if island.exists() and _has_features(island):
            notify("Береговая линия: вычитание островов")
            island_fixed = _run(runner, "native:fixgeometries", {
                "INPUT": str(island),
                "OUTPUT": _temp(config, "coastline_islands_fixed.geojson"),
            })
            ocean = _run(runner, "native:difference", {
                "INPUT": ocean,
                "OVERLAY": island_fixed,
                "OUTPUT": _temp(config, "coastline_ocean_islands.geojson"),
            })

        water = config.vector_dir / "water_dissolved.sqlite"
        if water.exists() and _has_features(water):
            notify("Береговая линия: объединение с внутренними водами")
            merged = _run(runner, "native:mergevectorlayers", {
                "LAYERS": [str(water), ocean],
                "OUTPUT": _temp(config, "coastline_merged.geojson"),
            })
            # Растворение останавливается на первом невалидном объекте, а
            # во внутренних водах попадается полигон с самопересечением.
            # Без починки геометрии растворение падает, океан не попадает
            # в water_dissolved — и море на карте не рисуется вовсе.
            merged = _run(runner, "native:fixgeometries", {
                "INPUT": merged,
                "OUTPUT": _temp(config, "coastline_merged_fixed.geojson"),
            })
            ocean = _run(runner, "native:dissolve", {
                "INPUT": merged,
                "FIELD": [],
                "SEPARATE_DISJOINT": False,
                "OUTPUT": _temp(config, "coastline_merged_dissolved.geojson"),
            })

        final = _run(runner, "native:multiparttosingleparts", {
            "INPUT": ocean,
            "OUTPUT": _temp(config, "coastline_water_parts.geojson"),
        })
        save_vector(final, water, "water_dissolved")
        notify("Береговая линия: water_dissolved обновлён")
        return True
    except InterruptedError:
        raise
    except PipelineError as exc:
        notify(f"WARNING: Береговая линия не обработана: {exc}")
        return False
    except CoastlineError as exc:
        notify(f"WARNING: Береговая линия не обработана: {exc}")
        return False
    except Exception as exc:
        notify(f"WARNING: Береговая линия не обработана: {exc}")
        return False
