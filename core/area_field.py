"""Поля площади для фильтров рендерера и подписей.

QGIS-выражение `$area` считает площадь для каждого объекта в кадре.
Провайдер не умеет проталкивать его в SQL, поэтому фильтр правила
проверяется вручную.  Если площадь лежит в обычном поле, тот же фильтр
уходит в SQL, и провайдер читает только подходящие объекты.

`area_m2` совпадает с `$area` при `ellipsoid=NONE`: площадь в единицах
СК слоя, умноженная на квадрат коэффициента перевода единиц в метры.
Этим полем пользуются фильтры рендерера.

Для подписей одного `area_m2` мало: на широте дельты Лены планарная
площадь в градусах завышает её в несколько раз.  Поэтому `area_geodesic`
считается по эллипсоиду WGS84 тем же `QgsDistanceArea`, что и `$area`
в контексте подписей.  Тем же полем пользуется рендерер воды: пороги
мелких озёр на обзорных масштабах должны быть одинаковыми в Исландии и
в Италии.
"""

from pathlib import Path

# Слои, у которых фильтры рендерера используют `$area`.
# Имя слоя в проекте -> имя файла без расширения.
AREA_LAYERS = {
    "allotments": "allotments",
    "building": "building",
    "cemetery": "cemetery",
    "landfill": "landfill",
    "nature_reserve": "nature_reserve",
    "orchard": "orchard",
    "plant nursery": "plant_nursery",
    "quarry": "quarry",
    "scrub": "scrub",
    "vineyard": "vineyard",
    "water": "water_dissolved",
    "wood": "wood",
}

# Слои, у которых фильтры рендерера используют геодезическую площадь.
# Имя файла без расширения.  Для воды нужна именно она: планарная
# `area_m2` в градусах завышает площадь на высоких широтах, и один и тот
# же порог скрывал бы разные по размеру озёра в Исландии и в Италии.
GEODESIC_AREA_LAYERS = frozenset({"water_dissolved"})

# Слои, у которых правила подписей или размер текста используют площадь.
# Имя слоя в проекте -> имя файла без расширения.
LABEL_AREA_LAYERS = {
    "cemetery": "cemetery",
    "desert": "desert",
    "glacier": "glacier",
    "island polygon": "island",
    "mountain area": "mountain_area",
    "nature_reserve": "nature_reserve",
    "plateau": "plateau",
    "sand": "sand",
    "water intermittent": "water_intermittent_dissolved",
    "water names continental": "water",
}

LABEL_AREA_FIELD = "area_geodesic"

# QgsUnitTypes.fromUnitToUnitFactor(DistanceDegrees, DistanceMeters).
DEGREE_TO_METER = 111319.49079327357


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _area_factor(srs):
    """Множитель площади СК слоя в квадратные метры."""
    if srs is None:
        return None
    if srs.IsGeographic():
        return DEGREE_TO_METER ** 2
    # GetLinearUnits() возвращает метров на единицу СК.
    return srs.GetLinearUnits() ** 2


def _fast_pragmas(dataset):
    """Ускоряет массовое заполнение: журнал в памяти, без fsync.

    Слой пишется во временный файл и подменяет готовый только после
    успеха, поэтому отключение синхронизации не угрожает прежним данным.
    """
    for pragma in ("PRAGMA journal_mode=MEMORY",
                   "PRAGMA synchronous=OFF",
                   "PRAGMA cache_size=-262144"):
        result = dataset.ExecuteSQL(pragma)
        if result is not None:
            dataset.ReleaseResultSet(result)


def _geometry_expression(dataset, table, geometry, srid):
    """Выражение геометрии, понятное SpatiaLite.

    GDAL пишет часть слоёв как SpatiaLite BLOB, часть — как обычный WKB
    или GeoPackage.  `ST_Area` принимает только первый, остальные надо
    обернуть в `GeomFromWKB`/`GeomFromGPB`.
    """
    quoted = _quote(geometry)
    columns = set()
    try:
        result = dataset.ExecuteSQL("PRAGMA table_info(geometry_columns)")
    except Exception:
        return quoted
    if result is not None:
        while True:
            feature = result.GetNextFeature()
            if feature is None:
                break
            columns.add(feature.GetField("name"))
        dataset.ReleaseResultSet(result)
    if "geometry_format" not in columns:
        return quoted
    try:
        result = dataset.ExecuteSQL(
            f"SELECT geometry_format FROM geometry_columns "
            f"WHERE f_table_name = {_quote(table)} LIMIT 1")
    except Exception:
        return quoted
    if result is None:
        return quoted
    feature = result.GetNextFeature()
    value = feature.GetField(0) if feature is not None else None
    dataset.ReleaseResultSet(result)
    value = (value or "").upper()
    if value == "WKB":
        return f"GeomFromWKB({quoted}, {srid})"
    if value in ("GPKG", "GEOPACKAGE"):
        return f"GeomFromGPB({quoted})"
    return quoted


