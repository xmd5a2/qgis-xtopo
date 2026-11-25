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

# Доля площади водной полосы, ниже которой часть охвата считается сушей.
# Случайно попавшая на сушу короткая обратная way даёт доли процента, а
# настоящий берег — заметно больше.  Считать площадь, а не точки, нужно
# потому, что длинный фьорд может быть обведён одной way: pointonsurface
# даёт для него одну-две точки, и порог по их числу отбрасывал залив.
OCEAN_STRIP_SHARE = 0.01


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


def _faces_with_strip(split, strip, destination):
    """Полигоны охвата, вдоль берега которых проходит водная полоса.

    Полоса узкая, поэтому площадь её пересечения с полигоном
    пропорциональна длине берега.  Длинный фьорд, обведённый одной
    OSM-way, даёт всего две точки ``pointonsurface``; порог по числу
    точек отбрасывал его целиком, а по площади — нет.
    """
    from osgeo import ogr

    from .pipeline import _prepared_geometry

    split_dataset = ogr.Open(str(split))
    strip_dataset = ogr.Open(str(strip))
    if split_dataset is None or strip_dataset is None:
        if split_dataset is not None:
            split_dataset = None
        if strip_dataset is not None:
            strip_dataset = None
        raise CoastlineError(f"Cannot open {split} or {strip}")
    split_layer = split_dataset.GetLayer(0)
    strip_layer = strip_dataset.GetLayer(0)
    if split_layer is None or strip_layer is None:
        strip_dataset = None
        split_dataset = None
        raise CoastlineError(f"Empty layer in {split} or {strip}")
    spatial_ref = split_layer.GetSpatialRef()
    totals = {}
    prepared_faces = {}
    try:
        for strip_feature in strip_layer:
            strip_geometry = strip_feature.GetGeometryRef()
            if (strip_geometry is None or strip_geometry.IsEmpty()
                    or strip_geometry.GetDimension() != 2):
                continue
            split_layer.SetSpatialFilter(strip_geometry)
            for face in split_layer:
                face_geometry = face.GetGeometryRef()
                if (face_geometry is None or face_geometry.IsEmpty()
                        or face_geometry.GetDimension() != 2):
                    continue
                fid = face.GetFID()
                if fid not in prepared_faces:
                    prepared_faces[fid] = _prepared_geometry(face_geometry)
                prepared = prepared_faces[fid]
                try:
                    if prepared is not None:
                        contains = prepared.Contains(strip_geometry)
                        intersects = contains or prepared.Intersects(strip_geometry)
                    else:
                        contains = face_geometry.Contains(strip_geometry)
                        intersects = contains or face_geometry.Intersects(
                            strip_geometry
                        )
                    if contains:
                        # Полоса целиком внутри части охвата: пересечение
                        # строить не нужно, его площадь равна площади полосы.
                        area = _geometry_area(strip_geometry)
                    elif intersects:
                        area = _geometry_area(
                            face_geometry.Intersection(strip_geometry)
                        )
                    else:
                        continue
                except Exception:
                    continue
                if area > 0:
                    totals[fid] = totals.get(fid, 0.0) + area
        split_layer.SetSpatialFilter(None)
    finally:
        strip_dataset = None
    total = sum(totals.values())
    threshold = max(MIN_POLYGON_AREA, total * OCEAN_STRIP_SHARE)
    selected = {fid for fid, area in totals.items() if area >= threshold}
    if not selected:
        split_dataset = None
        return None

    driver = ogr.GetDriverByName(_driver_name(destination))
    destination = Path(destination)
    destination.unlink(missing_ok=True)
    output = driver.CreateDataSource(str(destination))
    if output is None:
        split_dataset = None
        raise CoastlineError(f"Cannot create {destination}")
    written = 0
    try:
        layer = output.CreateLayer(
            "ocean", spatial_ref, geom_type=ogr.wkbMultiPolygon
        )
        split_layer.ResetReading()
        for face in split_layer:
            if face.GetFID() not in selected:
                continue
            geometry = face.GetGeometryRef()
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
        split_dataset = None
    if not written:
        destination.unlink(missing_ok=True)
        return None
    return destination


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
        "OUTPUT": _temp(config, f"coastline_side_{tag}.gpkg"),
    })
    thin = _run(runner, "native:buffer", {
        "INPUT": one_side,
        "DISTANCE": -SIDE_BUFFER_INSET,
        "SEGMENTS": 5,
        "END_CAP_STYLE": 0,
        "JOIN_STYLE": 0,
        "MITER_LIMIT": 2,
        "DISSOLVE": False,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_thin.gpkg"),
    })
    parts = _run(runner, "native:multiparttosingleparts", {
        "INPUT": thin,
        "OUTPUT": _temp(config, f"coastline_side_{tag}_parts.gpkg"),
    })
    clipped = _run(runner, "native:intersection", {
        "INPUT": parts,
        "OVERLAY": str(config.crop_path),
        "OUTPUT": _temp(config, f"coastline_side_{tag}_clipped.gpkg"),
    })
    # Часть охвата выбирается по площади попавшей в неё водной полосы.
    # Полоса узкая, поэтому площадь пропорциональна длине берега: длинный
    # фьорд, обведённый одной way, не теряется из-за того, что
    # pointonsurface даёт на него всего одну-две точки.
    return _faces_with_strip(
        split, clipped, _temp(config, f"coastline_ocean_{tag}.gpkg")
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
