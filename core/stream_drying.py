"""Подсыхание ручьёв: статический ранг видимости по сети.

Слой ручьёв разбивается на ветви в притоках и растворяется по значимым
для отрисовки тегам.  Верхняя часть потоков скрывается от истока:
250 м на 1:150000 и 700 м на 1:200000.  Обрезка останавливается на
T-развилке, но продолжается ниже, если все её боковые ветви уже
удалены.  Результат
записывается в поле ``dry_rank``, по которому фильтры шаблона скрывают
мелкие водотоки на обзорных масштабах.

Отдельно защищаются ручьи у источников: рядом с родником, у выхода из
водоёма или болота.  Защищённой главной линии ставится максимальный
ранг, и он распространяется вниз по главному продолжению, чтобы не
появилось разрывов.
"""

import bisect
import heapq
import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

FIELD = "dry_rank"
LAYERS = ("stream", "stream_intermittent")
ARROW_LAYER = "stream_smoothed"
# Порог в метрах для масштабов 1:150000 и 1:200000.
THRESHOLDS = (250.0, 700.0)
MAX_RANK = len(THRESHOLDS)
SNAP = 10.0
TJOIN = 5.0
SPRING_RADIUS = 100.0
OUTLET_TOLERANCE = 30.0
# Разрыв у устья: полигон воды иногда срезает последние метры ручья.
MOUTH_SNAP = 40.0
MOUTH_MAX_LENGTH = 300.0
# Допускаем плавный или шумный поворот, но не разворот линии.
MOUTH_MIN_DOT = -0.5
COASTLINE_LAYER = "coastline"
WATER_LAYERS = ("water_without_riverbanks", "water_dissolved")


class _Piece:
    """Кусок исходной линии между узлами."""

    __slots__ = (
        "group", "waterway", "intermittent", "tunnel", "layer", "sig",
        "attrs", "points", "length", "up", "down",
    )


class _Branch:
    """Растворённая ветвь между развилками."""

    __slots__ = (
        "group", "waterway", "intermittent", "tunnel", "layer", "sig",
        "attrs", "points", "length", "up", "down",
    )


class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        parent = self.parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, first, second):
        first = self.find(first)
        second = self.find(second)
        if first != second:
            self.parent[second] = first


def _text(value):
    return "" if value is None else str(value).strip()


def _distance(first, second):
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _length(points):
    return sum(
        _distance(first, second)
        for first, second in zip(points, points[1:])
    )


def _cumulative(points):
    values = [0.0]
    total = 0.0
    for first, second in zip(points, points[1:]):
        total += _distance(first, second)
        values.append(total)
    return values


def _point_at(points, cumulative, distance):
    if distance <= 0:
        return points[0]
    if distance >= cumulative[-1]:
        return points[-1]
    index = bisect.bisect_right(cumulative, distance) - 1
    index = max(0, min(index, len(points) - 2))
    start = cumulative[index]
    span = cumulative[index + 1] - start
    if span <= 1e-12:
        return points[index]
    ratio = (distance - start) / span
    first = points[index]
    second = points[index + 1]
    return (
        first[0] + (second[0] - first[0]) * ratio,
        first[1] + (second[1] - first[1]) * ratio,
    )


def _slice(points, cumulative, start, end):
    result = [_point_at(points, cumulative, start)]
    index = bisect.bisect_right(cumulative, start)
    while index < len(points) and cumulative[index] < end - 1e-9:
        result.append(points[index])
        index += 1
    result.append(_point_at(points, cumulative, end))
    cleaned = [result[0]]
    for point in result[1:]:
        if _distance(point, cleaned[-1]) > 1e-9:
            cleaned.append(point)
    return cleaned if len(cleaned) >= 2 else []


def _split_piece(points, cuts):
    """Делит линию в заданных расстояниях от начала."""
    cumulative = _cumulative(points)
    total = cumulative[-1]
    if total <= 1e-9:
        return []
    values = sorted({
        round(value, 4) for value in cuts
        if 0.01 < value < total - 0.01
    })
    if not values:
        return [points]
    bounds = [0.0] + values + [total]
    parts = []
    for start, end in zip(bounds, bounds[1:]):
        if end - start <= 0.5:
            continue
        part = _slice(points, cumulative, start, end)
        if len(part) >= 2 and _length(part) > 0.01:
            parts.append(part)
    return parts


def _point_to_polyline(points, point):
    """Расстояние до линии и расстояние вдоль неё до ближайшей точки."""
    best = (float("inf"), 0.0)
    along = 0.0
    px, py = point
    for first, second in zip(points, points[1:]):
        ax, ay = first
        bx, by = second
        dx = bx - ax
        dy = by - ay
        length_sq = dx * dx + dy * dy
        length = math.sqrt(length_sq)
        if length_sq <= 1e-18:
            distance = math.hypot(px - ax, py - ay)
            if distance < best[0]:
                best = (distance, along)
            continue
        ratio = ((px - ax) * dx + (py - ay) * dy) / length_sq
        ratio = max(0.0, min(1.0, ratio))
        qx = ax + dx * ratio
        qy = ay + dy * ratio
        distance = math.hypot(px - qx, py - qy)
        if distance < best[0]:
            best = (distance, along + length * ratio)
        along += length
    return best