def add_area_field(path):
    """Добавляет и заполняет `area_m2` в одном SQLite-слое.

    Возвращает True, если поле добавлено или заполнено.  Повторный
    вызов на готовом слое ничего не меняет.
    """
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    ogr.UseExceptions()
    path = Path(path)
    if not path.exists():
        return False
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_UPDATE)
    if dataset is None:
        return False
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return False
        srs = layer.GetSpatialRef()
        factor = _area_factor(srs)
        if factor is None:
            return False
        table = layer.GetName()
        geometry = layer.GetGeometryColumn() or "geometry"
        srid = int(srs.GetAuthorityCode(None) or 0)
        has_field = layer.GetLayerDefn().GetFieldIndex("area_m2") >= 0
        fill = not has_field
        if not has_field:
            _fast_pragmas(dataset)
            dataset.ExecuteSQL(
                f"ALTER TABLE {_quote(table)} ADD COLUMN \"area_m2\" REAL")
        else:
            result = dataset.ExecuteSQL(
                f'SELECT "area_m2" FROM {_quote(table)} LIMIT 1')
            if result is not None:
                feature = result.GetNextFeature()
                fill = feature is None or not feature.IsFieldSetAndNotNull(0)
                dataset.ReleaseResultSet(result)
        if fill:
            _fast_pragmas(dataset)
            expression = _geometry_expression(
                dataset, table, geometry, srid)
            dataset.ExecuteSQL(
                f'UPDATE {_quote(table)} SET "area_m2" = '
                f'ST_Area({expression}) * {factor!r}')
        return fill
    finally:
        dataset = None


def _source_crs(layer):
    """СК слоя в виде QgsCoordinateReferenceSystem."""
    from qgis.core import QgsCoordinateReferenceSystem

    srs = layer.GetSpatialRef()
    if srs is not None:
        authority = srs.GetAuthorityName(None)
        code = srs.GetAuthorityCode(None)
        if authority and code:
            crs = QgsCoordinateReferenceSystem(f"{authority}:{code}")
            if crs.isValid():
                return crs
        crs = QgsCoordinateReferenceSystem()
        if crs.createFromWkt(srs.ExportToWkt()):
            return crs
    return QgsCoordinateReferenceSystem("EPSG:4326")


def add_geodesic_area_field(path, field=LABEL_AREA_FIELD):
    """Добавляет и заполняет геодезическую площадь в одном слое.

    Значение совпадает с `$area` в контексте подписей: площадь по
    эллипсоиду WGS84 в квадратных метрах.  Повторный вызов на готовом
    слое ничего не меняет.
    """
    from osgeo import gdal, ogr
    from qgis.core import (
        QgsCoordinateTransformContext,
        QgsDistanceArea,
        QgsGeometry,
    )

    gdal.UseExceptions()
    ogr.UseExceptions()
    path = Path(path)
    if not path.exists():
        return False
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_UPDATE)
    if dataset is None:
        return False
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return False
        index = layer.GetLayerDefn().GetFieldIndex(field)
        if index >= 0:
            for feature in layer:
                if feature.IsFieldSetAndNotNull(index):
                    return False
        else:
            layer.CreateField(ogr.FieldDefn(field, ogr.OFTReal))
            index = layer.GetLayerDefn().GetFieldIndex(field)

        distance = QgsDistanceArea()
        distance.setSourceCrs(
            _source_crs(layer), QgsCoordinateTransformContext())
        distance.setEllipsoid("WGS84")

        layer.StartTransaction()
        try:
            layer.ResetReading()
            for feature in layer:
                geometry = feature.GetGeometryRef()
                area = 0.0
                if geometry is not None and not geometry.IsEmpty():
                    shape = QgsGeometry()
                    shape.fromWkb(bytes(geometry.ExportToWkb()))
                    if not shape.isEmpty():
                        area = distance.measureArea(shape)
                feature.SetField(index, area)
                layer.SetFeature(feature)
            layer.CommitTransaction()
        except Exception:
            layer.RollbackTransaction()
            raise
        return True
    finally:
        dataset = None


def ensure_area_fields(vector_dir):
    """Добавляет поля площади в подготовленные слои с фильтрами."""
    vector_dir = Path(vector_dir)
    renderer = set(AREA_LAYERS.values())
    labels = set(LABEL_AREA_LAYERS.values())
    geodesic = labels | set(GEODESIC_AREA_LAYERS)
    changed = []
    for name in sorted(renderer | geodesic):
        path = vector_dir / f"{name}.sqlite"
        updated = False
        if name in renderer:
            updated = add_area_field(path) or updated
        if name in geodesic:
            updated = add_geodesic_area_field(path) or updated
        if updated:
            changed.append(name)
    return changed
