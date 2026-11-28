"""Точки значков мостов: собираются при подготовке данных.

Значок моста раньше рисовался ``MarkerLine`` в середине линии, поэтому
фильтр правила не мог скрыть только значок: вместе с ним пропала бы вся
линия моста.  Такие значки участвовали в вытеснении лишь как
препятствия и не уступали всем POI.  Теперь при подготовке из линий
строятся обычные точечные слои: точка ставится там же, где рисовался
``CentralPoint``, угол поворота линии лежит в поле ``icon_angle``, а
длина — в ``bridge_length`` (правило железнодорожных мостов сравнивает
её с 200).

Дальше это обычные слои POI: поле ``hide`` с порогами подавления,
вытеснение всеми остальными значками.  Слои шаблона стоят рядом с
линиями мостов, поэтому значки рисуются над ними.
"""

from pathlib import Path

from . import i18n

ANGLE_FIELD = "icon_angle"
LENGTH_FIELD = "bridge_length"
# Поля прошлых сборок и идентификаторы объекта: в новом файле они не
# нужны, служебные значения посчитает сам конвейер значков.
SKIPPED_FIELDS = frozenset({
    "ogc_fid", "fid", "objectid", "hide", "icon_hide", ANGLE_FIELD,
    LENGTH_FIELD,
})

# (исходный файл, точечный файл, имя слоя в шаблоне)
SOURCES = (
    (
        "highway_main_bridge_dissolved.sqlite",
        "highway_main_bridge_icons.sqlite",
        "highway main bridge icons",
    ),
    ("track_bridge.sqlite", "track_bridge_icons.sqlite",
     "track bridge icons"),
    ("path_bridge.sqlite", "path_bridge_icons.sqlite",
     "path bridge icons"),
    ("rail_bridge.sqlite", "rail_bridge_icons.sqlite",
     "railway bridge icons"),
)


def build_bridge_icons(config, progress=None, cancel=None):
    """Строит точечные слои значков мостов из линий.

    Возвращает число точек.  Файл пишется во временный и подменяет
    готовый только после успеха, поэтому сбой не оставляет слой
    наполовину записанным.
    """
    report = progress or (lambda message: None)
    total = 0
    for source_name, target_name, layer_name in SOURCES:
        total += _build_one(
            config, source_name, target_name, layer_name,
            report, cancel,
        )
    if total:
        report(i18n.trf("Значки мостов: {} точек", total))
    return total


def _build_one(config, source_name, target_name, layer_name,
               report, cancel):
    """Точечный слой одного вида мостов; 0, если источника нет."""
    source_path = Path(config.vector_dir) / source_name
    target_path = Path(config.vector_dir) / target_name
    if not source_path.exists():
        target_path.unlink(missing_ok=True)
        return 0

    from osgeo import ogr, osr

    ogr.UseExceptions()
    source = ogr.Open(str(source_path))
    if source is None:
        target_path.unlink(missing_ok=True)
        return 0
    temporary = target_path.with_name(f"{target_path.stem}.part.sqlite")
    temporary.unlink(missing_ok=True)
    dataset = None
    target = None
    count = 0
    try:
        layer = source.GetLayer(0)
        if layer is None:
            target_path.unlink(missing_ok=True)
            return 0
        srs = layer.GetSpatialRef()
        if srs is None:
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
        driver = ogr.GetDriverByName("SQLite")
        dataset = driver.CreateDataSource(str(temporary))
        target = dataset.CreateLayer(target_name, srs, ogr.wkbPoint)
        copied, angle_index, length_index = _copy_fields(layer, target)
        definition = target.GetLayerDefn()

        target.StartTransaction()
        try:
            for feature in layer:
                if (
                    cancel is not None
                    and count % 500 == 0
                    and cancel.is_set()
                ):
                    raise InterruptedError("Preparation cancelled")
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                kind = ogr.GT_Flatten(geometry.GetGeometryType())
                if kind not in (
                    ogr.wkbLineString, ogr.wkbMultiLineString,
                ):
                    continue
                for part in _line_parts(geometry):
                    anchor = _anchor(part)
                    if anchor is None:
                        continue
                    point, angle = anchor
                    length = part.Length()
                    target_feature = ogr.Feature(definition)
                    for source_index, target_index in copied:
                        target_feature.SetField(
                            target_index, feature.GetField(source_index),
                        )
                    target_feature.SetField(angle_index, angle)
                    target_feature.SetField(length_index, length)
                    shape = ogr.Geometry(ogr.wkbPoint)
                    shape.AddPoint_2D(point[0], point[1])
                    target_feature.SetGeometry(shape)
                    target.CreateFeature(target_feature)
                    count += 1
            target.CommitTransaction()
        except Exception:
            target.RollbackTransaction()
            raise
    except Exception:
        target = None
        dataset = None
        temporary.unlink(missing_ok=True)
        raise
    finally:
        source = None

    target = None
    dataset = None
    if not count:
        temporary.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        return 0
    temporary.replace(target_path)
    report(i18n.trf("{}: {} точек", layer_name, count))
    return count