def _geometry_parts(geometry):
    """Линейные части геометрии как списки координат."""
    if geometry is None or geometry.IsEmpty():
        return []
    kind = geometry.GetGeometryName()
    if kind == "LINESTRING":
        get_points = getattr(geometry, "GetPoints", None)
        if get_points is not None:
            points = get_points()
            if points and len(points[0]) > 2:
                points = [(point[0], point[1]) for point in points]
        else:
            points = [
                (geometry.GetX(index), geometry.GetY(index))
                for index in range(geometry.GetPointCount())
            ]
        cleaned = [points[0]] if points else []
        for point in points[1:]:
            if _distance(point, cleaned[-1]) > 1e-9:
                cleaned.append(point)
        return [cleaned] if len(cleaned) >= 2 else []
    if kind in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
        result = []
        for index in range(geometry.GetGeometryCount()):
            result.extend(_geometry_parts(geometry.GetGeometryRef(index)))
        return result
    return []


def _geometry_points(geometry):
    """Точки точечной геометрии."""
    if geometry is None or geometry.IsEmpty():
        return []
    kind = geometry.GetGeometryName()
    if kind == "POINT":
        return [(geometry.GetX(), geometry.GetY())]
    if kind in ("MULTIPOINT", "GEOMETRYCOLLECTION"):
        result = []
        for index in range(geometry.GetGeometryCount()):
            result.extend(_geometry_points(geometry.GetGeometryRef(index)))
        return result
    return []


def _layer_fields(layer):
    definition = layer.GetLayerDefn()
    return [
        definition.GetFieldDefn(index)
        for index in range(definition.GetFieldCount())
    ]


def _read_input(path):
    """Читает слой и возвращает поля, СК и исходные линейные куски."""
    from osgeo import ogr

    source = ogr.Open(str(path))
    if source is None:
        raise OSError(f"Cannot open {path}")
    layer = source.GetLayer(0)
    definitions = _layer_fields(layer)
    keep = [
        index for index, definition in enumerate(definitions)
        if definition.GetName().lower() != FIELD
    ]
    fields = []
    for index in keep:
        definition = definitions[index]
        field = ogr.FieldDefn(
            definition.GetName(), definition.GetType()
        )
        field.SetWidth(definition.GetWidth())
        field.SetPrecision(definition.GetPrecision())
        fields.append(field)
    index_of = {
        definitions[index].GetName().lower(): position
        for position, index in enumerate(keep)
    }
    srs = layer.GetSpatialRef()
    if srs is not None:
        srs = srs.Clone()
    group = "intermittent" if "intermittent" in path.stem else "stream"
    pieces = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        attrs = tuple(feature.GetField(index) for index in keep)
        values = {
            name: attrs[position]
            for name, position in index_of.items()
        }
        for points in _geometry_parts(geometry):
            piece = _Piece()
            piece.group = group
            piece.waterway = _text(values.get("waterway"))
            piece.intermittent = _text(values.get("intermittent")).lower()
            piece.tunnel = _text(values.get("tunnel")).lower()
            piece.layer = _text(values.get("layer"))
            piece.sig = (
                group, piece.waterway, piece.intermittent,
                piece.tunnel, piece.layer,
            )
            piece.attrs = attrs
            piece.points = points
            piece.length = _length(points)
            piece.up = None
            piece.down = None
            pieces.append(piece)
    layer = None
    source = None
    return fields, srs, pieces


def _build_bbox_index(boxes):
    from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex, QgsRectangle

    index = QgsSpatialIndex()
    for position, (min_x, min_y, max_x, max_y) in enumerate(boxes):
        feature = QgsFeature(position)
        feature.setGeometry(QgsGeometry.fromRect(
            QgsRectangle(min_x, min_y, max_x, max_y)
        ))
        index.addFeature(feature)
    return index


def _branch_boxes(branches):
    boxes = []
    for branch in branches:
        xs = [point[0] for point in branch.points]
        ys = [point[1] for point in branch.points]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _polygon_boxes(polygons):
    boxes = []
    for geometry in polygons:
        min_x, max_x, min_y, max_y = geometry.GetEnvelope()
        boxes.append((min_x, min_y, max_x, max_y))
    return boxes


def _split_t_junctions(pieces, cancel=None):
    """Делит куски в местах, где конец другого куска попадает в середину."""
    from qgis.core import QgsRectangle

    boxes = []
    for piece in pieces:
        xs = [point[0] for point in piece.points]
        ys = [point[1] for point in piece.points]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    index = _build_bbox_index(boxes)
    cuts = defaultdict(set)
    for position, piece in enumerate(pieces):
        if cancel is not None and position % 500 == 0 and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        for point in (piece.points[0], piece.points[-1]):
            rectangle = QgsRectangle(
                point[0] - TJOIN, point[1] - TJOIN,
                point[0] + TJOIN, point[1] + TJOIN,
            )
            for other in index.intersects(rectangle):
                if other == position:
                    continue
                candidate = pieces[other]
                if _distance(point, candidate.points[0]) <= SNAP:
                    continue
                if _distance(point, candidate.points[-1]) <= SNAP:
                    continue
                distance, along = _point_to_polyline(
                    candidate.points, point
                )
                if distance <= TJOIN:
                    cuts[other].add(round(along, 3))
    result = []
    for position, piece in enumerate(pieces):
        for points in _split_piece(piece.points, cuts.get(position, ())):
            clone = _Piece()
            clone.group = piece.group
            clone.waterway = piece.waterway
            clone.intermittent = piece.intermittent
            clone.tunnel = piece.tunnel
            clone.layer = piece.layer
            clone.sig = piece.sig
            clone.attrs = piece.attrs
            clone.points = points
            clone.length = _length(points)
            result.append(clone)
    return result


