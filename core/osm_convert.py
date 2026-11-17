import json
import xml.etree.ElementTree as ET
from pathlib import Path


# Площадными считаются замкнутые way с ключами из списка GDAL
# (osmconf.ini, closed_ways_are_polygons).  Тот же список применяет
# драйвер GDAL в режиме PBF, поэтому один и тот же объект приходит
# площадью в обоих режимах: остров, граница региона, площадка спорта.
AREA_KEYS = frozenset({
    "aeroway", "amenity", "boundary", "building", "craft", "geological",
    "historic", "landuse", "leisure", "military", "natural", "office",
    "place", "shop", "sport", "tourism",
})
# Точные пары «ключ=значение» из того же списка GDAL.
AREA_PAIRS = frozenset({"highway=platform", "public_transport=platform"})

# Расширения списка GDAL: эти way в GDAL остаются линиями, но слой
# строит по ним площади.
AREA_MAN_MADE = {
    "pier", "breakwater", "bridge", "tower", "water_tower", "reservoir_covered",
    "wastewater_plant", "works", "lighthouse",
}
AREA_POWER = {"plant", "substation", "generator"}


def _tags(element):
    result = {}
    for tag in element.findall("tag"):
        key = tag.get("k")
        value = tag.get("v")
        if key is not None and value is not None:
            result[key] = value
    return result


class FieldNames:
    """Имена полей, уникальные без учёта регистра.

    SQLite не различает регистр имён колонок, поэтому теги вроде
    `communication:5G` и `communication:5g` приводят к ошибке создания
    таблицы, и слой молча теряется. Совпадающие имена переименуются
    одинаково для всех объектов слоя.
    """

    def __init__(self, priority=("name",)):
        self._mapping = {}
        self._used = set()
        for name in priority:
            self.resolve(name)

    def resolve(self, name):
        target = self._mapping.get(name)
        if target is not None:
            return target
        target = name
        lowered = target.lower()
        if lowered in self._used:
            index = 2
            while f"{lowered}{index}" in self._used:
                index += 1
            target = f"{name}{index}"
            lowered = target.lower()
        self._mapping[name] = target
        self._used.add(lowered)
        return target

    def apply(self, properties):
        """Свойства с уникальными именами; исходный словарь, если всё ок."""
        resolved = {}
        for key, value in properties.items():
            resolved[self.resolve(key)] = value
        if resolved == properties:
            return properties
        return resolved


def _is_area(tags):
    """Площадной ли замкнутый way.

    Правило — как в драйвере GDAL: площадью считается замкнутый way с
    любым из ключей ``AREA_KEYS`` или с точной парой «ключ=значение» из
    ``AREA_PAIRS``.  Тег ``area`` решает всё сам: ``area=no`` запрещает
    площадь, ``area=yes`` — требует.
    """
    if tags.get("area") == "yes":
        return True
    if tags.get("area") == "no":
        return False
    if tags.get("type") in ("multipolygon", "boundary"):
        return True
    if "building" in tags or "building:part" in tags:
        return True
    for key, value in tags.items():
        if key in AREA_KEYS:
            return True
        if f"{key}={value}" in AREA_PAIRS:
            return True
    if tags.get("man_made") in AREA_MAN_MADE:
        return True
    if tags.get("power") in AREA_POWER:
        return True
    if tags.get("waterway") in ("riverbank", "dock"):
        return True
    return False


def _coords(node_ids, nodes):
    result = []
    for node_id in node_ids:
        node = nodes.get(node_id)
        if node is None:
            return None
        result.append([node[0], node[1]])
    return result


def _ring_is_closed(ring):
    return len(ring) >= 4 and ring[0] == ring[-1]


def _assemble_rings(way_ids, ways, nodes):
    """Кольца мультиполигона из его way.

    Продолжение кольца ищется по индексу «конец отрезка → кандидаты»,
    поэтому сборка линейна по числу way.  Раньше кандидаты перебирались
    по всему списку, и мультиполигон в несколько тысяч way собирался
    квадратично.
    """
    pending = []
    for way_id in way_ids:
        way = ways.get(way_id)
        if way is None:
            continue
        coords = _coords(way["refs"], nodes)
        if coords and len(coords) >= 2:
            pending.append(coords)
    # Конец отрезка → кандидаты, у которых этот конец началом или концом.
    index = {}
    for position, coords in enumerate(pending):
        index.setdefault(tuple(coords[0]), []).append(position)
        index.setdefault(tuple(coords[-1]), []).append(position)
    used = [False] * len(pending)
    rings = []
    for position, coords in enumerate(pending):
        if used[position]:
            continue
        used[position] = True
        ring = list(coords)
        while not _ring_is_closed(ring):
            last = tuple(ring[-1])
            for candidate in index.get(last, ()):
                if used[candidate]:
                    continue
                other = pending[candidate]
                if other[0] == ring[-1]:
                    ring.extend(other[1:])
                elif other[-1] == ring[-1]:
                    ring.extend(reversed(other[:-1]))
                else:
                    continue
                used[candidate] = True
                break
            else:
                break
        if _ring_is_closed(ring):
            rings.append(ring)
    return rings


