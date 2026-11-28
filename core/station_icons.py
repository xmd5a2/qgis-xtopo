"""Значки железнодорожных станций: собираются при подготовке данных.

Значок станции ставится в точку самой станции OSM и поворачивается по
направлению рельсов.  Значок вокзала (`railway_station_building.svg`)
ставится, когда выполнены три условия:

* станция не подземная: нет `location=underground`, `tunnel=yes`,
  `subway=yes` и отрицательных `layer` или `level`;
* здание вокзала (`building=train_station`) стоит не дальше 100 м;
* станция стоит на рельсах, и рельсы наземные: слой `railway_all`
  содержит только линии без туннелей.

Раньше значок выбирался выражением в шаблоне, а угол считался
выражением с ``overlay_nearest``: QGIS при каждой отрисовке строил
индекс всего слоя рельсов и вокзалов.  Сравнение расстояния шло в
градусах — функция ``distance`` не переводит системы координат, — и
порог 500 м срабатывал всегда: значок вокзала получали все станции, а
сторона значка поворачивалась к ближайшему зданию на другом конце карты.

Теперь тег значка и угол пишутся в поля при подготовке, а в шаблоне
остаётся только обращение к полю.  Дальше это обычный слой POI: поле
``hide`` с порогами подавления, вытеснение населёнными пунктами и
остальными значками.
"""

import math
from pathlib import Path

from . import i18n

# Слой точек станций из OSM и готовый слой значков.
SOURCE_NAME = "railway_station_icons_raw"
TARGET_NAME = "railway_station_icons"
TARGET_FILE = "railway_station_icons.sqlite"
# Поле с углом поворота и поле с тегом значка.
ANGLE_FIELD = "icon_angle"
ICON_FIELD = "icon"

# Значки с путём относительно файла проекта: так же, как в шаблоне.
STATION_ICON = "../icons/railway_station.svg"
BUILDING_ICON = "../icons/railway_station_building.svg"

# Здание вокзала не дальше этого расстояния от станции.
BUILDING_MAX_DISTANCE = 100.0
# Насколько близко станция должна стоять к рельсам, чтобы считаться
# стоящей на них.
RAIL_MAX_DISTANCE = 25.0

# Слои-источники: наземные рельсы и здания вокзалов.  Оба лежат в
# EPSG:3857, поэтому расстояния считаются в метрах проекции.
RAIL_FILE = "railway_all_exploded.sqlite"
BUILDING_FILE = "building_train_station_reproj.sqlite"
METRIC_CRS = "EPSG:3857"

# Положения объекта, которые считаются подземными.
UNDERGROUND_LOCATIONS = frozenset({"underground", "tunnel"})

# Поля прошлых сборок и идентификаторы объекта: в новом файле они не
# нужны, служебные значения посчитает сам конвейер значков.
SKIPPED_FIELDS = frozenset({
    "ogc_fid", "fid", "objectid", "hide", "icon_hide", ANGLE_FIELD,
    ICON_FIELD,
})