def _node_pieces(pieces):
    """Склеивает концы кусков в узлы с допуском SNAP."""
    points = []
    for position, piece in enumerate(pieces):
        points.append((piece.points[0], position, 0))
        points.append((piece.points[-1], position, 1))
    union = _UnionFind(len(points))
    buckets = defaultdict(list)
    for index, (point, _position, _side) in enumerate(points):
        key = (
            math.floor(point[0] / SNAP),
            math.floor(point[1] / SNAP),
        )
        buckets[key].append(index)
    for index, (point, _position, _side) in enumerate(points):
        cell_x = math.floor(point[0] / SNAP)
        cell_y = math.floor(point[1] / SNAP)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in buckets.get((cell_x + dx, cell_y + dy), ()):
                    if other <= index:
                        continue
                    if _distance(point, points[other][0]) <= SNAP:
                        union.union(index, other)
    roots = {}
    node_points = {}
    for index, (point, _position, _side) in enumerate(points):
        root = union.find(index)
        if root not in roots:
            roots[root] = len(roots)
            node_points[roots[root]] = point
    for position, piece in enumerate(pieces):
        piece.up = roots[union.find(position * 2)]
        piece.down = roots[union.find(position * 2 + 1)]
        piece.points[0] = node_points[piece.up]
        piece.points[-1] = node_points[piece.down]
        piece.length = _length(piece.points)
    incident = defaultdict(list)
    for position, piece in enumerate(pieces):
        incident[piece.up].append((position, 0))
        incident[piece.down].append((position, 1))
    return incident


def _dissolve(pieces, incident):
    """Растворяет цепочки через узлы степени 2 с теми же тегами."""
    visited = set()
    branches = []
    for start in range(len(pieces)):
        if start in visited:
            continue
        chain = [start]
        visited.add(start)
        head = []
        current = start
        while True:
            node = pieces[current].up
            if len(incident.get(node, ())) != 2:
                break
            candidates = [
                position for position, side in incident.get(node, ())
                if side == 1 and position != current
                and position not in visited
                and pieces[position].sig == pieces[current].sig
            ]
            if len(candidates) != 1:
                break
            current = candidates[0]
            head.append(current)
            visited.add(current)
        if head:
            chain = head[::-1] + chain
        current = start
        while True:
            node = pieces[current].down
            if len(incident.get(node, ())) != 2:
                break
            candidates = [
                position for position, side in incident.get(node, ())
                if side == 0 and position != current
                and position not in visited
                and pieces[position].sig == pieces[current].sig
            ]
            if len(candidates) != 1:
                break
            current = candidates[0]
            chain.append(current)
            visited.add(current)
        points = list(pieces[chain[0]].points)
        for position in chain[1:]:
            part = pieces[position].points
            if _distance(points[-1], part[0]) <= SNAP * 1.5:
                points.extend(part[1:])
            else:
                # Разрыв в цепочке не соединяем: ветви останутся
                # отдельными, но граф уже связан общим узлом.
                points.extend(part)
        length = sum(pieces[position].length for position in chain)
        branch = _Branch()
        first = pieces[chain[0]]
        branch.group = first.group
        branch.waterway = first.waterway
        branch.intermittent = first.intermittent
        branch.tunnel = first.tunnel
        branch.layer = first.layer
        branch.sig = first.sig
        branch.attrs = first.attrs
        branch.points = points
        branch.length = length
        branch.up = pieces[chain[0]].up
        branch.down = pieces[chain[-1]].down
        branches.append(branch)
    return branches


def _components(branches):
    union = _UnionFind(len(branches))
    node_branches = defaultdict(list)
    for position, branch in enumerate(branches):
        node_branches[branch.up].append(position)
        node_branches[branch.down].append(position)
    for positions in node_branches.values():
        unique = list(dict.fromkeys(positions))
        for position in unique[1:]:
            union.union(unique[0], position)
    groups = defaultdict(list)
    for position in range(len(branches)):
        groups[union.find(position)].append(position)
    totals = {}
    for root, positions in groups.items():
        totals[root] = sum(branches[position].length for position in positions)
    return groups, totals


