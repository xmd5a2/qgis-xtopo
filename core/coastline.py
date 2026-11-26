"""Build an ocean polygon from OSM coastline ways.

The old Docker pipeline split the project bbox with the coastline and
used a one-sided buffer to find which pieces lie on the water side.
This module does the same with QGIS Processing algorithms, then merges
the ocean with inland water so the project keeps a single
``water_dissolved`` layer.
"""

import math
from pathlib import Path


class CoastlineError(RuntimeError):
    pass


# Смещение линии берега в воду, в градусах.  По этой линии равномерно
# расставляются точки, и часть охвата получает голоса пропорционально
# длине своего берега.  Смещение должно быть больше нуля, иначе точки
# лягут на саму линию и попадут сразу в две части охвата.
SIDE_OFFSET = 0.00013

# Сколько точек ставить вдоль смещённой линии.  Число фиксировано, поэтому
# стоимость выбора стороны океана не растёт вместе с длиной берега.
OCEAN_POINT_TARGET = 12000
OCEAN_POINT_MIN = 0.0005
OCEAN_POINT_MAX = 0.01

# Площадь в квадратных градусах: всё, что меньше, — щель на стыке
# геометрий, а не водоём.  1e-8°² — это порядка десятых долей м².
MIN_POLYGON_AREA = 1e-8

# Проверка «океан похож на острова» перебирает все полигоны слоя island
# и считает площадь пересечения.  В архипелаге полигонов десятки тысяч,
# и проверка стоит минуты; сторону воды там надёжно задаёт большинство
# водных точек, поэтому для крупных слоёв проверка пропускается.
LOOKS_LIKE_LAND_MAX_ISLANDS = 2000

# Для крупного архипелага слой island почти целиком состоит из тех же
# OSM-way, что уже лежат в coastline.sqlite. Вычитать их из океана не
# нужно: они уже вычтены кольцами берега, а пересечение с океаном даёт
# лишь полосы от упрощения.  Фильтр по @id включается только на больших
# слоях, чтобы не менять результат небольших охватов.
ISLAND_ID_FILTER_MIN_FEATURES = 5000

# Доля береговых точек, ниже которой часть охвата считается сушей.
# Случайно попавшая на сушу короткая обратная way даёт доли процента, а
# настоящий берег — заметно больше.  Точки расставлены равномерно, поэтому
# их число пропорционально длине берега: длинный фьорд, обведённый одной
# way, больше не теряется.
OCEAN_POINT_SHARE = 0.01

# Крупная суша может набрать больше 1% точек из-за обратных way.  У воды
# точки стоят вдоль всего берега, поэтому их плотность на единицу площади
# заметно выше средней по охвату.  Части с плотностью ниже 10% средней
# считаем сушей; малые водоёмы не страдают — у них плотность высокая.
OCEAN_MIN_DENSITY_SHARE = 0.1

# Хвост линии (в градусах), по которому считается направление продления.
# Последний сегмент у границы выгрузки может дрожать вдоль неё, и
# продление по одному сегменту уводит берег в море или в сушу.
OCEAN_EXTENSION_TAIL = 0.01


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


def _driver_name(destination):
    """Драйвер промежуточного слоя по расширению.

    GeoPackage держит крупные геометрии и имеет пространственный индекс.
    GeoJSON годится только для небольших слоёв: OGR отказывается читать
    объект больше нескольких десятков мегабайт, а растворённый берег
    архипелага — это одна линия на миллионы вершин.
    """
    return "GPKG" if Path(destination).suffix.lower() == ".gpkg" else "GeoJSON"


