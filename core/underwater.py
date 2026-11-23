"""Удаление подводных вершин и вулканов из подготовленных данных.

В OSM встречаются вершины и вулканы на дне моря (например, Teahitia с
``ele=-1600``).  На карте у них нет суши под значком и подписью, поэтому
в проект они попадать не должны.

Признак подводного объекта — попадание точки в полигон воды, а не знак
высоты: в Долине Смерти есть настоящие вершины с отрицательной высотой
(``Cinder Hill``, ``Mars Hill``), удалять их нельзя.  Поэтому ``ele`` не
используется, а точка проверяется по ``water_dissolved``/``water``.
Дополнительно убираются вулканы с явным тегом ``volcano:type=submarine``.
"""

from . import i18n

# Слои, где подводные объекты не нужны на карте.
LAYER_NAMES = ("peak", "volcano", "volcano_dirt")
# Площадные вулканы рисуются по центроидам: их тоже нужно очистить.
CENTROID_NAMES = ("volcano", "volcano_dirt")


def enabled(config):
    """Запрошен ли хотя бы один слой, который чистится от подводных объектов."""
    queries = set(getattr(config, "array_queries", None) or ())
    return any(name in queries for name in LAYER_NAMES)


def layer_paths(config):
    paths = []
    for name in LAYER_NAMES:
        paths.append(config.vector_dir / f"{name}.sqlite")
        if name in CENTROID_NAMES:
            paths.append(config.vector_dir / f"{name}_centroids.sqlite")
    return [path for path in paths if path.exists()]


def _water_path(config):
    """Растворённый слой воды: он содержит и море, и внутренние водоёмы."""
    for name in ("water_dissolved.sqlite", "water.sqlite"):
        path = config.vector_dir / name
        if path.exists():
            return path
    return None


def _water_index(dataset, cancel=None):
    """Полигоны воды и пространственный индекс по ним.

    Поточечный ``SetSpatialFilter`` — это отдельный запрос к SQLite на
    каждую вершину: 20 тыс. точек обрабатывались около минуты.  Индекс
    строится один раз, дальше проверяются только соседние полигоны.
    """
    from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex

    layer = dataset.GetLayer(0)
    if layer is None:
        return None, []
    index = QgsSpatialIndex()
    insert = getattr(index, "addFeature", None) or index.insertFeature
    geometries = []
    for position, feature in enumerate(layer):
        if cancel is not None and position % 500 == 0 and cancel.is_set():
            raise InterruptedError("Underwater filter cancelled")
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        qgis_geometry = QgsGeometry()
        qgis_geometry.fromWkb(geometry.ExportToIsoWkb())
        if qgis_geometry.isNull():
            continue
        # Море — один мультиполигон с тысячами дырок: QgsGeometry.contains
        # обходит их заново для каждой вершины.  Подготовленная GEOS-
        # геометрия строит индекс рёбер один раз.
        shape = geometry.Clone()
        create = getattr(shape, "CreatePreparedGeometry", None)
        try:
            prepared = create() if create is not None else None
        except Exception:  # noqa: BLE001 - останется обычная проверка
            prepared = None
        item = QgsFeature(len(geometries))
        item.setGeometry(qgis_geometry)
        insert(item)
        geometries.append((qgis_geometry, shape, prepared))
    return index, geometries


def _contains_point(index, geometries, geometry):
    """Лежит ли точка внутри полигона воды.

    Для площадных объектов берётся центроид: именно он становится точкой
    вулкана на карте.
    """
    from osgeo import ogr
    from qgis.core import QgsGeometry, QgsPointXY

    if geometry.GetGeometryName() != "POINT":
        geometry = geometry.Centroid()
    if geometry is None or geometry.IsEmpty():
        return False
    x = geometry.GetX()
    y = geometry.GetY()
    point = QgsGeometry.fromPointXY(QgsPointXY(x, y))
    ogr_point = ogr.Geometry(ogr.wkbPoint)
    ogr_point.AddPoint_2D(x, y)
    for fid in index.intersects(point.boundingBox()):
        qgis_geometry, _shape, prepared = geometries[fid]
        if prepared is not None:
            if prepared.Contains(ogr_point):
                return True
        elif qgis_geometry.contains(point):
            return True
    return False


def _filter_layer(path, index, geometries, cancel=None):
    from osgeo import ogr

    ogr.UseExceptions()
    dataset = ogr.Open(str(path), 1)
    if dataset is None:
        return 0
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return 0
        definition = layer.GetLayerDefn()
        fields = {
            definition.GetFieldDefn(position).GetName().lower(): position
            for position in range(definition.GetFieldCount())
        }
        volcano_type = fields.get("volcano:type")
        underwater = []
        for feature in layer:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Underwater filter cancelled")
            geometry = feature.GetGeometryRef()
            if geometry is None or geometry.IsEmpty():
                continue
            if volcano_type is not None:
                value = str(feature.GetField(volcano_type) or "").strip().lower()
                if value == "submarine":
                    underwater.append(feature.GetFID())
                    continue
            if _contains_point(index, geometries, geometry):
                underwater.append(feature.GetFID())
        # Удаление внутри обхода заставляет OGR повторно отдавать
        # прочитанные записи, поэтому сначала собираем FID.
        if underwater:
            # Без общей транзакции каждый DeleteFeature — отдельный
            # commit SQLite: несколько тысяч удалений идут десятки секунд.
            started = False
            start = getattr(dataset, "StartTransaction", None)
            if start is not None:
                try:
                    # OGR возвращает 0 (OGRERR_NONE) при успехе.
                    started = start() in (None, 0)
                except RuntimeError:
                    started = False
            try:
                for fid in underwater:
                    layer.DeleteFeature(fid)
                if started:
                    dataset.CommitTransaction()
            except Exception:
                if started:
                    dataset.RollbackTransaction()
                raise
            layer.SyncToDisk()
        return len(underwater)
    finally:
        dataset = None


def remove_underwater(config, progress=None, cancel=None):
    """Убирает подводные вершины и вулканы, возвращает число объектов."""
    from osgeo import ogr

    paths = layer_paths(config)
    water_path = _water_path(config)
    if not paths or water_path is None:
        return 0
    report = progress or (lambda message: None)
    ogr.UseExceptions()
    dataset = ogr.Open(str(water_path))
    if dataset is None:
        return 0
    try:
        index, geometries = _water_index(dataset, cancel=cancel)
        if index is None:
            return 0
        removed = 0
        for path in paths:
            removed += _filter_layer(
                path, index, geometries, cancel=cancel
            )
    finally:
        dataset = None
    if removed:
        report(i18n.trf("Подводные вершины: удалено {}", removed))
    return removed
