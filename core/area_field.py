"""Поле площади для фильтров рендерера.

QGIS-выражение `$area` считает планарную площадь в СК слоя и переводит
её в квадратные метры.  Провайдер не умеет проталкивать `$area` в SQL,
поэтому фильтр правила проверяется для каждого объекта в кадре.  Если
площадь лежит в обычном поле, тот же фильтр уходит в SQL, и провайдер
читает только подходящие объекты.

Значение `area_m2` совпадает с `$area` при `ellipsoid=NONE`, как в
шаблоне: площадь в единицах СК слоя, умноженная на квадрат коэффициента
перевода единиц в метры.
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


def ensure_area_fields(vector_dir):
    """Добавляет `area_m2` в подготовленные слои с фильтрами `$area`."""
    vector_dir = Path(vector_dir)
    changed = []
    for name in AREA_LAYERS.values():
        if add_area_field(vector_dir / f"{name}.sqlite"):
            changed.append(name)
    return changed