def build_station_icons(config, progress=None, cancel=None):
    """Строит слой значков станций из точек станций OSM.

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

    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsCoordinateTransform,
        QgsGeometry,
        QgsProject,
    )

    from . import measure

    vector_dir = Path(config.vector_dir)
    rails = _LayerIndex(vector_dir / RAIL_FILE)
    buildings = _LayerIndex(vector_dir / BUILDING_FILE)

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
        target = dataset.CreateLayer(TARGET_NAME, srs, ogr.wkbPoint)
        copied, icon_index, angle_index = _copy_fields(layer, target)
        definition = target.GetLayerDefn()

        source_crs = QgsCoordinateReferenceSystem.fromWkt(
            srs.ExportToWkt(),
        )
        if not source_crs.isValid():
            source_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            source_crs,
            QgsCoordinateReferenceSystem(METRIC_CRS),
            QgsProject.instance(),
        )

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
                projected = _projected(
                    transform, point.GetX(), point.GetY(),
                )
                if projected is None:
                    continue
                station = QgsGeometry.fromPointXY(projected)
                # Метры проекции Web Mercator растянуты широтой: на
                # земле расстояние меньше в cos(широты) раз.
                ground = measure.mercator_ground_factor(projected.y())
                rail = rails.nearest(station)
                building = buildings.nearest(station)
                rail_distance = _distance(station, rail, ground)
                building_distance = _distance(station, building, ground)
                on_rails = (
                    rail_distance is not None
                    and rail_distance <= RAIL_MAX_DISTANCE
                )
                near_building = (
                    building_distance is not None
                    and building_distance <= BUILDING_MAX_DISTANCE
                )
                icon = STATION_ICON
                if (
                    near_building
                    and on_rails
                    and not _is_underground(feature)
                ):
                    icon = BUILDING_ICON
                angle = None
                if rail is not None:
                    angle = _main_angle(rail)
                    if near_building:
                        # Сторона значка с вокзалом должна смотреть на
                        # здание.
                        angle = _turned(angle, station, building)
                target_feature = ogr.Feature(definition)
                for source_index, target_index in copied:
                    target_feature.SetField(
                        target_index, feature.GetField(source_index),
                    )
                target_feature.SetField(icon_index, icon)
                target_feature.SetField(angle_index, angle)
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
    report(i18n.trf("Значки станций: {} точек", count))
    return count


class _LayerIndex:
    """Слой с пространственным индексом: ближайшие объекты по точке."""

    def __init__(self, path):
        from qgis.core import QgsSpatialIndex, QgsVectorLayer

        self.index = QgsSpatialIndex()
        self.geometries = {}
        layer = QgsVectorLayer(str(path), Path(path).stem, "ogr")
        if not layer.isValid():
            return
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            self.geometries[feature.id()] = geometry
            self.index.addFeature(feature)

    def nearest(self, point):
        """Ближайшая геометрия к точке или None."""
        ids = self.index.nearestNeighbor(point, 1)
        for fid in ids:
            geometry = self.geometries.get(fid)
            if geometry is not None:
                return geometry
        return None


def _projected(transform, lon, lat):
    """Точка в EPSG:3857 или None, если перевод не удался."""
    from qgis.core import QgsCsException, QgsPointXY

    try:
        return transform.transform(QgsPointXY(lon, lat))
    except QgsCsException:
        return None


def _distance(station, geometry, ground):
    """Расстояние от станции до геометрии в метрах на земле."""
    if geometry is None:
        return None
    return station.distance(geometry) * ground


def _main_angle(geometry):
    """Угол линии по ориентированному минимальному прямоугольнику.

    Тот же угол, что давал шаблонный ``main_angle``: угол в градусах
    по часовой стрелке от севера.
    """
    try:
        box = geometry.orientedMinimumBoundingBox()
    except Exception:  # noqa: BLE001 - значок без поворота лучше пропустить
        return None
    if not box or len(box) < 3 or box[0] is None:
        return None
    try:
        return int(round(box[2] or 0.0))
    except (TypeError, ValueError):
        return None


def _turned(angle, station, building):
    """Угол значка, развёрнутый стороной вокзала к зданию."""
    azimuth = _azimuth(station, building)
    if (angle - azimuth + 360.0) % 360.0 > 180.0:
        return angle
    return angle + 180


def _azimuth(station, building):
    """Азимут от станции к зданию: градусы по часовой стрелке от севера."""
    x = building.asPoint().x() - station.asPoint().x()
    y = building.asPoint().y() - station.asPoint().y()
    return math.degrees(math.atan2(x, y)) % 360.0


def _is_underground(feature):
    """Подземная ли станция: положение, тоннель или слой ниже нуля."""
    location = str(_value(feature, "location") or "").strip().lower()
    if location in UNDERGROUND_LOCATIONS:
        return True
    for name in ("tunnel", "subway"):
        if str(_value(feature, name) or "").strip().lower() == "yes":
            return True
    if str(_value(feature, "station") or "").strip().lower() == "subway":
        return True
    for name in ("layer", "level"):
        number = _number(_value(feature, name))
        if number is not None and number < 0:
            return True
    return False


def _value(feature, name):
    """Значение поля объекта OSM (None, если поля нет)."""
    index = feature.GetFieldIndex(name)
    if index < 0 or not feature.IsFieldSetAndNotNull(index):
        return None
    return feature.GetField(index)


def _number(value):
    """Число из значения тега или None."""
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _copy_fields(source_layer, target_layer):
    """Копирует поля станции в слой значков.

    Возвращает пары индексов «источник — цель» и индексы полей тега
    значка и угла.  Поля прошлых сборок не копируются, иначе старое
    значение ``hide`` пережило бы пересборку.
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
    icon_field = ogr.FieldDefn(ICON_FIELD, ogr.OFTString)
    icon_field.SetWidth(64)
    target_layer.CreateField(icon_field)
    target_layer.CreateField(ogr.FieldDefn(ANGLE_FIELD, ogr.OFTInteger))
    definition = target_layer.GetLayerDefn()
    return (
        copied,
        definition.GetFieldIndex(ICON_FIELD),
        definition.GetFieldIndex(ANGLE_FIELD),
    )
