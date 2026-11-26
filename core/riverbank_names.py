"""Удаление имён у речных полигонов, дублирующих подпись реки.

Полигон русла помечен в OSM как ``natural=water`` + ``water=river``
(устаревший вариант — ``waterway=riverbank``).  Такой полигон не
вычитается из слоя ``river``, поэтому линия реки остаётся и получает
собственную подпись.  Если у полигона то же имя, на карте появляются
две одинаковые подписи — на воде и на линии.

Шаг очищает у речного полигона все поля имён, если хотя бы один вариант
имени совпадает с именем пересекающей его линии реки.  Полигоны
``natural=water`` без речного тега не трогаются: их линия вычитается из
``river``, и подпись полигона остаётся единственной.
"""

from pathlib import Path

from osgeo import ogr

RIVER_LAYERS = ("river", "river_intermittent")
RIVERBANK_WATER = frozenset({"river", "riverbank"})
NAME_FIELDS = frozenset({
    "alt_name", "int_name", "old_name", "official_name", "loc_name",
    "short_name",
})
# Упрощение и сглаживание линии могут сдвинуть её за край узкого русла.
LINE_TOLERANCE = 0.0015


def enabled(config):
    """Нужен ли шаг: запрошены вода и хотя бы один слой рек."""
    queries = set(getattr(config, "array_queries", None) or ())
    return "water" in queries and bool(queries & set(RIVER_LAYERS))


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _is_name_field(name):
    base = name.lower().split(":", 1)[0]
    return base == "name" or base in NAME_FIELDS


def _normalize(value):
    result = []
    for part in _text(value).split(";"):
        text = " ".join(part.lower().replace("ё", "е").split())
        if text:
            result.append(text)
    return result


def _name_fields(definition):
    fields = []
    for position in range(definition.GetFieldCount()):
        name = definition.GetFieldDefn(position).GetName()
        if _is_name_field(name):
            fields.append((position, name))
    return fields


def _names(feature, fields):
    variants = set()
    for position, _name in fields:
        variants.update(_normalize(feature.GetField(position)))
    return variants


def _field_index(definition, name):
    for position in range(definition.GetFieldCount()):
        if definition.GetFieldDefn(position).GetName().lower() == name:
            return position
    return None


def _river_index(vector_dir, cancel=None):
    """Имена линий рек и их геометрия: вариант имени → список линий."""
    index = {}
    for layer_name in RIVER_LAYERS:
        path = Path(vector_dir) / f"{layer_name}.sqlite"
        if not path.exists():
            continue
        dataset = ogr.Open(str(path))
        if dataset is None:
            continue
        try:
            layer = dataset.GetLayer(0)
            if layer is None:
                continue
            fields = _name_fields(layer.GetLayerDefn())
            if not fields:
                continue
            for position, feature in enumerate(layer):
                if (
                    cancel is not None
                    and position % 1000 == 0
                    and cancel.is_set()
                ):
                    raise InterruptedError("Riverbank names cancelled")
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                variants = _names(feature, fields)
                if not variants:
                    continue
                shape = geometry.Clone()
                item = (shape, shape.GetEnvelope())
                for variant in variants:
                    index.setdefault(variant, []).append(item)
        finally:
            dataset = None
    return index


def _is_riverbank(feature, water, waterway):
    if water is not None:
        value = _text(feature.GetField(water)).lower()
        if value in RIVERBANK_WATER:
            return True
    if waterway is not None:
        value = _text(feature.GetField(waterway)).lower()
        if value == "riverbank":
            return True
    return False


def _line_matches(geometry, variants, index):
    checked = set()
    for variant in variants:
        for item in index.get(variant, ()):
            key = id(item)
            if key in checked:
                continue
            checked.add(key)
            shape, _envelope = item
            if geometry.Intersects(shape):
                return True
            if geometry.Distance(shape) <= LINE_TOLERANCE:
                return True
    return False


def apply(vector_dir, cancel=None):
    """Убирает имена речных полигонов, совпавшие с линией реки.

    Возвращает словарь со счётчиками: ``checked`` — речных полигонов с
    именем, ``cleared`` — сколько имён удалено.
    """
    ogr.UseExceptions()
    water_path = Path(vector_dir) / "water.sqlite"
    if not water_path.exists():
        return {"checked": 0, "cleared": 0}
    index = _river_index(vector_dir, cancel=cancel)
    if not index:
        return {"checked": 0, "cleared": 0}
    dataset = ogr.Open(str(water_path), 1)
    if dataset is None:
        return {"checked": 0, "cleared": 0}
    checked = cleared = 0
    try:
        layer = dataset.GetLayer(0)
        if layer is None:
            return {"checked": 0, "cleared": 0}
        definition = layer.GetLayerDefn()
        fields = _name_fields(definition)
        if not fields:
            return {"checked": 0, "cleared": 0}
        water = _field_index(definition, "water")
        waterway = _field_index(definition, "waterway")
        if water is None and waterway is None:
            return {"checked": 0, "cleared": 0}
        layer.StartTransaction()
        try:
            for position, feature in enumerate(layer):
                if (
                    cancel is not None
                    and position % 500 == 0
                    and cancel.is_set()
                ):
                    raise InterruptedError("Riverbank names cancelled")
                if not _is_riverbank(feature, water, waterway):
                    continue
                variants = _names(feature, fields)
                if not variants:
                    continue
                checked += 1
                geometry = feature.GetGeometryRef()
                if geometry is None or geometry.IsEmpty():
                    continue
                if not _line_matches(geometry, variants, index):
                    continue
                for field, _name in fields:
                    feature.SetFieldNull(field)
                layer.SetFeature(feature)
                cleared += 1
        except Exception:
            layer.RollbackTransaction()
            raise
        layer.CommitTransaction()
    finally:
        dataset = None
    return {"checked": checked, "cleared": cleared}
