"""Наборы полей для тяжёлых слоёв.

Драйвер OGR читает все колонки таблицы при выборке объектов. В слоях OSM
их сотни, а стилю нужны единицы; лишние значения заметно замедляют рендер.
Подготовка записывает только поля из набора, которые реально есть в
источнике.

Названия (`name`, `name:en`, все `name:*`) и подписи (`ref`, `alt_name`,
…) сохраняются целиком: от них зависит переключение языка карты.
`@id` (`way/333333`) остаётся у каждого объекта: это идентификатор
исходного объекта OSM.
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

# Пункты: place/population нужны не только стилю, но и расчёту
# приоритетов подавления значков (`poi_icons._settlement_priority`).
PLACE_LOCALITY = ("place", "population", "capital", "hide")
PLACES_MAIN = (
    "place", "population", "capital", "admin_centre_2", "admin_centre_4",
    "hide",
)
PATH = (
    "highway", "embankment", "ice_road", "winter_road", "ref",
    "surface", "smoothness", "tracktype",
)
WATER = ("natural", "water", "intermittent", "ref")
TRACK = (
    "highway", "highway_integrity", "embankment", "ice_road",
    "winter_road", "ref", "surface", "smoothness", "tracktype",
)
# `ele_calc` — виртуальное поле шаблона; в данных его нет, `select`
# пропустит имя.  `ele`, `height` и `@id` нужны проверке пиков.
PEAK = (
    "ele", "ele_calc", "height", "@id", "wikipedia", "wikidata", "hide",
    "natural", "man_made", "place",
)

PRESETS = {
    "building": BUILDING,
    "highway_main": HIGHWAY,
    "place_locality_point": PLACE_LOCALITY,
    "places_main": PLACES_MAIN,
    "path": PATH,
    "water": WATER,
    "track": TRACK,
    "peak": PEAK,
}


def select(preset, available):
    """Поля набора, которые есть в источнике, в порядке источника."""
    exact = set(PRESETS[preset])
    return [
        name for name in available
        if name in exact
        or name == "@id"
        or name in LABEL_FIELDS
        or name.lower().startswith("name")
    ]