def _attach_mouth_fragments(branches, followers, cancel=None):
    """Присоединяет оторванные устья к продолжению линии.

    Полигон воды иногда срезает последние метры ручья, и устье остаётся
    отдельной тупиковой ветвью.  Если его начало продолжает направление
    соседней ветви, устье получает её нижний узел и наследует ранг,
    вместо того чтобы считаться самостоятельным коротким ручьём.
    """
    from qgis.core import QgsRectangle

    if not branches:
        return 0
    boxes = []
    for branch in branches:
        if not branch.points:
            boxes.append((0.0, 0.0, 0.0, 0.0))
            continue
        x, y = branch.points[-1]
        boxes.append((
            x - MOUTH_SNAP, y - MOUTH_SNAP,
            x + MOUTH_SNAP, y + MOUTH_SNAP,
        ))
    index = _build_bbox_index(boxes)
    attached = 0
    for position, branch in enumerate(branches):
        if len(branch.points) < 2:
            continue
        if branch.length > MOUTH_MAX_LENGTH:
            continue
        if [
            item for item in followers.get(branch.down, ())
            if item != position
        ]:
            continue
        start = branch.points[0]
        rectangle = QgsRectangle(
            start[0] - MOUTH_SNAP, start[1] - MOUTH_SNAP,
            start[0] + MOUTH_SNAP, start[1] + MOUTH_SNAP,
        )
        best = None
        best_score = None
        for candidate_position in index.intersects(rectangle):
            if candidate_position == position:
                continue
            other = branches[candidate_position]
            if len(other.points) < 2:
                continue
            end = other.points[-1]
            distance = _distance(start, end)
            if distance > MOUTH_SNAP:
                continue
            if distance <= 1e-9 and branch.length <= 1e-9:
                dot = 1.0
            else:
                first_x = branch.points[1][0] - start[0]
                first_y = branch.points[1][1] - start[1]
                last_x = end[0] - other.points[-2][0]
                last_y = end[1] - other.points[-2][1]
                first_length = math.hypot(first_x, first_y)
                last_length = math.hypot(last_x, last_y)
                if first_length <= 1e-9 or last_length <= 1e-9:
                    continue
                dot = (
                    first_x * last_x + first_y * last_y
                ) / (first_length * last_length)
                if dot < MOUTH_MIN_DOT:
                    continue
            score = (dot, -distance, other.length)
            if best_score is None or score > best_score:
                best_score = score
                best = candidate_position
        if best is None:
            continue
        other = branches[best]
        old_up = branch.up
        if old_up in followers and position in followers[old_up]:
            followers[old_up].remove(position)
        branch.up = other.down
        branch.points[0] = other.points[-1]
        branch.length = _length(branch.points)
        followers[other.down].append(position)
        attached += 1
    return attached


def _removal_prefixes(branches, feeders, followers, threshold,
                      protected=(), cancel=None):
    """Сколько метров удалить сверху каждой ветви.

    Идём от истоков вниз.  Если ветвь удалена целиком и у развилки не
    осталось других активных ветвей, развилка исчезает и обрезка
    продолжается по примыкающей ветви.
    """
    count = len(branches)
    prefix = [0.0] * count
    active = [True] * count
    heap = []
    for position, branch in enumerate(branches):
        incoming = [
            item for item in feeders.get(branch.up, ())
            if item != position and active[item]
        ]
        if not incoming and position not in protected:
            heapq.heappush(heap, (-threshold, position))
    while heap:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        negative, position = heapq.heappop(heap)
        remaining = -negative
        branch = branches[position]
        if position in protected:
            continue
        if not active[position] or remaining <= prefix[position] + 1e-9:
            continue
        if remaining + 1e-9 >= branch.length:
            prefix[position] = branch.length
            active[position] = False
            leftover = remaining - branch.length
            if leftover <= 1e-9:
                continue
            for follower in followers.get(branch.down, ()):
                if follower == position or not active[follower]:
                    continue
                incoming = [
                    item for item in feeders.get(branches[follower].up, ())
                    if item != follower and active[item]
                ]
                if incoming:
                    continue
                if leftover > prefix[follower] + 1e-9:
                    heapq.heappush(heap, (-leftover, follower))
        else:
            prefix[position] = remaining
    return prefix


def _clone_branch(branch, points, length):
    clone = _Branch()
    clone.group = branch.group
    clone.waterway = branch.waterway
    clone.intermittent = branch.intermittent
    clone.tunnel = branch.tunnel
    clone.layer = branch.layer
    clone.sig = branch.sig
    clone.attrs = branch.attrs
    clone.points = points
    clone.length = length
    clone.up = branch.up
    clone.down = branch.down
    return clone


def _split_prefixes(branches, removed_short, removed_long, protected,
                    mouths):
    """Режет ветви по удалённым сверху участкам."""
    result = []
    ranks = []
    for position, branch in enumerate(branches):
        total = branch.length
        if total <= 1e-9:
            result.append(branch)
            ranks.append(MAX_RANK)
            continue
        first = removed_short[position]
        second = max(first, removed_long[position])
        if position in protected:
            first = second = 0.0
        if position in mouths:
            keep = min(50.0, total)
            first = min(first, total - keep)
            second = min(second, total - keep)
        boundaries = sorted({
            0.0, total, min(first, total), min(second, total),
        })
        cumulative = _cumulative(branch.points)
        for start, end in zip(boundaries, boundaries[1:]):
            if end - start <= 1e-9:
                continue
            points = _slice(branch.points, cumulative, start, end)
            if start < first - 1e-9:
                rank = 0
            elif start < second - 1e-9:
                rank = 1
            else:
                rank = MAX_RANK
            result.append(_clone_branch(branch, points, end - start))
            ranks.append(rank)
    return result, ranks