def _copy_fields(source_layer, target_layer):
    """Копирует поля линии в точечный слой.

    Возвращает пары индексов «источник — цель» и индексы полей угла и
    длины.  Поля прошлых сборок не копируются, иначе старое значение
    ``hide`` пережило бы пересборку.
    """
    from osgeo import ogr

    definition = source_layer.GetLayerDefn()
    copied = []
    for source_index in range(definition.GetFieldCount()):
        field = definition.GetFieldDefn(source_index)
        name = field.GetName()
        if name.lower() in SKIPPED_FIELDS:
            continue
        target_field = ogr.FieldDefn(name, field.GetType())
        if field.GetWidth():
            target_field.SetWidth(field.GetWidth())
        if field.GetPrecision():
            target_field.SetPrecision(field.GetPrecision())
        target_layer.CreateField(target_field)
        target_index = target_layer.GetLayerDefn().GetFieldIndex(name)
        copied.append((source_index, target_index))
    target_layer.CreateField(ogr.FieldDefn(ANGLE_FIELD, ogr.OFTInteger))
    target_layer.CreateField(ogr.FieldDefn(LENGTH_FIELD, ogr.OFTReal))
    definition = target_layer.GetLayerDefn()
    return (
        copied,
        definition.GetFieldIndex(ANGLE_FIELD),
        definition.GetFieldIndex(LENGTH_FIELD),
    )


def _line_parts(geometry):
    """Части линии: у MultiLineString значок рисуется на каждой."""
    from osgeo import ogr

    kind = ogr.GT_Flatten(geometry.GetGeometryType())
    if kind == ogr.wkbLineString:
        yield geometry
        return
    if kind != ogr.wkbMultiLineString:
        # В растворённом слое встречаются замкнутые площадки; у них
        # значка моста нет.
        return
    for index in range(geometry.GetGeometryCount()):
        part = geometry.GetGeometryRef(index)
        if part is not None and not part.IsEmpty():
            yield part


def _anchor(part):
    """Точка середины линии и угол её сегмента.

    ``MarkerLine`` с ``CentralPoint`` рисует значок на половине длины
    части.  Угол считается как у QGIS: в координатах экрана ось Y
    направлена вниз, поэтому знак разности широт меняется.
    """
    import math

    length = part.Length()
    if length <= 0:
        return None
    middle = length / 2.0
    value = part.Value(middle)
    if value is None:
        return None
    if hasattr(value, "GetX"):
        point = (value.GetX(), value.GetY())
    else:
        point = tuple(value)[:2]
    walked = 0.0
    count = part.GetPointCount()
    for index in range(count - 1):
        first = part.GetPoint(index)
        second = part.GetPoint(index + 1)
        segment = math.hypot(
            second[0] - first[0], second[1] - first[1],
        )
        if segment <= 0:
            continue
        if walked + segment >= middle or index == count - 2:
            angle = math.degrees(math.atan2(
                -(second[1] - first[1]), second[0] - first[0],
            ))
            angle = ((angle + 180.0) % 360.0) - 180.0
            return point[:2], int(round(angle))
        walked += segment
    return point[:2], 0
