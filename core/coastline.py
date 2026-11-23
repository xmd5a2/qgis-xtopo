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


# Полоса по водную сторону берега: положительный буфер задаёт её ширину,
# отрицательный убирает кромку у самой линии. Сужение должно быть заметно
# меньше половины ширины: при равенстве полоса вырождается в линию,
# pointonsurface даёт единичные точки у концов, и океан не находится.
SIDE_BUFFER_WIDTH = 0.0002
SIDE_BUFFER_INSET = 0.3 * SIDE_BUFFER_WIDTH

# Площадь в квадратных градусах: всё, что меньше, — щель на стыке
# геометрий, а не водоём.  1e-8°² — это порядка десятых долей м².
MIN_POLYGON_AREA = 1e-8


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
    source_layer = dataset.GetLayer(0)
    # Слой создаётся в СК исходника: иначе промежуточные GeoJSON и
    # итоговый water_dissolved.sqlite остаются без пространственной
    # привязки.
    layer = output.CreateLayer(
        "coastline", source_layer.GetSpatialRef(), geom_type=ogr.wkbLineString
    )
    try:
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


def _geometry_area(geometry):
    """Площадь только площадных частей геометрии.

    Пересечение двух полигонов может оказаться линией, точкой или
    коллекцией. GetArea у не-площадной геометрии пишет предупреждение
    GDAL и возвращает 0, поэтому площадь собирается по частям.
    """
    if geometry is None or geometry.IsEmpty():
        return 0.0
    if geometry.GetGeometryName() in ("POLYGON", "MULTIPOLYGON"):
        return geometry.GetArea()
    total = 0.0
    for index in range(geometry.GetGeometryCount()):
        part = geometry.GetGeometryRef(index)
        if part is not None:
            total += _geometry_area(part)
    return total


def _polygon_area(path):
    """Суммарная площадь полигональных объектов слоя."""
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return 0.0
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return 0.0
        area = 0.0
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None and not geometry.IsEmpty() and geometry.GetDimension() == 2:
                area += _geometry_area(geometry)
        return area
    finally:
        dataset = None


def _intersection_area(first_path, second_path):
    """Площадь пересечения полигонов двух слоёв."""
    from osgeo import ogr

    first = ogr.Open(str(first_path))
    second = ogr.Open(str(second_path))
    if first is None or second is None:
        if first is not None:
            first = None
        if second is not None:
            second = None
        return 0.0
    try:
        first_layer = first.GetLayer(0)
        second_layer = second.GetLayer(0)
        if first_layer is None or second_layer is None:
            return 0.0
        area = 0.0
        for feature in first_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                continue
            second_layer.SetSpatialFilter(geometry)
            for other in second_layer:
                other_geometry = other.GetGeometryRef()
                if (other_geometry is None or other_geometry.IsEmpty()
                        or other_geometry.GetDimension() != 2):
                    continue
                if not geometry.Intersects(other_geometry):
                    continue
                try:
                    common = geometry.Intersection(other_geometry)
                except Exception:
                    continue
                if common is not None and not common.IsEmpty():
                    area += _geometry_area(common)
            second_layer.SetSpatialFilter(None)
        return area
    finally:
        second = None
        first = None


def _looks_like_land(ocean_path, island_path):
    """Похоже ли выбранное «море» на острова, а не на океан.

    Направление береговых линий после dissolve теряется, и полоса по
    водной стороне может оказаться на суше.  Тогда выбранные части
    охвата почти целиком совпадают с полигонами островов.  Настоящий
    океан пересекается с ними только на кромке берега.
    """
    if not island_path.exists():
        return False
    total = _polygon_area(ocean_path)
    if total <= 0:
        return True
    overlap = _intersection_area(ocean_path, island_path)
    return overlap >= total * 0.5


def _largest_polygon(source, destination):
    """Самый крупный полигон слоя — запасной вариант океана.

    Если в охвате нет открытых участков берега (все острова обведены
    замкнутыми кольцами), сторону воды по линиям не определить.  Для
    такого охвата океан — самая большая часть, на которые берег разбил
    прямоугольник охвата.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return None
        spatial_ref = layer.GetSpatialRef()
        best = None
        best_area = -1.0
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                continue
            area = _geometry_area(geometry)
            if area > best_area:
                best_area = area
                best = geometry.Clone()
    finally:
        dataset = None
    if best is None:
        return None
    if best.GetGeometryName() == "POLYGON":
        best = ogr.ForceToMultiPolygon(best)
    driver = ogr.GetDriverByName("GeoJSON")
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        raise CoastlineError(f"Cannot create {destination}")
    try:
        layer = output.CreateLayer(
            "ocean", spatial_ref, geom_type=ogr.wkbMultiPolygon
        )
        item = ogr.Feature(layer.GetLayerDefn())
        item.SetGeometry(best)
        layer.CreateFeature(item)
    finally:
        output = None
    return destination


def _write_polygons(source, destination):
    """Записать только полигональные объекты, сохранив поля.

    ``water_dissolved`` в прежних сборках мог содержать линии и точки:
    они попадали туда после ошибочного определения океана.  Растворение
    и вычитание с таким слоем работают непредсказуемо, поэтому перед
    объединением и сохранением оставляем только площадные объекты.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    driver = ogr.GetDriverByName("GeoJSON")
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        dataset = None
        raise CoastlineError(f"Cannot create {destination}")
    try:
        source_layer = dataset.GetLayer(0)
        if source_layer is None:
            return None
        source_definition = source_layer.GetLayerDefn()
        layer = output.CreateLayer(
            "water", source_layer.GetSpatialRef(), geom_type=ogr.wkbMultiPolygon
        )
        definition = layer.GetLayerDefn()
        fields = [
            (index, source_definition.GetFieldDefn(index).GetName())
            for index in range(source_definition.GetFieldCount())
        ]
        written = 0
        for feature in source_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                continue
            if _geometry_area(geometry) <= MIN_POLYGON_AREA:
                continue
            if geometry.GetGeometryName() == "POLYGON":
                geometry = ogr.ForceToMultiPolygon(geometry)
            item = ogr.Feature(definition)
            item.SetGeometry(geometry)
            for source_index, name in fields:
                target_index = definition.GetFieldIndex(name)
                if target_index >= 0:
                    item.SetField(target_index, feature.GetField(source_index))
            layer.CreateFeature(item)
            written += 1
        if not written:
            output = None
            destination.unlink(missing_ok=True)
            return None
    finally:
        output = None
        dataset = None
    return destination


