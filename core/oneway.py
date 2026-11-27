"""Нормализация ``oneway`` для дорожных слоёв.

В OSM односторонность задаётся разными значениями: ``yes``, ``1``,
``true``, ``-1`` и ``no``.  Подготовка оставляет только полезные
значения: ``yes``, ``-1`` и пусто.  ``-1`` сохраняет обратное
направление: стиль разворачивает стрелку на 180°.  Явный ``no`` и
прочие значения очищаются: для стиля это то же самое, что отсутствие
тега.  Геометрия дорог не меняется, поэтому подписи и другие стили
остаются как были.

Круговые развязки (``junction=roundabout``/``circular``) считаются
односторонними, если явно не указано ``oneway=no``.
"""

import sqlite3
from pathlib import Path

from . import sqlite_fields

ONEWAY_LAYERS = ("highway_main", "highway_main_tunnel")

# Начиная с какого масштаба (знаменателя) показывать стрелки.  Значения
# уменьшаются с понижением класса дороги: на обзоре остаются только
# старшие классы, улицы появляются при более близком увеличении.
ONEWAY_ARROW_SCALES = {
    "motorway": 50000,
    "motorway_link": 50000,
    "trunk": 45000,
    "trunk_link": 45000,
    "primary": 40000,
    "primary_link": 40000,
    "secondary": 32000,
    "secondary_link": 32000,
    "tertiary": 25000,
    "tertiary_link": 25000,
    "unclassified": 20000,
    "residential": 16000,
    "living_street": 12000,
    "service": 12000,
}

_TRUE_VALUES = frozenset({"yes", "1", "true"})
_ROUNDABOUTS = frozenset({"roundabout", "circular"})

_SYSTEM_TABLES = frozenset({
    "spatial_ref_sys", "spatialite_history", "geometry_columns",
    "spatial_ref_sys_aux", "views_geometry_columns",
    "virts_geometry_columns", "geometry_columns_statistics",
    "views_geometry_columns_statistics", "virts_geometry_columns_statistics",
    "geometry_columns_field_infos", "views_geometry_columns_field_infos",
    "geometry_columns_time", "geometry_columns_auth", "views_geometry_columns_auth",
    "virts_geometry_columns_auth", "data_licenses", "sql_statements_log",
    "SpatialIndex", "ElementaryGeometries", "KNN2",
})


def _text(value):
    return "" if value is None else str(value).strip().lower()


def normalize(value, junction=None):
    """Приводит значение OSM ``oneway`` к ``yes``/``-1``/пусто."""
    text = _text(value)
    if text in _TRUE_VALUES:
        return "yes"
    if text == "-1":
        return "-1"
    if text == "no":
        return ""
    if text == "" and _text(junction) in _ROUNDABOUTS:
        return "yes"
    return ""


def arrow_scale(highway):
    """Предельный масштаб стрелок для класса дороги; 0 — не рисовать."""
    return ONEWAY_ARROW_SCALES.get(_text(highway), 0)


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
    connection = sqlite_fields.connect(path)
    try:
        table = _geometry_table(connection)
        if not table:
            return 0
        columns = [
            row[1] for row in
            connection.execute(f"PRAGMA table_info({_quote(table)})")
        ]
        if "oneway" not in columns:
            connection.execute(
                f'ALTER TABLE {_quote(table)} ADD COLUMN "oneway" TEXT'
            )
        key = _key_column(connection, table, columns)
        key_expression = "rowid" if key == "rowid" else _quote(key)
        source = [
            _quote(field) if field in columns else "NULL"
            for field in ("oneway", "junction")
        ]
        rows = []
        processed = 0
        for values in connection.execute(
            f"SELECT {key_expression}, {', '.join(source)} "
            f"FROM {_quote(table)}"
        ):
            processed += 1
            normalized = normalize(values[1], values[2])
            current = "" if values[1] is None else values[1]
            if current != normalized:
                rows.append((normalized, values[0]))
        if rows:
            connection.executemany(
                f'UPDATE {_quote(table)} SET "oneway"=? '
                f"WHERE {key_expression}=?",
                rows,
            )
            connection.commit()
        return processed
    except sqlite3.Error:
        connection.rollback()
        return 0
    finally:
        connection.close()


def store(directory, name):
    """Нормализует поле в готовых файлах слоя. Возвращает число строк."""
    total = 0
    for stem in _file_names(name):
        path = Path(directory) / f"{stem}.sqlite"
        if path.exists():
            total += _add_to_file(path)
    return total
