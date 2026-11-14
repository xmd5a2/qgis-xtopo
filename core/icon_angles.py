"""Углы поворота значков: считаются при подготовке, а не при отрисовке.

Значок перевала поворачивается по направлению ближайшего гребня, значок
железнодорожной станции — по направлению ближайшей железной дороги, а
станция рядом со зданием вокзала получает другой значок.  Раньше всё это
считалось выражениями в шаблоне, и QGIS при каждой отрисовке строил
индекс всего слоя гребней или рельсов: карта открывалась заметно
дольше нужного.

Теперь значения пишутся в поля слоёв при подготовке карты, а в шаблоне
остаётся только обращение к полю.  Значения совпадают с прежними: их
считает тот же движок выражений QGIS, теми же выражениями из шаблона,
только один раз за подготовку вместо каждой отрисовки.

Слои-источники лежат в другой системе координат, чем значки: гребни,
рельсы и вокзалы проецированы в EPSG:3857 (метры).  Выражения QGIS
переводят их в систему слоя значков сами, поэтому и здесь слои
подключаются теми же именами, что и в шаблоне.

Поле ``icon_angle`` — угол в градусах, ``icon_building`` — признак
станции рядом со зданием вокзала (1 — значок вокзала).
"""

from pathlib import Path

from . import i18n

# Поле с готовым углом в слоях значков.
FIELD = "icon_angle"
# Поле с признаком станции у вокзала.
BUILDING_FIELD = "icon_building"

# Выражения взяты из шаблона automap.qgz без изменений: пересчитанное
# значение должно совпадать с тем, что QGIS считал при отрисовке.
# Условие по переменной проекта (@rotate_mountain_pass) в шаблоне
# остаётся: переключатель поворота должен работать и после подготовки.
PASS_EXPRESSION = (
    "round(degrees(azimuth(start_point(array_get(overlay_nearest( "
    "'ridge_exploded', $geometry,1,1,200),0)), end_point(array_get("
    "overlay_nearest( 'ridge_exploded', $geometry,1,1,200),0)))))"
)
STATION_EXPRESSION = (
    "round(main_angle(array_get(overlay_nearest( "
    "'railway_all_exploded', $geometry,50),0))) +\n"
    "IF(layer_property('building_train_station_reproj',id) IS NOT NULL,(\n"
    "IF((main_angle(array_get(overlay_nearest( 'railway_all_exploded', "
    "$geometry,50),0)) - degrees(azimuth($geometry, array_get("
    "overlay_nearest( 'building_train_station_reproj', $geometry,50),0)))"
    " + 360)%360 > 180, 0, 180)\n"
    "),0)"
)
HALT_EXPRESSION = (
    "round(main_angle(array_get(overlay_nearest( "
    "'railway_all_exploded', $geometry,50),0)))"
)
# Станция ближе 500 м к зданию вокзала получает другой значок.
BUILDING_EXPRESSION = (
    "IF(distance($geometry,array_get(overlay_nearest( "
    "'building_train_station_reproj', $geometry,50),0)) < 500, 1, 0)"
)

# (файл слоя значков, имя слоя в шаблоне, поля с выражениями,
#  слои-источники)
TARGETS = (
    (
        "mountain_pass.sqlite",
        "mountain pass",
        ((FIELD, PASS_EXPRESSION),),
        (("ridge_exploded.sqlite", "ridge_exploded"),),
    ),
    (
        "railway_station_icons_buffered_dissolved_centroids.sqlite",
        "railway station icons",
        ((FIELD, STATION_EXPRESSION), (BUILDING_FIELD, BUILDING_EXPRESSION)),
        (
            ("railway_all_exploded.sqlite", "railway_all_exploded"),
            ("building_train_station_reproj.sqlite",
             "building_train_station_reproj"),
        ),
    ),
    (
        "railway_halt_icons_buffered_dissolved_centroids.sqlite",
        "railway halt icons",
        ((FIELD, HALT_EXPRESSION),),
        (("railway_all_exploded.sqlite", "railway_all_exploded"),),
    ),
)


