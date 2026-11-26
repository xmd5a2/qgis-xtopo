"""Наборы полей для тяжёлых слоёв и отбор по шаблону проекта.

Драйвер OGR читает все колонки таблицы при выборке объектов. В слоях OSM
их сотни, а стилю нужны единицы; лишние значения заметно замедляют рендер.
Подготовка записывает только поля из набора, которые реально есть в
источнике.

Названия (`name`, `name:en`, все `name:*`) и подписи (`ref`, `alt_name`,
…) сохраняются целиком: от них зависит переключение языка карты.
`@id` (`way/333333`) остаётся у каждого объекта: это идентификатор
исходного объекта OSM.
"""

import html
import re
import zipfile
from functools import lru_cache
from pathlib import Path

from .resources import template_path

# Подписи используют эти поля напрямую.
LABEL_FIELDS = frozenset({
    "ref", "alt_name", "official_name", "old_name", "loc_name",
    "int_name", "short_name",
})

BUILDING = (
    "abandoned", "building", "disused", "historic", "ruins", "amenity",
)

HIGHWAY = (
    "highway", "service", "oneway", "junction", "ice_road", "winter_road",
    "embankment", "layer", "surface", "smoothness", "tracktype",
    "highway_integrity",
)

# Промзоны: стиль рисует один символ, подписи берут только name*.
INDUSTRIAL = ("landuse",)

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
    "industrial": INDUSTRIAL,
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


# Поля, которые добавляются после сохранения или нужны служебным шагам.
ESSENTIAL_FIELDS = frozenset({
    "@id", "@type", "hide", "area_geodesic", "area_m2", "@level_of_detail",
    "ele_calc", "highway_integrity", "dry_rank", "left:country",
    "right:country", "left:province", "right:province", "maritime",
    "open_border",
})

# Поля, которые могут понадобиться любому слою без явного набора.
COMMON_FIELDS = frozenset({
    "highway", "waterway", "railway", "barrier", "natural", "water",
    "landuse", "place", "building", "amenity", "tourism", "leisure",
    "man_made", "power", "boundary", "admin_level", "intermittent", "layer",
    "service", "oneway", "junction", "surface", "smoothness", "tracktype",
    "bridge", "tunnel", "length", "width", "ele", "height", "population",
    "capital", "ref",
})


def is_essential(name):
    """Поле нельзя выбрасывать независимо от ссылок в шаблоне."""
    return (
        name in ESSENTIAL_FIELDS
        or name in LABEL_FIELDS
        or name.lower().startswith("name")
    )


def _maplayer_blocks(text):
    """Блоки ``<maplayer>`` из XML шаблона без копирования всего документа."""
    position = 0
    while True:
        start = text.find("<maplayer", position)
        if start < 0:
            return
        end = text.find("</maplayer>", start)
        if end < 0:
            return
        end += len("</maplayer>")
        yield text[start:end]
        position = end


def _fields_in_expression(expression):
    """Имена, которые могут быть полями в выражении QGIS."""
    expression = html.unescape(expression or "")
    fields = set(re.findall(r'"([^"]+)"', expression))
    fields.update(re.findall(r"attribute\(\s*['\"]([^'\"]+)['\"]\s*\)", expression))
    fields.update(re.findall(r"[A-Za-z_@][A-Za-z0-9_:@]*", expression))
    return fields


@lru_cache(maxsize=1)
def template_references():
    """Поля, на которые ссылаются слои шаблона, по имени файла данных."""
    path = Path(template_path())
    if not path.exists():
        return {}
    try:
        with zipfile.ZipFile(path) as archive:
            name = next(
                item for item in archive.namelist() if item.endswith(".qgs")
            )
            text = archive.read(name).decode("utf-8")
    except (OSError, KeyError, StopIteration, zipfile.BadZipFile):
        return {}
    result = {}
    expression_attributes = re.compile(
        r'\b(?:fieldName|filter|expr|expression|geometryGenerator|'
        r'constraintExpression|formula|attr|displayExpression|'
        r'subsetString)="([^"]*)"'
    )
    property_field = re.compile(
        r'<property\b[^>]*\bfield="([^"]+)"'
    )
    for block in _maplayer_blocks(text):
        source = re.search(r"<datasource>([^<]+)</datasource>", block)
        if source:
            filename = source.group(1).split("|", 1)[0]
            stem = Path(filename).stem
        else:
            layer = re.search(r"<layername>([^<]+)</layername>", block)
            if not layer:
                continue
            stem = layer.group(1)
        references = result.setdefault(stem, set())
        for match in expression_attributes.finditer(block):
            references.update(_fields_in_expression(match.group(1)))
        for match in property_field.finditer(block):
            references.add(html.unescape(match.group(1)))
        for match in re.finditer(
            r"attribute\(\s*['\"]([^'\"]+)['\"]\s*\)", block
        ):
            references.add(html.unescape(match.group(1)))
    return result


def select_for_path(destination, available, include_common=False):
    """Поля слоя по ссылкам шаблона; ``None`` — слоя в шаблоне нет."""
    references = template_references().get(Path(destination).stem)
    if references is None:
        return None
    keep = set(references)
    if include_common:
        keep.update(COMMON_FIELDS)
    return [
        name for name in available
        if name in keep or is_essential(name)
    ]


def merge_selection(primary, extra, available):
    """Объединяет два отбора, сохраняя порядок полей источника."""
    keep = set(extra or ())
    keep.update(primary or ())
    return [name for name in available if name in keep]