def _upstream_values(branches, followers, groups, totals):
    """Длина самого длинного пути от истока до низа ветви.

    Длины ветвей вдоль пути суммируются, поэтому разбиение OSM на
    короткие сегменты не меняет результат.  Циклы схлопываются в сильно
    связные компоненты: путь по циклу можно продолжать сколько угодно,
    поэтому компонента даёт сумму своих ветвей и ограничена общей длиной
    связной компоненты.
    """
    count = len(branches)
    successors = [
        [item for item in followers.get(branch.down, ()) if item != position]
        for position, branch in enumerate(branches)
    ]
    # Итеративный Косараджу: рекурсия не выдержит длинных цепочек.
    order = []
    visited = bytearray(count)
    for start in range(count):
        if visited[start]:
            continue
        visited[start] = 1
        stack = [(start, 0)]
        while stack:
            position, index = stack[-1]
            edges = successors[position]
            if index < len(edges):
                stack[-1] = (position, index + 1)
                follower = edges[index]
                if not visited[follower]:
                    visited[follower] = 1
                    stack.append((follower, 0))
            else:
                order.append(position)
                stack.pop()
    reverse = [[] for _ in range(count)]
    for position, edges in enumerate(successors):
        for follower in edges:
            reverse[follower].append(position)
    component_of = [-1] * count
    components = []
    for start in reversed(order):
        if component_of[start] >= 0:
            continue
        identifier = len(components)
        members = []
        component_of[start] = identifier
        stack = [start]
        while stack:
            position = stack.pop()
            members.append(position)
            for predecessor in reverse[position]:
                if component_of[predecessor] < 0:
                    component_of[predecessor] = identifier
                    stack.append(predecessor)
        components.append(members)
    group_of = {}
    for root, positions in groups.items():
        for position in positions:
            group_of[position] = root
    values = [0.0] * count
    for identifier, members in enumerate(components):
        upstream = 0.0
        for position in members:
            for predecessor in reverse[position]:
                if component_of[predecessor] != identifier:
                    upstream = max(upstream, values[predecessor])
        # У одиночной вершины это её собственная длина, у цикла —
        # сумма всех ветвей компоненты.
        value = upstream + sum(
            branches[position].length for position in members
        )
        for position in members:
            values[position] = min(value, totals[group_of[position]])
    return values


def _downstream_values(branches, followers, cancel=None):
    """Длина главного пути вниз; для циклов обрывается на повторе."""
    count = len(branches)
    state = [0] * count
    values = [0.0] * count
    for start in range(count):
        if state[start] == 2:
            continue
        stack = [(start, 0)]
        while stack:
            if cancel is not None and len(stack) % 2000 == 0 and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            position, phase = stack.pop()
            if phase == 0:
                if state[position] == 2 or state[position] == 1:
                    continue
                state[position] = 1
                stack.append((position, 1))
                for follower in followers.get(branches[position].down, ()):
                    if follower == position:
                        continue
                    if state[follower] == 0:
                        stack.append((follower, 0))
            else:
                best = 0.0
                for follower in followers.get(branches[position].down, ()):
                    if follower == position:
                        continue
                    if state[follower] == 2:
                        best = max(best, values[follower])
                values[position] = branches[position].length + best
                state[position] = 2
    return values


def _transform_points(transform, points):
    if transform is None:
        return points
    transformed = transform.TransformPoints(
        [(point[0], point[1]) for point in points]
    )
    return [(item[0], item[1]) for item in transformed]


def _metric_transform(srs, pieces):
    """Локальная метрическая СК для географических слоёв."""
    if srs is None or not srs.IsGeographic():
        return None, None
    from osgeo import osr

    xs = [point[0] for piece in pieces for point in piece.points]
    ys = [point[1] for piece in pieces for point in piece.points]
    if not xs or not ys:
        return None, None
    centre_lon = (min(xs) + max(xs)) / 2.0
    centre_lat = (min(ys) + max(ys)) / 2.0
    metric = osr.SpatialReference()
    metric.ImportFromProj4(
        f"+proj=aeqd +lat_0={centre_lat:.10f} "
        f"+lon_0={centre_lon:.10f} +datum=WGS84 +units=m +no_defs"
    )
    metric.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    source = srs.Clone()
    source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    forward = osr.CoordinateTransformation(source, metric)
    inverse = osr.CoordinateTransformation(metric, source)
    return forward, inverse


def _read_points(path, transform=None):
    from osgeo import ogr

    source = ogr.Open(str(path))
    if source is None:
        return None
    layer = source.GetLayer(0)
    points = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        if transform is not None:
            geometry = geometry.Clone()
            geometry.Transform(transform)
        points.extend(_geometry_points(geometry))
    layer = None
    source = None
    return points


def _read_polygons(path, skip_sub_sea=False, transform=None):
    from osgeo import ogr

    source = ogr.Open(str(path))
    if source is None:
        return None
    layer = source.GetLayer(0)
    definition = layer.GetLayerDefn()
    sub_sea = (
        definition.GetFieldIndex("sub_sea") if skip_sub_sea else -1
    )
    polygons = []
    for feature in layer:
        if sub_sea >= 0 and _text(feature.GetField(sub_sea)):
            continue
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        clone = geometry.Clone()
        if clone.GetGeometryName() not in (
            "POLYGON", "MULTIPOLYGON", "GEOMETRYCOLLECTION",
        ):
            continue
        if transform is not None:
            try:
                clone.Transform(transform)
            except Exception:
                continue
        polygons.append(clone)
    layer = None
    source = None
    return polygons


def _read_coastline(directory, transform=None):
    """Читает береговую линию в метрической СК."""
    from osgeo import ogr

    path = directory / f"{COASTLINE_LAYER}.sqlite"
    if not path.exists():
        return []
    source = ogr.Open(str(path))
    if source is None:
        return []
    layer = source.GetLayer(0)
    lines = []
    for feature in layer:
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        clone = geometry.Clone()
        if clone.GetGeometryName() not in (
            "LINESTRING", "MULTILINESTRING", "GEOMETRYCOLLECTION",
        ):
            continue
        if transform is not None:
            try:
                clone.Transform(transform)
            except Exception:
                continue
        lines.append(clone)
    layer = None
    source = None
    return lines


