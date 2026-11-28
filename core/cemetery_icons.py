"""Точки значков кладбищ: собираются при подготовке данных.

Значок кладбища рисуется отдельным слоем над картой.  Пока он был
вложен в полигон (CentroidFill), он не мог вытесняться другими
значками: фильтр правила скрыл бы вместе с ним всю площадку.  Поэтому
при подготовке из площадок строится точечный слой
``cemetery_icons.sqlite``: точка ставится там же, где рисовался
CentroidFill (``pointOnSurface``), а угол поворота значка считается по
ориентированному минимальному прямоугольнику и лежит в поле
``icon_angle``.

Дальше это обычный слой POI: поле ``hide`` с порогами подавления,
вытеснение населёнными пунктами и остальными значками.  Слой шаблона
стоит выше карты, поэтому полигоны и линии его не перекрывают.
"""

from pathlib import Path

from . import i18n

SOURCE_NAME = "cemetery"
TARGET_NAME = "cemetery_icons"
TARGET_FILE = "cemetery_icons.sqlite"
ANGLE_FIELD = "icon_angle"
# Поля прошлых сборок и идентификаторы объекта: в новом файле они не
# нужны, служебные значения посчитает сам конвейер значков.
SKIPPED_FIELDS = frozenset({
    "ogc_fid", "fid", "objectid", "hide", "icon_hide", ANGLE_FIELD,
})


def build_cemetery_icons(config, progress=None, cancel=None):
    """Строит точечный слой значков кладбищ из площадок.

    Возвращает число точек.  Файл пишется во временный и подменяет
    готовый только после успеха, поэтому сбой не оставляет слой
    наполовину записанным.
    """
    report = progress or (lambda message: None)
    source_path = Path(config.vector_dir) / f"{SOURCE_NAME}.sqlite"
    target_path = Path(config.vector_dir) / TARGET_FILE
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
        target = dataset.CreateLayer(TARGET_NAME, srs, ogr.wkbPoint)
        copied, area_index = _copy_fields(layer, target)
        target_definition = target.GetLayerDefn()
        angle_index = target_definition.GetFieldIndex(ANGLE_FIELD)

        count = 0
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
                point = geometry.PointOnSurface()
                if point is None or point.IsEmpty():
                    continue
                target_feature = ogr.Feature(target_definition)
                for source_index, target_index in copied:
                    target_feature.SetField(
                        target_index, feature.GetField(source_index),
                    )
                target_feature.SetField(
                    area_index, _area(feature, geometry, srs),
                )
                target_feature.SetField(
                    angle_index, _main_angle(geometry),
                )
                target_feature.SetGeometry(point)
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
    report(i18n.trf("Значки кладбищ: {} точек", count))
    return count


def _copy_fields(source_layer, target_layer):
    """Копирует поля площадки в точечный слой.

    Возвращает пары индексов «источник — цель» и индекс поля
    ``area_m2``.  Если площади в источнике ещё нет, она будет
    посчитана по геометрии при записи каждой точки.
    """
    from osgeo import ogr

    definition = source_layer.GetLayerDefn()
    copied = []
    area_index = -1
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
        if name.lower() == "area_m2":
            area_index = target_index
    if area_index < 0:
        target_layer.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
        area_index = target_layer.GetLayerDefn().GetFieldIndex("area_m2")
    target_layer.CreateField(ogr.FieldDefn(ANGLE_FIELD, ogr.OFTInteger))
    return copied, area_index


def _area(feature, geometry, srs):
    """Площадь площадки в квадратных метрах.

    Берётся готовое поле ``area_m2``, а если его нет — планарная
    площадь геометрии с переводом единиц слоя в метры.
    """
    index = feature.GetFieldIndex("area_m2")
    if index >= 0 and feature.IsFieldSetAndNotNull(index):
        return float(feature.GetField(index))
    from qgis.core import QgsGeometry

    shape = QgsGeometry()
    shape.fromWkb(bytes(geometry.ExportToWkb()))
    if shape.isEmpty():
        return 0.0
    if srs is not None and srs.IsGeographic():
        factor = 111319.49079327357 ** 2
    elif srs is not None:
        factor = srs.GetLinearUnits() ** 2
    else:
        factor = 1.0
    return shape.area() * factor


def _main_angle(geometry):
    """Угол поворота значка по ориентированному прямоугольнику."""
    from qgis.core import QgsGeometry

    shape = QgsGeometry()
    shape.fromWkb(bytes(geometry.ExportToWkb()))
    if shape.isEmpty():
        return 0
    try:
        box = shape.orientedMinimumBoundingBox()
    except Exception:  # noqa: BLE001 - значок без поворота лучше пропуска
        return 0
    if not box or box[0] is None or box[0].isNull():
        return 0
    return int(round(box[2] or 0.0))
