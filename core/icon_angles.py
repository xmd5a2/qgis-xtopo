"""Углы поворота значков: считаются при подготовке, а не при отрисовке.

Значок перевала поворачивается по направлению ближайшего гребня, значок
остановки — по направлению ближайшей железной дороги.  Раньше всё это
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

Поле ``icon_angle`` — угол в градусах.  Значок станции и его поворот
считает ``core/station_icons.py``: там же выбирается тег значка.
"""

from pathlib import Path

from . import i18n

# Поле с готовым углом в слоях значков.
FIELD = "icon_angle"

# Выражения взяты из шаблона automap.qgz без изменений: пересчитанное
# значение должно совпадать с тем, что QGIS считал при отрисовке.
# Условие по переменной проекта (@rotate_mountain_pass) в шаблоне
# остаётся: переключатель поворота должен работать и после подготовки.
#
# Перевал поворачивается по гребню, только если он рядом с ним: до 200 м.
# Предел задан в метрах, а слой значков в градусах, поэтому расстояние
# считается в метрической системе.  overlay_nearest расстояние не
# ограничивает: пятый его аргумент измеряется в единицах слоя значков,
# то есть в градусах, и 200 градусов не ограничивают ничего.
PASS_MAX_DISTANCE = 200
METRIC_CRS = "EPSG:3857"
PASS_EXPRESSION = (
    "IF(distance(transform($geometry,'{crs}','{metric}'),"
    "transform(array_get(overlay_nearest('ridge_exploded',$geometry,1,1),0),"
    "'{crs}','{metric}')) <= {limit},"
    "round(degrees(azimuth(start_point(array_get(overlay_nearest("
    "'ridge_exploded',$geometry,1,1),0)),end_point(array_get("
    "overlay_nearest('ridge_exploded',$geometry,1,1),0))))),NULL)"
)
HALT_EXPRESSION = (
    "round(main_angle(array_get(overlay_nearest( "
    "'railway_all_exploded', $geometry,50),0)))"
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


def _fill(text, crs):
    """Подставляет в выражение систему координат слоя и предел расстояния."""
    return text.format(
        crs=crs or "EPSG:4326",
        metric=METRIC_CRS,
        limit=PASS_MAX_DISTANCE,
    )


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
        expression = QgsExpression(_fill(text, source.crs().authid()))
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
    """Записывает посчитанные значения в поля слоя.

    Запись идёт через SQLite напрямую: обновление строки через GDAL в
    Spatialite переписывает геометрию и пространственный индекс и стоит
    около сотни миллисекунд на объект, через SQLite — микросекунды.
    """
    if not values:
        return
    from . import sqlite_fields

    connection = sqlite_fields.connect(path)
    try:
        table = sqlite_fields.table_name(connection, path)
        columns = {
            row[1].lower(): row[1] for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            )
        }
        fields = list(next(iter(values.values())))
        sqlite_fields.ensure_fields(connection, table, columns, fields)
        primary = sqlite_fields.primary_key(connection, table)
        if primary.lower() not in columns:
            primary = "rowid"
        for name in fields:
            column = columns[name.lower()]
            updates = [
                (
                    None if row.get(name) is None
                    else int(round(float(row[name]))),
                    fid,
                )
                for fid, row in values.items()
            ]
            connection.executemany(
                f'UPDATE "{table}" SET "{column}" = ? '
                f'WHERE "{primary}" = ?',
                updates,
            )
        connection.commit()
    finally:
        connection.close()
