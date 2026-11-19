"""Материализация ``highway_integrity`` для дорожных слоёв.

Шаблон раньше объявлял поле виртуальным: QGIS вычислял его выражением на
каждый объект при каждом кадре, а выражение вызывало Python-функцию. Это
заметно на плотных дорожных слоях. Подготовка пишет те же значения в
обычный столбец, и рендер читает его без Python.

Выражение QGIS ``calculate_highway_integrity`` вызывает ту же функцию
``calculate``, поэтому формула у подготовки и у старых проектов одна.
"""

import sqlite3
from pathlib import Path

MAX_INTEGRITY = 30

# Слои, чьи данные читают поле. Для highway_main записываются ещё
# разбиения по layer, которые шаблон показывает отдельными слоями.
INTEGRITY_LAYERS = (
    "bridge_area",
    "highway_main",
    "highway_main_area",
    "highway_main_tunnel",
    "place_square_highway_area",
    "track",
)

_SYSTEM_TABLES = frozenset({
    "spatial_ref_sys", "spatialite_history", "geometry_columns",
    "spatial_ref_sys_aux", "views_geometry_columns",
    "virts_geometry_columns", "geometry_columns_statistics",
    "views_geometry_columns_statistics", "virts_geometry_columns_statistics",
    "geometry_columns_field_infos", "views_geometry_columns_field_infos",
    "virts_geometry_columns_field_infos", "geometry_columns_time",
    "geometry_columns_auth", "views_geometry_columns_auth",
    "virts_geometry_columns_auth", "data_licenses", "sql_statements_log",
    "SpatialIndex", "ElementaryGeometries", "KNN2",
})

_SURFACE_VALUES = {
    "paved": 3, "concrete": 3, "concrete:lanes": 3,
    "concrete:plates": 3, "sett": 3, "paving_stones": 3,
    "metal": 3, "wood": 3,
    "fine_gravel": 4, "grass_paver": 4,
    "compacted": 8,
    "ground": 9, "earth": 9, "pebblestone": 9,
    "grass": 10,
    "cobblestone": 11,
    "gravel": 12,
    "stone": 13, "rock": 13, "rocky": 13,
    "unpaved": 14, "dirt": 14,
    "salt": 15, "ice": 15, "snow": 15,
    "sand": 16,
    "mud": 18,
}

_SMOOTHNESS_PENALTY = {
    "excellent": 5, "very_good": 4, "good": 2,
}

_SMOOTHNESS_FIXED = {
    "excellent": 7, "very_good": 6, "good": 8, "intermediate": 9,
    "bad": 9, "very_bad": 12, "horrible": 15,
    "very_horrible": 18, "impassable": 24,
}

_SMOOTHNESS_ADD = {
    "bad": 6, "very_bad": 7, "horrible": 9,
    "very_horrible": 11, "impassable": 12,
}

_SMOOTHNESS_ADD_ASPHALT = {
    "bad": 7, "very_bad": 12, "horrible": 19,
    "very_horrible": 22, "impassable": 26,
}

_TRACKTYPE_ADD = {
    "grade1": 1, "grade2": 3, "grade3": 7, "grade4": 10, "grade5": 15,
}

_PAVED_HIGHWAYS = frozenset({
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "service", "pedestrian", "living_street", "footway", "cycleway",
})


def calculate(highway, surface, smoothness, tracktype):
    """Целочисленная оценка проезжаемости дороги, 0…33."""
    highway = "" if highway is None else str(highway)
    surface = ("" if surface is None else str(surface)).lower()
    smoothness = ("" if smoothness is None else str(smoothness)).lower()
    tracktype = ("" if tracktype is None else str(tracktype)).lower()

    result = _SURFACE_VALUES.get(surface, 0)
    track_or_path = highway in ("track", "path") and surface == ""
    if smoothness in _SMOOTHNESS_FIXED and track_or_path:
        result = _SMOOTHNESS_FIXED[smoothness]
    elif smoothness in _SMOOTHNESS_PENALTY:
        result -= _SMOOTHNESS_PENALTY[smoothness]
    elif smoothness in _SMOOTHNESS_ADD:
        if surface == "asphalt":
            result += _SMOOTHNESS_ADD_ASPHALT[smoothness]
        else:
            result += _SMOOTHNESS_ADD[smoothness]
    # intermediate не меняет результат вне track/path — как в оригинале.

    if surface == "":
        result += _TRACKTYPE_ADD.get(tracktype, 0)

    if highway in _PAVED_HIGHWAYS and surface == "" and smoothness == "":
        result = 100
    if highway in ("track", "path") and not (
            surface or smoothness or tracktype):
        result = 100
    if result < 0:
        result = 0
    return int(result * 10 / MAX_INTEGRITY)


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _geometry_table(connection):
    try:
        row = connection.execute(
            "SELECT f_table_name FROM geometry_columns LIMIT 1"
        ).fetchone()
        if row:
            return row[0]
    except sqlite3.Error:
        pass
    names = [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'idx_%'"
        )
        if row[0] not in _SYSTEM_TABLES
    ]
    return names[0] if len(names) == 1 else None


def _key_column(connection, table, columns):
    for row in connection.execute(f"PRAGMA table_info({_quote(table)})"):
        if row[5]:
            return row[1]
    for candidate in ("ogc_fid", "fid", "objectid"):
        if candidate in columns:
            return candidate
    return "rowid"


def _file_names(name):
    if name == "highway_main":
        return (name, f"{name}_layer_1", f"{name}_layer_-1")
    return (name,)


def _add_to_file(path):
    connection = sqlite3.connect(str(path))
    try:
        table = _geometry_table(connection)
        if not table:
            return 0
        columns = [
            row[1] for row in
            connection.execute(f"PRAGMA table_info({_quote(table)})")
        ]
        if "highway_integrity" in columns:
            return 0
        connection.execute(
            f'ALTER TABLE {_quote(table)} '
            f'ADD COLUMN "highway_integrity" INTEGER'
        )
        key = _key_column(connection, table, columns)
        source = [
            _quote(field) if field in columns else "NULL"
            for field in ("highway", "surface", "smoothness", "tracktype")
        ]
        key_expression = "rowid" if key == "rowid" else _quote(key)
        rows = [
            (calculate(*values[1:]), values[0])
            for values in connection.execute(
                f"SELECT {key_expression}, {', '.join(source)} "
                f"FROM {_quote(table)}"
            )
        ]
        connection.executemany(
            f'UPDATE {_quote(table)} SET "highway_integrity"=? '
            f"WHERE {key_expression}=?",
            rows,
        )
        connection.commit()
        return len(rows)
    except sqlite3.Error:
        connection.rollback()
        return 0
    finally:
        connection.close()


def store(directory, name):
    """Дописывает поле в готовые файлы слоя. Возвращает число строк."""
    total = 0
    for stem in _file_names(name):
        path = Path(directory) / f"{stem}.sqlite"
        if path.exists():
            total += _add_to_file(path)
    return total
