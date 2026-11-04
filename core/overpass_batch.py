"""Объединение запросов Overpass и разделение ответа по слоям.

Многие слои проекта запрашиваются одним и тем же фильтром по тегам.
Вместо 160-170 отдельных HTTP-запросов можно отправить несколько объединённых
запросов (по объединению фильтров) и разложить один ответ по слоям на
стороне клиента: условия запросов — это проверка тегов объекта, поэтому
их можно повторить на Python.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path


class BatchError(RuntimeError):
    pass


_STATEMENT = re.compile(
    r"\b(node|way|relation)\s*((?:\[[^\]]*\]\s*)*);"
)
_CONDITION = re.compile(
    r'\["([^"]+)"\s*(?:(!=|=|!~|~)\s*"([^"]*)")?\]'
)
_REMAINDER = re.compile(r"[\s()\[\];]")


class Selector:
    """Одно условие отбора объектов: тип элемента и список тегов."""

    def __init__(self, kind, conditions):
        self.kind = kind
        self.conditions = list(conditions)

    def matches(self, kind, tags):
        if kind != self.kind:
            return False
        for key, operator, value in self.conditions:
            actual = tags.get(key)
            if operator == "exists":
                if key not in tags:
                    return False
            elif operator == "=":
                if actual != value:
                    return False
            elif operator == "!=":
                if actual == value:
                    return False
            elif operator == "~":
                if actual is None or not re.search(value, actual):
                    return False
            elif operator == "!~":
                if actual is not None and re.search(value, actual):
                    return False
        return True

    def to_overpass(self):
        conditions = "".join(
            f'["{key}"{operator}"{value}"]'
            if operator != "exists" else f'["{key}"]'
            for key, operator, value in self.conditions
        )
        return f"{self.kind}{conditions};"


def parse_selectors(query_text):
    """Разбирает текст запроса в список селекторов.

    Возвращает None, если запрос использует синтаксис, который нельзя
    повторить на клиенте (например, ``->.relations``): такие запросы
    выполняются как обычно, по одному.
    """
    text = re.sub(r"#[^\n]*", "", query_text or "")
    statements = list(_STATEMENT.finditer(text))
    if not statements:
        return None
    remainder = _STATEMENT.sub("", text)
    remainder = _REMAINDER.sub("", remainder)
    if remainder:
        return None
    selectors = []
    for match in statements:
        raw = match.group(2) or ""
        conditions = []
        for key, operator, value in _CONDITION.findall(raw):
            conditions.append((key, operator or "exists", value or ""))
        # Неопознанные условия — запрос не поддерживаем.
        if _CONDITION.sub("", raw).strip():
            return None
        selectors.append(Selector(match.group(1), conditions))
    return selectors


def build_batch_query(selectors, bbox):
    """Собирает один запрос Overpass по объединению фильтров."""
    bbox_part = bbox.as_overpass() if bbox is not None else ""
    header = "[out:xml][timeout:3600][maxsize:2000000000]"
    if bbox_part:
        header += f"[bbox:{bbox_part}]"
    body = "(\n" + "\n".join(
        selector.to_overpass() for selector in selectors
    ) + "\n);"
    return f"{header};\n{body}\nout body;>;out skel qt;"


def _tags(element):
    result = {}
    for tag in element.findall("tag"):
        key = tag.get("k")
        value = tag.get("v")
        if key is not None and value is not None:
            result[key] = value
    return result


def _read_elements(path, cancel=None):
    """Читает объединённый ответ: теги, узлы, линии, отношения."""
    nodes = {}
    ways = {}
    relations = {}
    for _event, element in ET.iterparse(str(path), events=("end",)):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Overpass batch cancelled")
        if element.tag == "node":
            node_id = int(element.get("id"))
            tags = _tags(element)
            existing = nodes.get(node_id)
            # Узел может прийти дважды: с тегами (body) и без них (skel).
            if existing is None or (tags and not existing[2]):
                nodes[node_id] = (
                    float(element.get("lon")),
                    float(element.get("lat")),
                    tags or (existing[2] if existing else None),
                )
            element.clear()
        elif element.tag == "way":
            way_id = int(element.get("id"))
            refs = [int(nd.get("ref")) for nd in element.findall("nd")]
            tags = _tags(element)
            existing = ways.get(way_id)
            if existing is None:
                ways[way_id] = {"refs": refs, "tags": tags}
            else:
                if refs and not existing["refs"]:
                    existing["refs"] = refs
                if tags and not existing["tags"]:
                    existing["tags"] = tags
            element.clear()
        elif element.tag == "relation":
            relation_id = int(element.get("id"))
            members = [
                (member.get("type"), int(member.get("ref")), member.get("role", ""))
                for member in element.findall("member")
            ]
            tags = _tags(element)
            existing = relations.get(relation_id)
            if existing is None:
                relations[relation_id] = {"members": members, "tags": tags}
            else:
                if members and not existing["members"]:
                    existing["members"] = members
                if tags and not existing["tags"]:
                    existing["tags"] = tags
            element.clear()
    return nodes, ways, relations


def _write_layer(path, nodes, ways, relations, selectors):
    """Записывает объекты слоя вместе с необходимыми зависимостями."""
    matched_nodes = []
    matched_ways = []
    matched_relations = []
    for node_id, (_lon, _lat, tags) in nodes.items():
        if tags and any(
            selector.matches("node", tags) for selector in selectors
        ):
            matched_nodes.append(node_id)
    for way_id, way in ways.items():
        tags = way["tags"]
        if tags and any(
            selector.matches("way", tags) for selector in selectors
        ):
            matched_ways.append(way_id)
    for relation_id, relation in relations.items():
        tags = relation["tags"]
        if tags and any(
            selector.matches("relation", tags) for selector in selectors
        ):
            matched_relations.append(relation_id)

    needed_nodes = set(matched_nodes)
    needed_ways = set()
    for way_id in matched_ways:
        needed_ways.add(way_id)
        needed_nodes.update(ways[way_id]["refs"])
    for relation_id in matched_relations:
        for kind, ref, _role in relations[relation_id]["members"]:
            if kind == "way":
                needed_ways.add(ref)
            elif kind == "node":
                needed_nodes.add(ref)
    for way_id in list(needed_ways):
        way = ways.get(way_id)
        if way is not None:
            needed_nodes.update(way["refs"])

    tagged_nodes = set(matched_nodes)
    tagged_ways = set(matched_ways)
    tagged_relations = set(matched_relations)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write('<osm version="0.6" generator="QGIS-xtopo">\n')
        for node_id in sorted(needed_nodes):
            node = nodes.get(node_id)
            if node is None:
                continue
            lon, lat, tags = node
            if node_id in tagged_nodes:
                handle.write(
                    f'  <node id="{node_id}" lat="{lat!r}" lon="{lon!r}">\n'
                )
                for key, value in (tags or {}).items():
                    handle.write(
                        f'    <tag k="{_escape(key)}" v="{_escape(value)}"/>\n'
                    )
                handle.write("  </node>\n")
            else:
                handle.write(
                    f'  <node id="{node_id}" lat="{lat!r}" lon="{lon!r}"/>\n'
                )
        for way_id in sorted(needed_ways):
            way = ways.get(way_id)
            if way is None:
                continue
            tags = way["tags"] if way_id in tagged_ways else None
            handle.write(f'  <way id="{way_id}">\n')
            for ref in way["refs"]:
                handle.write(f'    <nd ref="{ref}"/>\n')
            for key, value in (tags or {}).items():
                handle.write(
                    f'    <tag k="{_escape(key)}" v="{_escape(value)}"/>\n'
                )
            handle.write("  </way>\n")
        for relation_id in sorted(tagged_relations):
            relation = relations[relation_id]
            handle.write(f'  <relation id="{relation_id}">\n')
            for kind, ref, role in relation["members"]:
                handle.write(
                    f'    <member type="{kind}" ref="{ref}" role="{_escape(role)}"/>\n'
                )
            for key, value in (relation["tags"] or {}).items():
                handle.write(
                    f'    <tag k="{_escape(key)}" v="{_escape(value)}"/>\n'
                )
            handle.write("  </relation>\n")
        handle.write("</osm>\n")
    return len(matched_nodes) + len(matched_ways) + len(matched_relations)


def _escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def split_batch(path, layers, progress=None, cancel=None):
    """Разделяет объединённый ответ по слоям.

    layers — словарь {имя слоя: [Selector, ...]}.  Возвращает
    {имя слоя: (путь, число объектов)}.
    """
    nodes, ways, relations = _read_elements(path, cancel=cancel)
    result = {}
    for name, selectors in layers.items():
        if progress:
            progress(f"Разделение ответа Overpass: {name}")
        count = _write_layer(
            Path(path).parent / f"{name}.osm",
            nodes, ways, relations, selectors,
        )
        result[name] = (Path(path).parent / f"{name}.osm", count)
    return result