def _water_path(directory):
    from osgeo import ogr

    for name in WATER_LAYERS:
        path = directory / f"{name}.sqlite"
        if not path.exists() or path.stat().st_size <= 0:
            continue
        source = ogr.Open(str(path))
        if source is None:
            continue
        layer = source.GetLayer(0)
        count = layer.GetFeatureCount() if layer is not None else 0
        layer = None
        source = None
        if count != 0:
            return path
    return None


def _protect_springs(points, branches, down_values, cancel=None):
    from qgis.core import QgsRectangle

    if not points:
        return set()
    index = _build_bbox_index(_branch_boxes(branches))
    starts = set()
    for position, point in enumerate(points):
        if cancel is not None and position % 500 == 0 and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        rectangle = QgsRectangle(
            point[0] - SPRING_RADIUS, point[1] - SPRING_RADIUS,
            point[0] + SPRING_RADIUS, point[1] + SPRING_RADIUS,
        )
        best = None
        best_score = None
        for branch_position in index.intersects(rectangle):
            branch = branches[branch_position]
            distance, _along = _point_to_polyline(branch.points, point)
            if distance > SPRING_RADIUS:
                continue
            score = (down_values[branch_position], -distance)
            if best_score is None or score > best_score:
                best_score = score
                best = branch_position
        if best is not None:
            starts.add(best)
    return starts


def _line_geometry(points):
    from osgeo import ogr

    line = ogr.Geometry(ogr.wkbLineString)
    for x, y in points:
        line.AddPoint_2D(x, y)
    return line


def _point_geometry(point):
    from osgeo import ogr

    geometry = ogr.Geometry(ogr.wkbPoint)
    geometry.AddPoint_2D(point[0], point[1])
    return geometry


def _protect_polygons(polygons, branches, down_values, cancel=None):
    from qgis.core import QgsRectangle

    if not polygons:
        return set()
    index = _build_bbox_index(_polygon_boxes(polygons))
    prepared = []
    for polygon in polygons:
        create = getattr(polygon, "CreatePreparedGeometry", None)
        try:
            prepared.append(create() if create is not None else None)
        except Exception:
            prepared.append(None)
    best = {}
    for position, branch in enumerate(branches):
        if cancel is not None and position % 500 == 0 and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        xs = [point[0] for point in branch.points]
        ys = [point[1] for point in branch.points]
        rectangle = QgsRectangle(
            min(xs), min(ys), max(xs), max(ys)
        )
        candidates = index.intersects(rectangle)
        if not candidates:
            continue
        line = _line_geometry(branch.points)
        point = _point_geometry(branch.points[-1])
        for polygon_position in candidates:
            polygon = polygons[polygon_position]
            shape = prepared[polygon_position]
            try:
                if shape is not None:
                    intersects = shape.Intersects(line)
                else:
                    intersects = polygon.Intersects(line)
                if not intersects:
                    continue
                if polygon.Distance(point) <= OUTLET_TOLERANCE:
                    continue
            except Exception:
                continue
            current = best.get(polygon_position)
            if current is None or (
                down_values[position], -position
            ) > (
                down_values[current], -current
            ):
                best[polygon_position] = position
    return set(best.values())


def _coastline_outlets(lines, branches, cancel=None):
    """Находит ветви, нижний конец которых выходит на берег."""
    from qgis.core import QgsRectangle

    if not lines:
        return set()
    boxes = []
    for line in lines:
        min_x, max_x, min_y, max_y = line.GetEnvelope()
        boxes.append((min_x, min_y, max_x, max_y))
    index = _build_bbox_index(boxes)
    outlets = set()
    for position, branch in enumerate(branches):
        if cancel is not None and position % 500 == 0 and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        x, y = branch.points[-1]
        rectangle = QgsRectangle(
            x - OUTLET_TOLERANCE, y - OUTLET_TOLERANCE,
            x + OUTLET_TOLERANCE, y + OUTLET_TOLERANCE,
        )
        candidates = index.intersects(rectangle)
        if not candidates:
            continue
        point = None
        for line_position in candidates:
            if point is None:
                point = _point_geometry((x, y))
            try:
                if lines[line_position].Distance(point) <= OUTLET_TOLERANCE:
                    outlets.add(position)
                    break
            except Exception:
                continue
    return outlets


def _apply_outlet_protection(outlets, branches, feeders, values, ranks):
    """Оставляет видимым путь от устья вверх до крупной ветви."""
    result = list(ranks)
    for start in outlets:
        position = start
        seen = set()
        while position is not None and position not in seen:
            seen.add(position)
            if result[position] >= MAX_RANK:
                break
            result[position] = MAX_RANK
            candidates = [
                item for item in feeders.get(branches[position].up, ())
                if item != position
            ]
            if not candidates:
                break
            position = max(
                candidates, key=lambda item: (values[item], -item),
            )
    return result


def _apply_protection(starts, branches, followers, down_values, ranks):
    result = list(ranks)
    protected = set()
    for start in starts:
        position = start
        seen = set()
        while position is not None and position not in seen:
            seen.add(position)
            if ranks[position] >= MAX_RANK:
                break
            result[position] = MAX_RANK
            protected.add(position)
            candidates = [
                follower for follower in followers.get(
                    branches[position].down, ()
                )
                if follower != position
            ]
            if not candidates:
                break
            position = max(
                candidates,
                key=lambda item: (down_values[item], -item),
            )
    return result, protected


def _default_rank(value):
    return sum(1 for threshold in THRESHOLDS if value + 1e-9 >= threshold)


