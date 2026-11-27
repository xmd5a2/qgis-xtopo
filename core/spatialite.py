"""Параметры создания SpatiaLite для файлов слоёв.

``SPATIALITE=YES`` без ``INIT_WITH_EPSG=NO`` записывает в каждый файл
полную таблицу EPSG: около 6,7 МБ и заметного времени на файл. Слою
нужна только его собственная проекция, поэтому таблица заполняется
одной записью. QGIS читает такой файл и строит пространственный фильтр
так же, как файл с полной таблицей.
"""

CREATION_OPTIONS = ("SPATIALITE=YES", "INIT_WITH_EPSG=NO")


def gdal_options():
    """Те же параметры в виде аргументов ``gdal.VectorTranslate``."""
    options = []
    for option in CREATION_OPTIONS:
        options.extend(["-dsco", option])
    return options