def _write_line_features(source, destination, keep=None):
    """Пишет линейные части объектов, для которых верно ``keep``.

    Возвращает путь или ``None``, если подходящих линий не оказалось.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    driver = ogr.GetDriverByName(_driver_name(destination))
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
    written = 0
    try:
        for feature in source_layer:
            for geometry in _line_geometries(feature.GetGeometryRef()):
                if keep is not None and not keep(geometry):
                    continue
                item = ogr.Feature(layer.GetLayerDefn())
                item.SetGeometry(geometry)
                layer.CreateFeature(item)
                written += 1
    finally:
        output = None
        dataset = None
    if not written:
        destination.unlink(missing_ok=True)
        return None
    return destination


def _write_lines(source, destination, open_only=False):
    """Write only line geometries; closed coastline ways arrive as polygons.

    ``open_only`` drops the closed rings: those are islands and they must
    not take part in the search for the water side of the coastline.
    """
    if open_only:
        _write_line_features(
            source, destination, keep=lambda geometry: not _is_closed_ring(geometry)
        )
    else:
        _write_line_features(source, destination)
    return Path(destination)


def _write_selected_lines(source, destination, closed):
    """Пишет только замкнутые кольца или только незамкнутые линии.

    После растворения одиночные ways складываются в кольца островов.
    Такие кольца не нужно резать прямоугольником охвата: их площадь
    вычитается из результата одним ``difference``.  Для архипелага это
    заметно быстрее, чем ``splitwithlines``, который прогоняет каждое
    кольцо по всем уже полученным частям охвата.
    """
    keep = _is_closed_ring if closed else (
        lambda geometry: not _is_closed_ring(geometry)
    )
    return _write_line_features(source, destination, keep=keep)


def _extended_end(line, start, distance, tail):
    """Координата продолжения конца линии по направлению её хвоста."""
    count = line.GetPointCount()
    if count < 2:
        return None
    index = 0 if start else count - 1
    end_x, end_y = line.GetX(index), line.GetY(index)
    step = 1 if start else -1
    far_x, far_y = end_x, end_y
    walked = 0.0
    while 0 <= index + step < count:
        next_x, next_y = line.GetX(index + step), line.GetY(index + step)
        walked += math.hypot(next_x - far_x, next_y - far_y)
        far_x, far_y = next_x, next_y
        index += step
        if walked >= tail:
            break
    dx, dy = end_x - far_x, end_y - far_y
    length = math.hypot(dx, dy)
    if length <= 0:
        return None
    return (
        end_x + dx / length * distance,
        end_y + dy / length * distance,
    )


def _extend_open_lines(source, destination, distance):
    """Продолжает оба конца каждой незамкнутой линии за пределы охвата.

    Берег, оборванный границей выгрузки внутри охвата, не делит его на
    море и сушу.  Направление берётся по хвосту линии длиной
    ``OCEAN_EXTENSION_TAIL``: у границы выгрузки последние вершины часто
    идут вдоль неё, а не вдоль берега.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    driver = ogr.GetDriverByName(_driver_name(destination))
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        dataset = None
        raise CoastlineError(f"Cannot create {destination}")
    written = 0
    try:
        source_layer = dataset.GetLayer(0)
        if source_layer is None:
            return None
        layer = output.CreateLayer(
            "coastline", source_layer.GetSpatialRef(), geom_type=ogr.wkbLineString
        )
        for feature in source_layer:
            for part in _line_geometries(feature.GetGeometryRef()):
                if _is_closed_ring(part):
                    continue
                points = [part.GetPoint(index)[:2]
                          for index in range(part.GetPointCount())]
                start = _extended_end(
                    part, True, distance, OCEAN_EXTENSION_TAIL
                )
                end = _extended_end(
                    part, False, distance, OCEAN_EXTENSION_TAIL
                )
                if start is None and end is None:
                    continue
                if start is not None:
                    points.insert(0, start)
                if end is not None:
                    points.append(end)
                line = ogr.Geometry(ogr.wkbLineString)
                for x, y in points:
                    line.AddPoint_2D(x, y)
                item = ogr.Feature(layer.GetLayerDefn())
                item.SetGeometry(line)
                layer.CreateFeature(item)
                written += 1
    finally:
        output = None
        dataset = None
    if not written:
        destination.unlink(missing_ok=True)
        return None
    return destination