def _fast_pragmas(dataset):
    for pragma in (
        "PRAGMA journal_mode=MEMORY",
        "PRAGMA synchronous=OFF",
        "PRAGMA cache_size=-262144",
    ):
        try:
            result = dataset.ExecuteSQL(pragma)
        except Exception:
            continue
        if result is not None:
            dataset.ReleaseResultSet(result)


def _write_layer(path, fields, srs, branches, ranks, output_transform=None,
                 cancel=None):
    from osgeo import ogr

    temporary = path.with_name(f"{path.stem}.dry{path.suffix}")
    temporary.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("SQLite")
    dataset = driver.CreateDataSource(
        str(temporary), options=["SPATIALITE=YES"]
    )
    if dataset is None:
        raise OSError(f"Cannot create {temporary}")
    layer = dataset.CreateLayer(
        path.stem, srs, ogr.wkbMultiLineString,
        options=["SPATIAL_INDEX=YES"],
    )
    if layer is None:
        dataset = None
        temporary.unlink(missing_ok=True)
        raise OSError(f"Cannot create layer in {temporary}")
    for definition in fields:
        field = ogr.FieldDefn(
            definition.GetName(), definition.GetType()
        )
        field.SetWidth(definition.GetWidth())
        field.SetPrecision(definition.GetPrecision())
        layer.CreateField(field)
    layer.CreateField(ogr.FieldDefn(FIELD, ogr.OFTInteger))
    rank_index = len(fields)
    _fast_pragmas(dataset)
    written = 0
    try:
        layer.StartTransaction()
        definition = layer.GetLayerDefn()
        for position, branch in enumerate(branches):
            if cancel is not None and written % 500 == 0 and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            if len(branch.points) < 2:
                continue
            feature = ogr.Feature(definition)
            for index, value in enumerate(branch.attrs):
                if value is not None:
                    try:
                        feature.SetField(index, value)
                    except Exception:
                        pass
            feature.SetField(rank_index, int(ranks[position]))
            line = _line_geometry(branch.points)
            if output_transform is not None:
                line.Transform(output_transform)
            multi = ogr.Geometry(ogr.wkbMultiLineString)
            multi.AddGeometry(line)
            feature.SetGeometry(multi)
            layer.CreateFeature(feature)
            written += 1
        layer.CommitTransaction()
    except Exception:
        layer.RollbackTransaction()
        layer = None
        dataset = None
        temporary.unlink(missing_ok=True)
        raise
    layer = None
    dataset = None
    os.replace(temporary, path)
    return written


