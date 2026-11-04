import json
import xml.etree.ElementTree as ET
from pathlib import Path


AREA_NATURAL = {
    "water", "wood", "scrub", "grass", "wetland", "bare_rock", "scree",
    "sand", "beach", "glacier", "shingle", "reef", "shoal", "tundra",
    "desert", "crevasse_zone", "stone", "spring", "sinkhole",
}
AREA_LANDUSE = {
    "residential", "farmyard", "farmland", "vineyard", "cemetery", "orchard",
    "industrial", "quarry", "spoil_heap", "plant_nursery", "greenhouse_horticulture",
    "military", "allotments", "landfill", "logging", "prison", "resort",
    "marketplace", "railway", "forest", "meadow", "basin", "reservoir",
}
AREA_AMENITY = {
    "parking", "school", "university", "hospital", "place_of_worship",
    "grave_yard", "marketplace", "fuel", "camp_site", "picnic_site",
}
AREA_LEISURE = {
    "park", "garden", "playground", "sports_centre", "pitch", "golf_course",
    "nature_reserve", "common", "recreation_ground",
}
AREA_MAN_MADE = {
    "pier", "breakwater", "bridge", "tower", "water_tower", "reservoir_covered",
    "wastewater_plant", "works", "lighthouse",
}
AREA_POWER = {"plant", "substation", "generator"}
AREA_AEROWAY = {"aerodrome", "apron", "terminal", "hangar"}
AREA_TOURISM = {"zoo", "theme_park", "attraction", "camp_site", "hotel", "museum"}
AREA_HISTORIC = {"archaeological_site", "castle", "ruins", "fort", "monument", "memorial"}


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
    if tags.get("area") == "yes":
        return True
    if tags.get("area") == "no":
        return False
    if tags.get("type") in ("multipolygon", "boundary"):
        return True
    if "building" in tags or "building:part" in tags:
        return True
    if tags.get("landuse") in AREA_LANDUSE:
        return True
    if tags.get("natural") in AREA_NATURAL:
        return True
    if tags.get("amenity") in AREA_AMENITY:
        return True
    if tags.get("leisure") in AREA_LEISURE:
        return True
    if tags.get("man_made") in AREA_MAN_MADE:
        return True
    if tags.get("power") in AREA_POWER:
        return True
    if tags.get("aeroway") in AREA_AEROWAY:
        return True
    if tags.get("tourism") in AREA_TOURISM:
        return True
    if tags.get("historic") in AREA_HISTORIC:
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
    pending = []
    for way_id in way_ids:
        way = ways.get(way_id)
        if way is None:
            continue
        coords = _coords(way["refs"], nodes)
        if coords and len(coords) >= 2:
            pending.append(coords)
    rings = []
    while pending:
        ring = pending.pop(0)
        while not _ring_is_closed(ring):
            last = ring[-1]
            matched = False
            for index, candidate in enumerate(pending):
                if candidate[0] == last:
                    ring.extend(candidate[1:])
                    pending.pop(index)
                    matched = True
                    break
                if candidate[-1] == last:
                    ring.extend(reversed(candidate[:-1]))
                    pending.pop(index)
                    matched = True
                    break
            if not matched:
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


def convert_osm_to_features(source, progress=None, cancel=None, field_names=None):
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

    features = []
    for node_id, tags in node_tags.items():
        lon, lat = nodes[node_id]
        features.append(
            _feature(
                "node", node_id, tags,
                {"type": "Point", "coordinates": [lon, lat]},
                field_names,
            )
        )

    for way_id, way in ways.items():
        tags = way["tags"]
        if not tags:
            continue
        coords = _coords(way["refs"], nodes)
        if not coords or len(coords) < 2:
            continue
        if _ring_is_closed(coords) and _is_area(tags):
            geometry = {"type": "Polygon", "coordinates": [coords]}
        else:
            geometry = {"type": "LineString", "coordinates": coords}
        features.append(_feature("way", way_id, tags, geometry, field_names))

    for relation_id, members, tags in relations:
        if not tags:
            continue
        geometry = _relation_geometry(members, ways, nodes, tags)
        if geometry is not None:
            features.append(
                _feature("relation", relation_id, tags, geometry, field_names)
            )

    return features


def convert_osm_xml(source, destination, progress=None, cancel=None):
    features = convert_osm_to_features(
        source, progress=progress, cancel=cancel, field_names=FieldNames()
    )
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(
            {"type": "FeatureCollection", "features": features},
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return len(features)
