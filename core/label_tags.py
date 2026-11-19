"""Тип объекта подписи по тегам OSM.

Инверсия надписей (`core/basemap.py`) не применяется к двум типам
объектов: дорогам, нарисованным широкой полосой, и площадным дорогам.
Признак берётся из тегов объекта, а не из цвета надписи: цвет полосы
в шаблоне может поменяться, и исключение перестало бы работать.

Тип определяется по фильтру правила подписей: правило наследует
фильтры родителей, поэтому у надписи магистрали видны теги всех
правил выше.  Если фильтр ничего не говорит о типе объекта, смотрим
на данные слоя.
"""

import re

from qgis.core import QgsRuleBasedLabeling

# Классы дорог, которые в шаблоне нарисованы широкой полосой: надпись
# лежит на самой полосе, и обмен цветов её рассыпает.
WIDE_BAND_HIGHWAYS = frozenset({
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
})

# Площадь: её надпись инверсия меняет, даже если площадь нарисована
# площадной дорогой.
SQUARE = "square"

_FIELD = r"""(?:"(?P<quoted>[^"]+)"|(?P<plain>(?!(?:NOT|IN|AND|OR)\b)[A-Za-z_][\w:]*))"""

_EQUAL = re.compile(_FIELD + r"""\s*=\s*'(?P<value>[^']*)'""")
_IN = re.compile(
    _FIELD + r"""\s+IN\s*\((?P<values>[^)]*)\)""",
    re.IGNORECASE,
)
_PRESENT = re.compile(
    _FIELD + r"""\s+IS\s+NOT\s+NULL""",
    re.IGNORECASE,
)
_NOT = re.compile(r"\bNOT\b", re.IGNORECASE)
_JOIN = re.compile(r"\b(?:AND|OR)\b", re.IGNORECASE)


def rule_filters(labeling):
    """Условия отбора объектов по правилам подписей: ключ -> условие.

    Условие правила дополняется условиями родителей: надпись магистрали
    отбирается и группой, и правилом внутри неё.
    """
    filters = {}
    if not isinstance(labeling, QgsRuleBasedLabeling):
        return filters
    _walk_rules(labeling.rootRule(), "", filters)
    return filters


def _walk_rules(rule, inherited, filters):
    expression = rule.filterExpression()
    effective = expression
    if inherited:
        effective = "(%s) AND (%s)" % (inherited, expression)
    filters[rule.ruleKey()] = effective
    for child in rule.children():
        _walk_rules(child, effective, filters)


def _negated(expression, position):
    """Отрицается ли условие, начинающееся с позиции.

    NOT относится к условию, если между ним и условием нет ни AND,
    ни OR: `NOT "highway" = 'motorway'` отрицает сравнение, а
    `NOT "highway" = 'motorway' OR "highway" = 'service'` — нет.
    """
    before = expression[:position]
    matches = list(_NOT.finditer(before))
    if not matches:
        return False
    return _JOIN.search(before[matches[-1].end():]) is None


def filter_tags(expression):
    """Теги, которые ограничивает условие отбора: поле -> значения.

    Пустой словарь означает, что условие ничего не говорит о типе
    объекта.  Условия с отрицанием игнорируются: по ним тип не
    определить.
    """
    tags = {}
    if not expression:
        return tags
    for match in _EQUAL.finditer(expression):
        if _negated(expression, match.start()):
            continue
        field = match.group("quoted") or match.group("plain")
        tags.setdefault(field, set()).add(match.group("value"))
    for match in _IN.finditer(expression):
        if _negated(expression, match.start()):
            continue
        field = match.group("quoted") or match.group("plain")
        values = re.findall(r"'([^']*)'", match.group("values"))
        tags.setdefault(field, set()).update(values)
    for match in _PRESENT.finditer(expression):
        field = match.group("quoted") or match.group("plain")
        # Поле есть, значение неизвестно: пустое множество значений.
        tags.setdefault(field, set())
    return tags


def is_wide_band(tags):
    """Дорога, нарисованная широкой полосой.

    Класс дороги определяется по тегам объекта: надпись лежит на самой
    полосе, и обмен цветов её рассыпает.
    """
    values = tags.get("highway")
    if not values:
        return False
    return values <= WIDE_BAND_HIGHWAYS


def is_area_road(tags):
    """Площадная дорога: area:highway=* или highway=* с area=yes."""
    if "area:highway" in tags:
        return not is_square(tags)
    if tags.get("area") == {"yes"} and "highway" in tags:
        return not is_square(tags)
    return False


def is_square(tags):
    """Площадь place=square: её надпись инверсия меняет."""
    return SQUARE in tags.get("place", ())


def layer_is_area_road(layer, limit=50):
    """Слой состоит из площадных дорог.

    Смотрим на теги самих объектов: у площадной дороги есть
    ``area:highway`` или ``highway`` вместе с ``area=yes``.  Площади
    (``place=square``) площадными дорогами не считаются: их надписи
    инверсия меняет.
    """
    names = {field.name() for field in layer.fields()}
    area_tag = "area:highway" in names
    area_yes = {"area", "highway"} <= names
    if not area_tag and not area_yes:
        return False
    square = False
    road = False
    for count, feature in enumerate(layer.getFeatures()):
        if count >= limit:
            break
        if not square and "place" in names:
            square = square or feature["place"] == SQUARE
        if not road:
            if area_tag and feature["area:highway"]:
                road = True
            elif area_yes and feature["area"] == "yes" and feature["highway"]:
                road = True
        if square and road:
            break
    return road and not square


def excluded(tags, layer_area_road=False):
    """Исключить ли правило подписей из инверсии.

    Правило, про тип объекта которого ничего не сказано, определяется
    по слою: площадные дороги инверсия не меняет.
    """
    if is_wide_band(tags):
        return True
    if is_area_road(tags):
        return True
    if layer_area_road and not is_square(tags):
        return True
    return False