def _write_island_overlay(island, coastline, destination):
    """Полигоны островов, которых нет среди coastline-объектов.

    Слой ``island`` частично собран из тех же OSM-way, что и ``coastline``:
    они уже вычтены кольцами берега.  Повторно вычитать десятки тысяч
    таких полигонов — значит обрабатывать полосы упрощения длиной в
    миллионы сегментов.  Возвращает ``None``, если лишних островов нет,
    и исходный путь, если в слоях нет ``@id`` и фильтровать нечем.
    """
    from osgeo import ogr

    coastline_dataset = ogr.Open(str(coastline))
    if coastline_dataset is None:
        return Path(island)
    coastline_layer = coastline_dataset.GetLayer(0)
    coastline_definition = coastline_layer.GetLayerDefn()
    coastline_id = coastline_definition.GetFieldIndex("@id")
    if coastline_id < 0:
        coastline_dataset = None
        return Path(island)
    polygon_ids = set()
    for feature in coastline_layer:
        geometry = feature.GetGeometryRef()
        if geometry is not None and geometry.GetDimension() == 2:
            value = feature.GetField(coastline_id)
            # Совсем мелкие кольца упрощение может схлопнуть в ноль.
            # Такие острова оставляем в накладке, иначе они окажутся водой.
            if value is not None and geometry.GetArea() > MIN_POLYGON_AREA:
                polygon_ids.add(value)
    coastline_dataset = None

    dataset = ogr.Open(str(island))
    if dataset is None:
        return Path(island)
    source_layer = dataset.GetLayer(0)
    definition = source_layer.GetLayerDefn()
    island_id = definition.GetFieldIndex("@id")
    if island_id < 0:
        dataset = None
        return Path(island)
    driver = ogr.GetDriverByName(_driver_name(destination))
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        dataset = None
        raise CoastlineError(f"Cannot create {destination}")
    layer = output.CreateLayer(
        "islands", source_layer.GetSpatialRef(), geom_type=ogr.wkbMultiPolygon
    )
    written = 0
    try:
        for feature in source_layer:
            if feature.GetField(island_id) in polygon_ids:
                continue
            geometry = feature.GetGeometryRef()
            if (geometry is None or geometry.IsEmpty()
                    or geometry.GetDimension() != 2):
                continue
            if geometry.GetGeometryName() == "POLYGON":
                geometry = ogr.ForceToMultiPolygon(geometry)
            elif geometry.GetGeometryName() != "MULTIPOLYGON":
                continue
            item = ogr.Feature(layer.GetLayerDefn())
            item.SetGeometry(geometry)
            layer.CreateFeature(item)
            written += 1
    finally:
        output = None
        dataset = None
    if not written:
        destination.unlink(missing_ok=True)
        return None
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


def _layer_is_valid(path):
    """Все ли геометрии слоя корректны.

    Слой ``island`` рецепт уже чинит перед сохранением. Повторная
    ``fixgeometries`` переписывает десятки мегабайт впустую, поэтому
    чинить его ещё раз нужно только при невалидных объектах — например,
    в проекте, собранном старой версией плагина.
    """
    try:
        from osgeo import ogr
    except ImportError:
        return False
    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return False
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return False
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None and not geometry.IsValid():
                return False
        return True
    except Exception:
        return False
    finally:
        dataset = None


def _count_features(path):
    """Число объектов слоя."""
    from osgeo import ogr

    try:
        dataset = ogr.Open(str(path))
    except Exception:
        dataset = None
    if dataset is None:
        return 0
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return 0
        return sum(1 for _feature in layer)
    except Exception:
        return 0
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

    from .pipeline import _prepared_geometry

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
            # Океан — один полигон с десятками тысяч дыр.  Без
            # подготовленной геометрии Intersects строит индекс рёбер
            # заново на каждый остров, и проверка идёт часами.
            prepared = _prepared_geometry(geometry)
            second_layer.SetSpatialFilter(geometry)
            for other in second_layer:
                other_geometry = other.GetGeometryRef()
                if (other_geometry is None or other_geometry.IsEmpty()
                        or other_geometry.GetDimension() != 2):
                    continue
                if prepared is not None:
                    intersects = prepared.Intersects(other_geometry)
                else:
                    intersects = geometry.Intersects(other_geometry)
                if not intersects:
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
    driver = ogr.GetDriverByName(_driver_name(destination))
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
    driver = ogr.GetDriverByName(_driver_name(destination))
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


