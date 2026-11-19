"""Тип объекта подписи по тегам OSM.

Инверсия надписей (`core/basemap.py`) не применяется к двум типам
объектов: дорогам, нарисованным широкой полосой, и площадным дорогам.
Признак берётся из тегов объекта, а не из цвета надписи: цвет полосы
в шаблоне может поменяться, и исключение перестало бы работать.

Тип определяется по условиям отбора: условие правила подписей (правило
наследует условия родителей) и условие слоя.  Если условия молчат,
смотрим на теги самих объектов слоя.
"""

import re
from dataclasses import dataclass

from qgis.core import QgsRuleBasedLabeling

# Классы дорог, которые в шаблоне нарисованы широкой полосой: надпись
# лежит на самой полосе, и обмен цветов её рассыпает.  Это магистрали
# и второстепенные дороги вместе с их примыканиями.
WIDE_BAND_HIGHWAYS = frozenset({
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
})

# Все классы дорог, которые встречаются в данных плагина: по ним
# вычисляется дополнение к условию вида «highway NOT IN (...)».
HIGHWAY_CLASSES = WIDE_BAND_HIGHWAYS | frozenset({
    "residential",
    "unclassified",
    "living_street",
    "pedestrian",
    "service",
})

# Площадь: её надпись инверсия меняет, даже если площадь нарисована
# площадной дорогой.
SQUARE = "square"

_FIELD = r"""(?:"(?P<quoted>[^"]+)"|(?P<plain>(?!(?:NOT|IN|AND|OR)\b)[A-Za-z_][\w:]*))"""

_EQUAL = re.compile(_FIELD + r"""\s*=\s*'(?P<value>[^']*)'""")
_IN = re.compile(
    _FIELD + r"""\s+(?P<neg>NOT\s+)?IN\s*\((?P<values>[^)]*)\)""",
    re.IGNORECASE,
)
_PRESENT = re.compile(
    _FIELD + r"""\s+IS\s+NOT\s+NULL""",
    re.IGNORECASE,
)
_NOT = re.compile(r"\bNOT\b", re.IGNORECASE)
_JOIN = re.compile(r"\b(?:AND|OR)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Profile:
    """Тип объектов по тегам OSM.

    classes — классы дорог, которые отбирает условие (None — условие
    не позволяет их определить); area_road — площадная дорога;
    square — площадь.
    """

    classes: frozenset = None
    area_road: bool = False
    square: bool = False


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


def _classes(values, excluded):
    """Классы дорог, которые отбирает условие; None — не определены."""
    if values and excluded:
        return frozenset(values) - frozenset(excluded)
    if values:
        return frozenset(values)
    if excluded:
        # Условие исключает классы: остаются все прочие.
        return HIGHWAY_CLASSES - frozenset(excluded)
    return None


def profile(expression):
    """Профиль объектов, которые отбирает условие отбора.

    Отрицание не перечисляет значения, а исключает их: набор классов
    восстанавливается по списку известных классов дорог.
    """
    if not expression:
        return Profile()
    values = {}
    excluded = {}
    for match in _EQUAL.finditer(expression):
        field = match.group("quoted") or match.group("plain")
        if _negated(expression, match.start()):
            excluded.setdefault(field, set()).add(match.group("value"))
        else:
            values.setdefault(field, set()).add(match.group("value"))
    for match in _IN.finditer(expression):
        field = match.group("quoted") or match.group("plain")
        items = set(re.findall(r"'([^']*)'", match.group("values")))
        if match.group("neg"):
            excluded.setdefault(field, set()).update(items)
        else:
            values.setdefault(field, set()).update(items)
    for match in _PRESENT.finditer(expression):
        field = match.group("quoted") or match.group("plain")
        # Поле есть, значение неизвестно.
        values.setdefault(field, set())
    area_road = "area:highway" in values or "area:highway" in excluded
    if not area_road and values.get("area") == {"yes"} and values.get("highway"):
        area_road = True
    return Profile(
        classes=_classes(values.get("highway"), excluded.get("highway")),
        area_road=area_road,
        square=SQUARE in values.get("place", ()),
    )


def layer_profile(layer, limit=50):
    """Профиль объектов слоя по тегам.

    Условие слоя прямо говорит о типах объектов (так разделены
    магистрали и улицы); если его нет или оно молчит, смотрим на теги
    самих объектов.
    """
    subset = profile(layer.subsetString())
    names = {field.name() for field in layer.fields()}
    classes = subset.classes
    area_road = subset.area_road
    square = subset.square
    need_data = (
        (classes is None and "highway" in names)
        or "area:highway" in names
        or {"area", "highway"} <= names
    )
    if need_data:
        data_classes, data_area, data_square = _sample(layer, names, limit)
        if classes is None:
            classes = data_classes
        area_road = area_road or data_area
        square = square or data_square
    return Profile(
        classes=classes, area_road=area_road, square=square
    )


def _sample(layer, names, limit):
    """Профиль объектов слоя по тегам самих объектов."""
    classes = set()
    area_road = False
    square = False
    has_area = "area:highway" in names
    has_area_yes = {"area", "highway"} <= names
    for count, feature in enumerate(layer.getFeatures()):
        if count >= limit:
            break
        if "highway" in names and feature["highway"]:
            classes.add(feature["highway"])
        if "place" in names and feature["place"] == SQUARE:
            square = True
        if has_area and feature["area:highway"]:
            area_road = True
        elif has_area_yes and feature["area"] == "yes" and feature["highway"]:
            area_road = True
    return (frozenset(classes) if classes else None), area_road, square


def combine(rule, layer):
    """Профиль объектов правила подписей в слое.

    Правило отбирает часть объектов слоя, поэтому класс дороги берём
    из правила, а из слоя — только когда правило о дорогах не говорит.
    """
    classes = rule.classes
    if classes is None:
        classes = layer.classes
    return Profile(
        classes=classes,
        area_road=rule.area_road or layer.area_road,
        square=rule.square or layer.square,
    )


def excluded(item):
    """Исключить ли объекты из инверсии.

    Дороги, нарисованные широкой полосой, и площадные дороги: их
    надписи лежат на самой дороге, и обмен цветов их рассыпает.
    Площади под исключение не попадают: их названия инверсия меняет.
    """
    if item.classes and item.classes <= WIDE_BAND_HIGHWAYS:
        return True
    if item.square:
        return False
    return item.area_road