def _ring_contains(ring, point):
    x, y = point
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _relation_geometry(members, ways, nodes, tags):
    if tags.get("type") not in ("multipolygon", "boundary", "route"):
        return None
    way_members = [member for member in members if member[0] == "way"]
    if not way_members:
        return None
    if tags.get("type") == "route":
        lines = []
        for _, ref, _ in way_members:
            way = ways.get(ref)
            if way is None:
                continue
            coords = _coords(way["refs"], nodes)
            if coords and len(coords) >= 2:
                lines.append(coords)
        if not lines:
            return None
        return {"type": "MultiLineString" if len(lines) > 1 else "LineString",
                "coordinates": lines if len(lines) > 1 else lines[0]}

    outer_ids = [ref for kind, ref, role in way_members if kind == "way" and role in ("outer", "")]
    inner_ids = [ref for kind, ref, role in way_members if kind == "way" and role == "inner"]
    outer_rings = _assemble_rings(outer_ids, ways, nodes)
    inner_rings = _assemble_rings(inner_ids, ways, nodes)
    if not outer_rings:
        return None

    polygons = [[outer] for outer in outer_rings]
    for inner in inner_rings:
        candidates = [
            index for index, polygon in enumerate(polygons)
            if _ring_contains(polygon[0], inner[0])
        ]
        if candidates:
            target = min(candidates, key=lambda index: abs(_ring_area(polygons[index][0])))
            polygons[target].append(inner)
        elif polygons:
            polygons[0].append(inner)

    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _ring_area(ring):
    area = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index]
        x2, y2 = ring[index + 1]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _feature(osm_type, osm_id, tags, geometry, field_names=None):
    properties = dict(tags)
    properties["@id"] = f"{osm_type}/{osm_id}"
    properties["@type"] = osm_type
    if field_names is not None:
        properties = field_names.apply(properties)
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": geometry,
    }


def _iter_features(source, progress=None, cancel=None, field_names=None):
    """Объекты OSM по одному.

    Объекты отдаются по мере готовности, а не списком: на большой
    выгрузке список из сотен тысяч признаков занимал больше памяти,
    чем сами данные.  Порядок тот же: узлы, затем way, затем отношения.
    """
    nodes = {}
    node_tags = {}
    ways = {}
    relations = []
    count = 0

    for event, element in ET.iterparse(str(source), events=("end",)):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Conversion cancelled")
        if element.tag == "node":
            node_id = int(element.get("id"))
            nodes[node_id] = (
                round(float(element.get("lon")), 7),
                round(float(element.get("lat")), 7),
            )
            tags = _tags(element)
            if tags:
                node_tags[node_id] = tags
            element.clear()
        elif element.tag == "way":
            way_id = int(element.get("id"))
            refs = [int(nd.get("ref")) for nd in element.findall("nd")]
            ways[way_id] = {"refs": refs, "tags": _tags(element)}
            element.clear()
        elif element.tag == "relation":
            relation_id = int(element.get("id"))
            members = [
                (member.get("type"), int(member.get("ref")), member.get("role", ""))
                for member in element.findall("member")
            ]
            relations.append((relation_id, members, _tags(element)))
            element.clear()
        count += 1
        if progress is not None and count % 10000 == 0:
            progress(f"Parsed {count} objects")

    for node_id, tags in node_tags.items():
        lon, lat = nodes[node_id]
        yield _feature(
            "node", node_id, tags,
            {"type": "Point", "coordinates": [lon, lat]},
            field_names,
        )
    node_tags.clear()

    for way_id, way in ways.items():
        # Теги way отношениям не нужны — освобождаем их сразу.
        tags = way.pop("tags", None)
        if not tags:
            continue
        coords = _coords(way["refs"], nodes)
        if not coords or len(coords) < 2:
            continue
        if _ring_is_closed(coords) and _is_area(tags):
            geometry = {"type": "Polygon", "coordinates": [coords]}
        else:
            geometry = {"type": "LineString", "coordinates": coords}
        yield _feature("way", way_id, tags, geometry, field_names)

    for relation_id, members, tags in relations:
        if not tags:
            continue
        geometry = _relation_geometry(members, ways, nodes, tags)
        if geometry is not None:
            yield _feature(
                "relation", relation_id, tags, geometry, field_names
            )


def convert_osm_to_features(source, progress=None, cancel=None, field_names=None):
    """Все объекты OSM списком."""
    return list(
        _iter_features(
            source, progress=progress, cancel=cancel,
            field_names=field_names,
        )
    )


def convert_osm_xml(source, destination, progress=None, cancel=None):
    """Пишет объекты OSM в GeoJSON, не держа их всех в памяти."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as handle:
        handle.write('{"type":"FeatureCollection","features":[')
        for feature in _iter_features(
            source, progress=progress, cancel=cancel,
            field_names=FieldNames(),
        ):
            if count:
                handle.write(",")
            json.dump(
                feature, handle, ensure_ascii=False, separators=(",", ":")
            )
            count += 1
        handle.write("]}")
    return count