def compute_icon_angles(config, progress=None, cancel=None):
    """Считает углы поворота значков и пишет их в поля слоёв.

    Возвращает число слоёв, в которых значения посчитаны.
    """
    report = progress or (lambda message: None)
    vector_dir = Path(config.vector_dir)
    done = 0
    for file_name, layer_name, fields, overlays in TARGETS:
        if cancel is not None and cancel.is_set():
            break
        source_path = vector_dir / file_name
        if not source_path.exists():
            continue
        overlay_paths = [
            (vector_dir / name, title) for name, title in overlays
            if (vector_dir / name).exists()
        ]
        if not overlay_paths:
            continue
        values = _evaluate(source_path, layer_name, fields, overlay_paths)
        if values is None:
            report(i18n.trf(
                "WARNING: {}: угол поворота не посчитан", layer_name,
            ))
            continue
        _write_fields(source_path, values)
        done += 1
    if done:
        report(i18n.trf("Углы значков: {} слоёв", done))
    return done


def _evaluate(source_path, layer_name, fields, overlay_paths):
    """Считает выражения по объектам слоя.

    Возвращает {fid: {поле: значение}} или None, если слой не читается.
    """
    from qgis.core import (
        QgsExpression,
        QgsExpressionContext,
        QgsExpressionContextScope,
        QgsExpressionContextUtils,
        QgsMapLayerStore,
        QgsProject,
        QgsVectorLayer,
    )

    source = QgsVectorLayer(str(source_path), layer_name, "ogr")
    if not source.isValid():
        return None
    # Слои-источники подключаются отдельным хранилищем: в проекте
    # шаблонные слои с теми же именами указывают на ещё не готовые
    # файлы, и выражение нашло бы их вместо подготовленных.
    store = QgsMapLayerStore()
    for path, title in overlay_paths:
        overlay = QgsVectorLayer(str(path), title, "ogr")
        if overlay.isValid():
            store.addMapLayer(overlay)
    expressions = {}
    for field, text in fields:
        expression = QgsExpression(text)
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.globalScope())
        context.appendScope(QgsExpressionContextUtils.layerScope(source))
        context.appendScope(
            QgsExpressionContextUtils.projectScope(QgsProject.instance())
        )
        scope = QgsExpressionContextScope()
        scope.addLayerStore(store)
        # Последняя область имеет приоритет: её слои находятся первыми.
        context.appendScope(scope)
        if not expression.prepare(context):
            return None
        expressions[field] = (expression, context)
    values = {}
    for feature in source.getFeatures():
        row = {}
        for field, (expression, context) in expressions.items():
            context.setFeature(feature)
            value = expression.evaluate(context)
            if expression.hasEvalError():
                # Выражение без результата (нет слоя-источника) — значение
                # неизвестно, поле остаётся пустым.
                value = None
            row[field] = value
        values[feature.id()] = row
    return values


def _write_fields(path, values):
    """Записывает посчитанные значения в поля слоя."""
    from osgeo import ogr

    ogr.UseExceptions()
    dataset = ogr.Open(str(path), 1)
    if dataset is None:
        return
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return
        indexes = {}
        for field in values[next(iter(values))] if values else ():
            index = layer.FindFieldIndex(field, True)
            if index < 0:
                layer.CreateField(ogr.FieldDefn(field, ogr.OFTInteger))
                index = layer.FindFieldIndex(field, True)
            indexes[field] = index
        for fid, row in values.items():
            feature = layer.GetFeature(fid)
            if feature is None:
                continue
            for field, value in row.items():
                index = indexes.get(field, -1)
                if index < 0:
                    continue
                if value is None:
                    feature.SetFieldNull(index)
                else:
                    feature.SetField(index, int(round(float(value))))
            layer.SetFeature(feature)
        layer.SyncToDisk()
    finally:
        dataset = None
