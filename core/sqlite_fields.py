"""Запись атрибутов Spatialite напрямую, без переписывания геометрии.

Обновление строки через GDAL в Spatialite переписывает геометрию и
пространственный индекс и стоит около сотни миллисекунд на объект,
через sqlite3 — микросекунды.  Геометрия при этом не меняется: триггеры
Spatialite висят на ``UPDATE OF "geom"``, а поля атрибутов — другие
столбцы.

Слои подготовки лежат во временном каталоге и заменяются только после
успешной сборки, поэтому ждать fsync на каждой транзакции не нужно:
``synchronous=OFF`` ускоряет запись в разы и не меняет содержимое файлов.
"""

import sqlite3


def connect(path):
    """Подключение к Spatialite с быстрыми, но недолговечными прагмами."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA cache_size=-262144")
    except sqlite3.DatabaseError:
        pass
    return connection


def table_name(connection, path):
    """Имя таблицы слоя в файле Spatialite."""
    rows = connection.execute(
        "SELECT f_table_name FROM geometry_columns"
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise ValueError(f"в {path} нет слоёв с геометрией")
    raise ValueError(f"в {path} несколько слоёв: {[r[0] for r in rows]}")


def primary_key(connection, table):
    """Имя первичного ключа таблицы."""
    for row in connection.execute(f'PRAGMA table_info("{table}")'):
        if row[5]:
            return row[1]
    return "fid"


def ensure_fields(connection, table, columns, names):
    """Добавляет недостающие целочисленные поля.

    ``columns`` — словарь ``имя в нижнем регистре -> имя в таблице``;
    он дополняется добавленными полями и возвращается.
    """
    for name in names:
        if name.lower() not in columns:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{name}" INTEGER'
            )
            columns[name.lower()] = name
    return columns