def _output(result, parameters, key="OUTPUT"):
    for name, value in result.items():
        if str(name).lower() == key.lower():
            return value
    return parameters.get(key) or parameters.get(key.lower())


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


def _side_ocean(runner, config, split, open_source, side):
    """Части охвата, лежащие по указанную сторону берега.

    Входные линии не растворяются: ``native:dissolve`` меняет
    направление части линий, и сторона воды перестаёт быть той, которую
    задаёт OSM (вода справа по ходу линии).  Направление исходных
    незамкнутых ways сохраняется, поэтому буфер строится на воде.
    """
    tag = "right" if side else "left"
    one_side = _run(runner, "native:singlesidedbuffer", {
        "INPUT": str(open_source),
        "DISTANCE": SIDE_BUFFER_WIDTH,
        "SIDE": side,
        "SEGMENTS": 5,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2,
        "OUTPUT": _temp(config, f"coastline_side_{tag}.geojson"),
    })
    thin = _run(runner, "native:buffer", {
        "INPUT": one_side,
        "DISTANCE": -SIDE_BUFFER_INSET,
        "SEGMENTS": 5,
        "END_CAP_STYLE": 0,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2,
        "DISSOLVE": False,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_thin.geojson"),
    })
    parts = _run(runner, "native:multiparttosingleparts", {
        "INPUT": thin,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_parts.geojson"),
    })
    clipped = _run(runner, "native:intersection", {
        "INPUT": parts,
        "OVERLAY": str(config.crop_path),
        "OUTPUT": _temp(config, f"coastline_side_{tag}_clipped.geojson"),
    })
    # Точка берётся строго внутри отступа по водной стороне, а часть
    # области выбирается по «содержит»: вершина на самой линии берега
    # принадлежит сразу двум частям, и океаном выбирался обломок.
    vertices = _run(runner, "native:pointonsurface", {
        "INPUT": clipped,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_vertices.gpkg"),
    })
    return _run(runner, "native:extractbylocation", {
        "INPUT": split,
        "INTERSECT": vertices,
        "PREDICATE": [1],
        "OUTPUT": _temp(config, f"coastline_ocean_{tag}.geojson"),
    })


def prepare_coastline(config, bbox, progress=None, cancel=None):
    """Create or update ``water_dissolved.sqlite`` with the ocean."""
    from .pipeline import ProcessingRunner, PipelineError, save_vector

    notify = progress or (lambda message: None)
    lines = config.vector_dir / "coastline.sqlite"
    crop = config.crop_path
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
        # Разрезанные части и точки-вершины — GeoPackage: у GeoJSON нет
        # пространственного индекса, и native:extractbylocation предупреждает
        # о полном переборе вершин для каждой части области.
        split = _run(runner, "native:splitwithlines", {
            "INPUT": str(crop),
            "LINES": simplified,
            "OUTPUT": _temp(config, "coastline_split.gpkg"),
        })

        notify("Береговая линия: определение стороны океана")
        # Сторону океана показывают только незамкнутые линии берега:
        # замкнутые кольца — это острова, и по ним сторона выходит неверной
        # (в дельте Дуная океаном оказывался крошечный обломок у края
        # области, а море не заливалось вовсе).
        island = config.vector_dir / "island.sqlite"
        ocean = None
        if _has_features(open_source):
            ocean = _side_ocean(runner, config, split, open_source, 1)
        if (ocean is not None and _has_features(ocean)
                and not _looks_like_land(ocean, island)):
            pass
        else:
            if ocean is not None and _has_features(ocean):
                notify(
                    "WARNING: Береговая линия: сторона океана определена "
                    "неверно, повтор с другой стороны"
                )
            retried = None
            if _has_features(open_source):
                retried = _side_ocean(runner, config, split, open_source, 0)
            if (retried is not None and _has_features(retried)
                    and not _looks_like_land(retried, island)):
                ocean = retried
            else:
                # Все берега замкнуты в кольца: сторону воды по
                # направлению линий не определить.  Океан — крупнейшая
                # часть, на которые берег разбил охват.
                ocean = _largest_polygon(
                    split, _temp(config, "coastline_ocean_largest.geojson")
                )
                if ocean is not None:
                    notify(
                        "WARNING: Береговая линия: океан взят как "
                        "крупнейшая часть охвата"
                    )
        if ocean is None or not _has_features(ocean):
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
        existing = None
        if water.exists() and _has_features(water):
            existing = _write_polygons(
                water, config.temp_dir / "coastline_existing_water.geojson"
            )
        if existing is not None:
            notify("Береговая линия: объединение с внутренними водами")
            merged = _run(runner, "native:mergevectorlayers", {
                "LAYERS": [str(existing), ocean],
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
        final = _write_polygons(
            final, config.temp_dir / "coastline_water_polygons.geojson"
        )
        if final is None:
            notify("WARNING: Береговая линия: в воде нет полигонов")
            return False
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