def _set_arrow_ranks(path, ranks_by_id, default=0, cancel=None):
    """Пишет ``dry_rank`` в слой стрелок по OSM ``@id``.

    Стрелки строятся из отдельного сглаженного слоя.  Если ОСМ-путь
    разрезан на ветви с разными рангами, берётся худший ранг: стрелка
    не должна висеть над участком, который уже скрыт.
    """
    if not path.exists():
        return 0
    table = path.stem
    connection = sqlite3.connect(str(path))
    try:
        columns = {
            row[1].lower(): row[1]
            for row in connection.execute(
                f'PRAGMA table_info("{table}")'
            )
        }
        id_field = columns.get("@id")
        if id_field is None:
            return 0
        rank_field = columns.get(FIELD)
        if rank_field is None:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{FIELD}" INTEGER'
            )
            rank_field = FIELD
        rows = connection.execute(
            f'SELECT rowid, "{id_field}" FROM "{table}"'
        ).fetchall()
        updates = []
        for position, (row_id, value) in enumerate(rows):
            if cancel is not None and position % 500 == 0 and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            updates.append((int(ranks_by_id.get(_text(value), default)),
                            row_id))
        connection.executemany(
            f'UPDATE "{table}" SET "{rank_field}" = ? WHERE rowid = ?',
            updates,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(rows)


def _source_starts(directory, branches, followers, transform=None,
                   cancel=None):
    """Ветви у родников, водоёмов и болот."""
    springs = []
    spring_path = directory / "spring.sqlite"
    if spring_path.exists():
        springs = _read_points(spring_path, transform=transform) or []
    water = []
    water_path = _water_path(directory)
    if water_path is not None:
        water = _read_polygons(water_path, transform=transform) or []
    wetland = []
    wetland_path = directory / "wetland.sqlite"
    if wetland_path.exists():
        wetland = _read_polygons(
            wetland_path, skip_sub_sea=True, transform=transform,
        ) or []
    if not springs and not water and not wetland:
        return set(), None
    down_values = _downstream_values(
        branches, followers, cancel=cancel
    )
    starts = set()
    if springs:
        starts.update(_protect_springs(
            springs, branches, down_values, cancel=cancel,
        ))
    if water:
        starts.update(_protect_polygons(
            water, branches, down_values, cancel=cancel,
        ))
    if wetland:
        starts.update(_protect_polygons(
            wetland, branches, down_values, cancel=cancel,
        ))
    return starts, down_values


def apply(directory, cancel=None, progress=None):
    """Считает ``dry_rank`` и перезаписывает слои ручьёв.

    Возвращает статистику.  Ошибки чтения источников не отменяют
    подсыхание: защита у источников в этом случае просто пропускается.
    """
    directory = Path(directory)
    layer_pieces = {}
    fields = {}
    srs = {}
    pieces = []
    for name in LAYERS:
        path = directory / f"{name}.sqlite"
        if not path.exists():
            continue
        layer_fields, layer_srs, input_pieces = _read_input(path)
        fields[name] = layer_fields
        srs[name] = layer_srs
        layer_pieces[name] = input_pieces
        pieces.extend(input_pieces)
    if not pieces:
        return {"branches": 0, "protected": 0}

    source_srs = next(
        (srs[name] for name in LAYERS if name in srs), None
    )
    forward, inverse = _metric_transform(source_srs, pieces)
    if forward is not None:
        for piece in pieces:
            piece.points = _transform_points(forward, piece.points)
            piece.length = _length(piece.points)

    pieces = _split_t_junctions(pieces, cancel=cancel)
    incident = _node_pieces(pieces)
    branches = _dissolve(pieces, incident)
    followers = defaultdict(list)
    for position, branch in enumerate(branches):
        followers[branch.up].append(position)
    mouths = _attach_mouth_fragments(
        branches, followers, cancel=cancel,
    )
    feeders = defaultdict(list)
    for position, branch in enumerate(branches):
        feeders[branch.down].append(position)
    groups, totals = _components(branches)
    values = _upstream_values(branches, followers, groups, totals)
    ranks = [_default_rank(value) for value in values]

    protected = 0
    protected_positions = set()
    warning = None
    try:
        starts, down_values = _source_starts(
            directory, branches, followers,
            transform=forward, cancel=cancel,
        )
        if starts and down_values is not None:
            ranks, protected_positions = _apply_protection(
                starts, branches, followers, down_values, ranks
            )
            protected = len(starts)
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - карта не должна страдать
        warning = str(exc)

    outlets = 0
    mouth_branches = set()
    try:
        coastline = _read_coastline(directory, transform=forward)
        mouth_branches = _coastline_outlets(
            coastline, branches, cancel=cancel,
        )
        outlets = len(mouth_branches)
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - карта не должна страдать
        warning = (
            f"{warning}; берег: {exc}" if warning else f"берег: {exc}"
        )

    removed_short = _removal_prefixes(
        branches, feeders, followers, THRESHOLDS[0],
        protected_positions, cancel=cancel,
    )
    removed_long = _removal_prefixes(
        branches, feeders, followers, THRESHOLDS[1],
        protected_positions, cancel=cancel,
    )
    branches, ranks = _split_prefixes(
        branches, removed_short, removed_long,
        protected_positions, mouth_branches,
    )

    rank_by_branch = {
        id(branch): ranks[position]
        for position, branch in enumerate(branches)
    }
    arrow_ranks = {}
    if "stream" in fields:
        id_index = next(
            (
                index for index, definition in enumerate(fields["stream"])
                if definition.GetName().lower() == "@id"
            ),
            None,
        )
        if id_index is not None:
            for branch, rank in zip(branches, ranks):
                if branch.group != "stream":
                    continue
                key = _text(branch.attrs[id_index])
                if not key:
                    continue
                current = arrow_ranks.get(key)
                arrow_ranks[key] = (
                    rank if current is None else min(current, rank)
                )
    counts = {}
    for name in LAYERS:
        input_pieces = layer_pieces.get(name)
        if input_pieces is None:
            continue
        group = "intermittent" if "intermittent" in name else "stream"
        layer_branches = [
            branch for branch in branches if branch.group == group
        ]
        # Ветви уже содержат атрибуты и геометрию, но не привязаны к
        # исходному слою.  Пишем ветви, сохраняя атрибуты первого куска.
        layer_ranks = [
            rank_by_branch[id(branch)] for branch in layer_branches
        ]
        path = directory / f"{name}.sqlite"
        _write_layer(
            path, fields[name], srs[name],
            layer_branches, layer_ranks,
            output_transform=inverse, cancel=cancel,
        )
        counts[name] = len(layer_branches)
    arrows = 0
    try:
        arrows = _set_arrow_ranks(
            directory / f"{ARROW_LAYER}.sqlite", arrow_ranks,
            default=0, cancel=cancel,
        )
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001 - стрелки не должны ронять карту
        warning = (
            f"{warning}; стрелки: {exc}" if warning else f"стрелки: {exc}"
        )
    stats = {
        "branches": len(branches),
        "protected": protected,
        "mouths": mouths,
        "outlets": outlets,
        "arrows": arrows,
        "layers": counts,
    }
    if warning:
        stats["warning"] = warning
    return stats


def ensure_visible(directory, names=None):
    """Аварийный режим: ставит всем объектам максимальный ``dry_rank``.

    Нужен, если основной расчёт упал: фильтры шаблона не должны скрыть
    все ручьи.
    """
    directory = Path(directory)
    names = names or LAYERS
    for name in names:
        path = directory / f"{name}.sqlite"
        if not path.exists():
            continue
        fields, srs, pieces = _read_input(path)
        branches = []
        for piece in pieces:
            branch = _Branch()
            branch.group = piece.group
            branch.waterway = piece.waterway
            branch.intermittent = piece.intermittent
            branch.tunnel = piece.tunnel
            branch.layer = piece.layer
            branch.sig = piece.sig
            branch.attrs = piece.attrs
            branch.points = piece.points
            branch.length = piece.length
            branch.up = piece.up
            branch.down = piece.down
            branches.append(branch)
        _write_layer(
            path, fields, srs, branches, [MAX_RANK] * len(branches)
        )
    _set_arrow_ranks(
        directory / f"{ARROW_LAYER}.sqlite", {}, default=MAX_RANK
    )
