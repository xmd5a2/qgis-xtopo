"""Наборы полей для тяжёлых слоёв.

Драйвер OGR читает все колонки таблицы при выборке объектов. В слоях OSM
их сотни, а стилю нужны единицы; лишние значения заметно замедляют рендер.
Подготовка записывает только поля из набора, которые реально есть в
источнике.

Названия (`name`, `name:en`, все `name:*`) и подписи (`ref`, `alt_name`,
…) сохраняются целиком: от них зависит переключение языка карты.
"""

# Подписи используют эти поля напрямую.
LABEL_FIELDS = frozenset({
    "ref", "alt_name", "official_name", "old_name", "loc_name",
    "int_name", "short_name",
})

BUILDING = (
    "abandoned", "building", "disused", "historic", "ruins", "amenity",
)

HIGHWAY = (
    "highway", "service", "ice_road", "winter_road", "embankment", "layer",
    "surface", "smoothness", "tracktype", "highway_integrity",
)

PRESETS = {
    "building": BUILDING,
    "highway_main": HIGHWAY,
}


def select(preset, available):
    """Поля набора, которые есть в источнике, в порядке источника."""
    exact = set(PRESETS[preset])
    return [
        name for name in available
        if name in exact
        or name in LABEL_FIELDS
        or name.lower().startswith("name")
    ]