def _line_length(path):
    """Суммарная длина линейных объектов слоя."""
    from osgeo import ogr

    dataset = ogr.Open(str(path))
    if dataset is None:
        return 0.0
    total = 0.0

    def add(geometry):
        nonlocal total
        if geometry is None or geometry.IsEmpty():
            return
        name = geometry.GetGeometryName()
        if name in ("LINESTRING", "LINEARRING"):
            total += geometry.Length()
        elif name in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
            for index in range(geometry.GetGeometryCount()):
                add(geometry.GetGeometryRef(index))

    try:
        layer = dataset.GetLayer(0)
        if layer is not None:
            for feature in layer:
                add(feature.GetGeometryRef())
    finally:
        dataset = None
    return total


def _ocean_point_spacing(length):
    """Шаг точек вдоль берега: фиксированное число точек на любой охват."""
    return min(
        max(length / OCEAN_POINT_TARGET, OCEAN_POINT_MIN),
        OCEAN_POINT_MAX,
    )


def _counted_faces(source):
    """Голоса и площади частей охвата из слоя с полем ``vcount``.

    Возвращает ``(faces, total_votes, total_area)``; ``faces`` — список
    пар «голоса, площадь».  Площадь считается планарно в единицах слоя:
    важна не абсолютная величина, а отношение плотности частей.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return [], 0, 0.0
        index = layer.GetLayerDefn().GetFieldIndex("vcount")
        if index < 0:
            raise CoastlineError(f"{source} has no vcount field")
        faces = []
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            if geometry.GetDimension() != 2:
                continue
            faces.append((
                feature.GetField(index) or 0,
                _geometry_area(geometry),
            ))
    finally:
        dataset = None
    return (
        faces,
        sum(votes for votes, _area in faces),
        sum(area for _votes, area in faces),
    )


def _faces_by_votes(source, destination, threshold, min_density=0.0):
    """Записать части охвата, набравшие достаточно голосов.

    Голос — береговая точка на водной стороне берега.  Порог по числу
    голосов отсекает случайные попадания, а порог по плотности — крупную
    сушу: единичные точки от обратных way и от смещения поперёк узких
    проток теряются на её площади, тогда как у воды точки стоят вдоль
    всего берега.
    """
    from osgeo import ogr

    dataset = ogr.Open(str(source))
    if dataset is None:
        raise CoastlineError(f"Cannot open {source}")
    driver = ogr.GetDriverByName(_driver_name(destination))
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        dataset = None
        raise CoastlineError(f"Cannot create {destination}")
    written = 0
    try:
        source_layer = dataset.GetLayer(0)
        if source_layer is None:
            return None
        source_definition = source_layer.GetLayerDefn()
        index = source_definition.GetFieldIndex("vcount")
        if index < 0:
            raise CoastlineError(f"{source} has no vcount field")
        layer = output.CreateLayer(
            "ocean", source_layer.GetSpatialRef(), geom_type=ogr.wkbMultiPolygon
        )
        definition = layer.GetLayerDefn()
        fields = [
            (position, source_definition.GetFieldDefn(position).GetName())
            for position in range(source_definition.GetFieldCount())
        ]
        for feature in source_layer:
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty() or geometry.GetDimension() != 2:
                continue
            votes = feature.GetField(index) or 0
            if votes < threshold:
                continue
            area = _geometry_area(geometry)
            if area <= MIN_POLYGON_AREA:
                continue
            if min_density > 0 and votes / area < min_density:
                continue
            if geometry.GetGeometryName() == "POLYGON":
                geometry = ogr.ForceToMultiPolygon(geometry)
            item = ogr.Feature(definition)
            item.SetGeometry(geometry)
            for source_position, name in fields:
                target_position = definition.GetFieldIndex(name)
                if target_position >= 0:
                    item.SetField(
                        target_position, feature.GetField(source_position)
                    )
            layer.CreateFeature(item)
            written += 1
    finally:
        output = None
        dataset = None
    if not written:
        destination.unlink(missing_ok=True)
        return None
    return destination


def _side_ocean(runner, config, split, open_source, side):
    """Части охвата, лежащие по указанную сторону берега.

    Входные линии не растворяются: ``native:dissolve`` меняет
    направление части линий, и сторона воды перестаёт быть той, которую
    задаёт OSM (вода справа по ходу линии).  Направление исходных
    незамкнутых ways сохраняется, поэтому смещённая линия идёт по воде.
    """
    tag = "right" if side else "left"
    # У offsetline положительное расстояние смещает влево, отрицательное —
    # вправо; side=1 означает воду справа.
    distance = SIDE_OFFSET if not side else -SIDE_OFFSET
    offset = _run(runner, "native:offsetline", {
        "INPUT": str(open_source),
        "DISTANCE": distance,
        "SEGMENTS": 5,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2,
        "OUTPUT": _temp(config, f"coastline_side_{tag}.gpkg"),
    })
    if not _has_features(offset):
        return None
    clipped = _run(runner, "native:clip", {
        "INPUT": str(offset),
        "OVERLAY": str(config.crop_path),
        "OUTPUT": _temp(config, f"coastline_side_{tag}_clipped.gpkg"),
    })
    if not _has_features(clipped):
        return None
    length = _line_length(clipped)
    if length <= 0:
        return None
    # Вдоль смещённой линии ставятся равномерные точки.  Их число
    # фиксировано, поэтому стоимость не растёт с длиной берега, а голоса
    # частей охвата пропорциональны длине их берега.  Одна длинная way
    # фьорда получает сотни точек, а не одну.
    points = _run(runner, "native:pointsalonglines", {
        "INPUT": str(clipped),
        "DISTANCE": _ocean_point_spacing(length),
        "START_OFFSET": 0,
        "END_OFFSET": 0,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_points.gpkg"),
    })
    # Части охвата выбираются по числу попавших в них береговых точек, а
    # не по «содержит хотя бы одну»: единичные way с обратным направлением
    # дают точку на суше, и одного такого попадания хватало, чтобы в океан
    # попала вся материковая часть охвата.
    counted = _run(runner, "native:countpointsinpolygon", {
        "POLYGONS": split,
        "POINTS": str(points),
        "FIELD": "vcount",
        "OUTPUT": _temp(config, f"coastline_side_{tag}_count.gpkg"),
    })
    faces, total, area = _counted_faces(counted)
    if not faces or total <= 0:
        return None
    threshold = max(1, int(total * OCEAN_POINT_SHARE))
    # Плотность голосов на единицу площади: у воды она близка к средней
    # по охвату, у крупной суши — в разы ниже.  Сравнение с собственной
    # средней делает порог независимым от длины берега и размера области.
    min_density = (
        total / area * OCEAN_MIN_DENSITY_SHARE if area > 0 else 0.0
    )
    return _faces_by_votes(
        counted,
        _temp(config, f"coastline_ocean_{tag}.gpkg"),
        threshold,
        min_density,
    )


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
        line_source = _write_lines(lines, config.temp_dir / "coastline_lines.gpkg")
        dissolved = _run(runner, "native:dissolve", {
            "INPUT": str(line_source),
            "FIELD": [],
            "SEPARATE_DISJOINT": False,
            "OUTPUT": _temp(config, "coastline_dissolved.gpkg"),
        })
        simplified = _run(runner, "native:simplifygeometries", {
            "INPUT": dissolved,
            "METHOD": 0,
            "TOLERANCE": 0.00005,
            "OUTPUT": _temp(config, "coastline_simplified.gpkg"),
        })

        notify("Береговая линия: разделение охвата")
        # Замкнутые кольца островов не режут охват: splitwithlines
        # прогоняет каждое кольцо по всем уже полученным частям, и на
        # архипелаге это квадратично.  Кольца вычитаются площадями, а
        # охват режется только незамкнутым берегом.
        open_parts = _write_selected_lines(
            simplified,
            config.temp_dir / "coastline_open_parts.gpkg",
            closed=False,
        )
        if open_parts is not None:
            # Берег может закончиться внутри охвата: страны/выгрузки
            # обрывают coastline на границе, и такая линия не делит
            # прямоугольник на море и сушу.  Продлеваем концы по
            # направлению хвоста линии и обрезаем по рамке, чтобы
            # каждый открытый берег дошёл до её края.
            if bbox is not None:
                extension = max(
                    bbox.lon_max - bbox.lon_min,
                    bbox.lat_max - bbox.lat_min,
                ) * 2
            else:
                extension = 2.0
            open_parts = _extend_open_lines(
                open_parts,
                config.temp_dir / "coastline_open_extended.gpkg",
                extension,
            )
            if open_parts is not None:
                open_parts = _run(runner, "native:clip", {
                    "INPUT": str(open_parts),
                    "OVERLAY": str(crop),
                    "OUTPUT": _temp(config, "coastline_open_clipped.gpkg"),
                })
            if open_parts is None or not _has_features(open_parts):
                open_parts = None
        rings = _write_selected_lines(
            simplified, config.temp_dir / "coastline_rings.gpkg", closed=True
        )
        ring_polygons = None
        if rings is not None:
            ring_polygons = _run(runner, "native:polygonize", {
                "INPUT": str(rings),
                "KEEP_FIELDS": False,
                "OUTPUT": _temp(config, "coastline_rings_polygons.gpkg"),
            })
            ring_polygons = _run(runner, "native:fixgeometries", {
                "INPUT": ring_polygons,
                "OUTPUT": _temp(config, "coastline_rings_fixed.gpkg"),
            })
        # Промежуточные слои — GeoPackage: он держит крупные геометрии и
        # имеет пространственный индекс.
        split = str(crop)
        if open_parts is not None:
            # splitwithlines режет охват каждой линией и на длинном берегу
            # с десятками тысяч вершин уходит в минуты.  Unary union за один
            # проход делает линии узлованными, а polygonize строит все части
            # охвата сразу; на берегу Финляндии это доли секунды.
            boundary = _run(runner, "native:boundary", {
                "INPUT": str(crop),
                "OUTPUT": _temp(config, "coastline_crop_boundary.gpkg"),
            })
            graph = _run(runner, "native:mergevectorlayers", {
                "LAYERS": [str(open_parts), boundary],
                "OUTPUT": _temp(config, "coastline_graph.gpkg"),
            })
            graph = _run(runner, "native:dissolve", {
                "INPUT": graph,
                "FIELD": [],
                "SEPARATE_DISJOINT": False,
                "OUTPUT": _temp(config, "coastline_graph_noded.gpkg"),
            })
            split = _run(runner, "native:polygonize", {
                "INPUT": graph,
                "KEEP_FIELDS": False,
                "OUTPUT": _temp(config, "coastline_split.gpkg"),
            })
        if ring_polygons is not None:
            split = _run(runner, "native:difference", {
                "INPUT": split,
                "OVERLAY": ring_polygons,
                "OUTPUT": _temp(config, "coastline_rings_diff.gpkg"),
            })
            split = _run(runner, "native:multiparttosingleparts", {
                "INPUT": split,
                "OUTPUT": _temp(config, "coastline_rings_parts.gpkg"),
            })

        notify("Береговая линия: определение стороны океана")
        # Сторону океана показывают только незамкнутые линии берега:
        # замкнутые кольца — это острова, и по ним сторона выходит неверной
        # (в дельте Дуная океаном оказывался крошечный обломок у края
        # области, а море не заливалось вовсе).
        island = config.vector_dir / "island.sqlite"
        island_count = _count_features(island) if island.exists() else 0
        # Крупный слой островов делает _looks_like_land дороже самого
        # океана, а его сигнал в архипелаге ненадёжен: острова без
        # coastline-кольца лежат внутри моря и дают «пересечение» с ним.
        check_islands = (
            island.exists()
            and island_count <= LOOKS_LIKE_LAND_MAX_ISLANDS
        )
        ocean = None
        open_source = None
        if open_parts is not None:
            # Буфер строится по исходным, нерастворённым ways: dissolve
            # меняет направление части линий, и сторона воды теряется.
            open_source = _write_lines(
                lines,
                config.temp_dir / "coastline_open_lines.gpkg",
                open_only=True,
            )
            ocean = _side_ocean(runner, config, split, open_source, 1)
        if (ocean is not None and _has_features(ocean)
                and (not check_islands
                     or not _looks_like_land(ocean, island))):
            pass
        else:
            if ocean is not None and _has_features(ocean):
                notify(
                    "WARNING: Береговая линия: сторона океана определена "
                    "неверно, повтор с другой стороны"
                )
            retried = None
            if open_source is not None:
                retried = _side_ocean(runner, config, split, open_source, 0)
            if (retried is not None and _has_features(retried)
                    and (not check_islands
                         or not _looks_like_land(retried, island))):
                ocean = retried
            else:
                # Незамкнутого берега нет — охват уже разрезан кольцами,
                # и океан это крупнейшая часть. Тот же запасной вариант
                # нужен, когда обе стороны не прошли проверку.
                ocean = _largest_polygon(
                    split, _temp(config, "coastline_ocean_largest.gpkg")
                )
                if ocean is not None and open_source is not None:
                    notify(
                        "WARNING: Береговая линия: океан взят как "
                        "крупнейшая часть охвата"
                    )
        if ocean is None or not _has_features(ocean):
            notify("WARNING: Береговая линия: океан не построен")
            return False
        if not _layer_is_valid(ocean):
            ocean = _run(runner, "native:fixgeometries", {
                "INPUT": ocean,
                "OUTPUT": _temp(config, "coastline_ocean_fixed.gpkg"),
            })
        ocean = _run(runner, "native:fieldcalculator", {
            "INPUT": ocean,
            "FIELD_NAME": "natural",
            "FIELD_TYPE": 2,
            "FIELD_LENGTH": 20,
            "FIELD_PRECISION": 0,
            "FORMULA": "'water'",
            "OUTPUT": _temp(config, "coastline_ocean_water.gpkg"),
        })

        if island.exists() and _has_features(island):
            notify("Береговая линия: вычитание островов")
            island_source = str(island)
            if island_count > ISLAND_ID_FILTER_MIN_FEATURES:
                filtered = _write_island_overlay(
                    island,
                    lines,
                    config.temp_dir / "coastline_islands_extra.gpkg",
                )
                island_source = str(filtered) if filtered is not None else None
            if island_source is None:
                notify("Береговая линия: острова уже в кольцах берега")
            else:
                if (island_source == str(island)
                        and not _layer_is_valid(island)):
                    island_source = _run(runner, "native:fixgeometries", {
                        "INPUT": island_source,
                        "OUTPUT": _temp(config, "coastline_islands_fixed.gpkg"),
                    })
                # Полигон материка может быть больше охвата на порядки.
                # Difference с целой Гренландией на узком охвате занимает
                # секунды, а с обрезанным по рамке островом — сотые доли.
                island_source = _run(runner, "native:clip", {
                    "INPUT": island_source,
                    "OVERLAY": str(crop),
                    "OUTPUT": _temp(config, "coastline_islands_clipped.gpkg"),
                })
                if not _has_features(island_source):
                    notify("Береговая линия: острова вне охвата")
                else:
                    ocean = _run(runner, "native:difference", {
                        "INPUT": ocean,
                        "OVERLAY": island_source,
                        "OUTPUT": _temp(config, "coastline_ocean_islands.gpkg"),
                    })

        water = config.vector_dir / "water_dissolved.sqlite"
        existing = None
        if water.exists() and _has_features(water):
            existing = _write_polygons(
                water, config.temp_dir / "coastline_existing_water.gpkg"
            )
        if existing is not None:
            notify("Береговая линия: объединение с внутренними водами")
            merged = _run(runner, "native:mergevectorlayers", {
                "LAYERS": [str(existing), ocean],
                "OUTPUT": _temp(config, "coastline_merged.gpkg"),
            })
            # Починка нужна перед разбиением: во внутренних водах
            # попадается полигон с самопересечением, из-за которого
            # native:multiparttosingleparts может упасть.
            merged = _run(runner, "native:fixgeometries", {
                "INPUT": merged,
                "OUTPUT": _temp(config, "coastline_merged_fixed.gpkg"),
            })
            # Не растворяем океан с внутренними водами.  Озеро у берега
            # касается океана и после растворения становится частью его
            # полигона; затем вырезание пересечений вычитает из большого
            # полигона остров — вместе с ним пропадает и озеро.  Слою
            # нужен один набор объектов, а не одна геометрия.
            ocean = merged

        final = _run(runner, "native:multiparttosingleparts", {
            "INPUT": ocean,
            "OUTPUT": _temp(config, "coastline_water_parts.gpkg"),
        })
        final = _write_polygons(
            final, config.temp_dir / "coastline_water_polygons.gpkg"
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
