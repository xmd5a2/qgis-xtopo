"""Упаковка дорожных сегментов для ускорения рендера.

Именованные дороги сливаются в цепочки только по общим концам.  Для
односторонних дорог слияние направленное: конец одного сегмента должен
совпасть с началом следующего.  Поэтому ориентация линии не меняется и
стрелки ``oneway`` остаются правильными.  Безымянные дороги пакуются в
``MultiLineString`` по тайлам без слияния геометрий: каждая исходная
часть сохраняет своё направление.

Обрабатываются ``highway_main.sqlite``, ``path.sqlite`` и ``track.sqlite``.
Разбиения ``highway_main`` по ``layer``, мосты и туннели остаются
отдельными слоями и не пакуются.  У ``path``/``track`` из ключа
исключены ``highway_integrity`` и ``oneway``: стрелок там нет, а
интегритет слишком дробит группы.
"""

import math
import os
from pathlib import Path

PACK_LAYERS = ("highway_main", "path", "track")

# Ключ упаковки.  У path/track нет стрелок oneway, а highway_integrity
# намеренно не входит в ключ: иначе мелкие различия дробят группы.
KEY_FIELDS = {
    "highway_main": (
        "highway", "highway_integrity", "service", "embankment", "layer",
        "oneway",
    ),
    "path": ("highway", "service", "embankment", "layer"),
    "track": ("highway", "service", "embankment", "layer"),
}

# Тайл 0.05° примерно 5.5 км на широте Гуаякиля.  Для проецированных
# данных берём 5 км в единицах слоя.
TILE_SIZE = 0.05
PROJECTED_TILE_SIZE = 5000.0

NAME_EXTRA_FIELDS = (
    "alt_name", "int_name", "loc_name", "official_name", "old_name",
    "short_name",
)

def pack(directory, name, cancel=None):
    """Упаковывает дороги слоя. Возвращает число записанных объектов."""
    if name not in PACK_LAYERS:
        return 0
    path = Path(directory) / f"{name}.sqlite"
    if not path.exists():
        return 0
    return _pack_file(path, KEY_FIELDS[name], cancel=cancel)


def _feature_text(feature, index):
    if index is None:
        return ""
    return feature.GetFieldAsString(index).strip()


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _special_name_ids(
    source, layer, field_names, name_index, ref_index, other_indexes
):
    """FID объектов без name/ref, но с другим вариантом имени.

    Таких объектов единицы.  Их список позволяет не читать все поля
    названий у десятков тысяч безымянных дорог.
    """
    if not other_indexes:
        return set()
    empty = []
    for index in (name_index, ref_index):
        if index is None:
            empty.append("1=1")
        else:
            field = _quote(field_names[index])
            empty.append(f"({field} IS NULL OR {field} = '')")
    present = " OR ".join(
        f"({_quote(field_names[index])} IS NOT NULL "
        f"AND {_quote(field_names[index])} <> '')"
        for index in other_indexes
    )
    where = " AND ".join(empty) + f" AND ({present})"
    fid_column = layer.GetFIDColumn() or "rowid"
    sql = (
        f"SELECT {_quote(fid_column)} AS \"special_id\" "
        f"FROM {_quote(layer.GetName())} WHERE {where}"
    )
    try:
        result = source.ExecuteSQL(sql)
    except Exception:  # noqa: BLE001 - слой не должен пропасть из-за запроса
        return None
    if result is None:
        return None
    try:
        return {feature.GetField(0) for feature in result}
    finally:
        source.ReleaseResultSet(result)


def _attribute_values(feature, field_types):
    """Поля первого объекта группы с сохранением типов и NULL."""
    from osgeo import ogr

    values = []
    for index, kind in enumerate(field_types):
        if kind == ogr.OFTInteger:
            value = feature.GetFieldAsInteger(index)
            if value == 0 and (
                not feature.IsFieldSet(index) or feature.IsFieldNull(index)
            ):
                value = None
        elif kind == ogr.OFTInteger64:
            value = feature.GetFieldAsInteger64(index)
            if value == 0 and (
                not feature.IsFieldSet(index) or feature.IsFieldNull(index)
            ):
                value = None
        elif kind == ogr.OFTReal:
            value = feature.GetFieldAsDouble(index)
            if value == 0 and (
                not feature.IsFieldSet(index) or feature.IsFieldNull(index)
            ):
                value = None
        elif kind in (ogr.OFTString, ogr.OFTWideString):
            value = feature.GetFieldAsString(index)
            if value == "" and feature.IsFieldNull(index):
                value = None
        else:
            value = feature.GetField(index)
        values.append(value)
    return tuple(values)


def _name_fields(field_names):
    """Поля названий: все name* и варианты имени из набора подписей."""
    lowered = {name.lower() for name in field_names}
    result = [
        name for name in field_names if name.lower().startswith("name")
    ]
    for name in NAME_EXTRA_FIELDS:
        if name in lowered and name not in result:
            result.append(name)
    return result


def _line_parts(geometry):
    """Координаты линейных частей геометрии."""
    if geometry is None or geometry.IsEmpty():
        return []
    kind = geometry.GetGeometryName()
    if kind == "LINESTRING":
        points = geometry.GetPoints()
        return [points] if len(points) >= 2 else []
    if kind in ("MULTILINESTRING", "GEOMETRYCOLLECTION"):
        parts = []
        for index in range(geometry.GetGeometryCount()):
            parts.extend(_line_parts(geometry.GetGeometryRef(index)))
        return parts
    return []


def _point_key(point):
    return (round(point[0], 9), round(point[1], 9))


def _chain_points(parts):
    """Склеивает части цепочки, убирая повтор точки на стыке."""
    points = []
    for part in parts:
        if points and _point_key(points[-1]) == _point_key(part[0]):
            points.extend(part[1:])
        else:
            points.extend(part)
    return points


def _reverse_if(part, reverse):
    return list(reversed(part)) if reverse else part


def _merge_directed(parts):
    """Сливает односторонние части только по схеме конец → начало."""
    if len(parts) < 2:
        return [[part] for part in parts]
    starts = {}
    ends = {}
    for index, part in enumerate(parts):
        starts.setdefault(_point_key(part[0]), []).append(index)
        ends.setdefault(_point_key(part[-1]), []).append(index)

    used = set()
    chains = []

    def can_follow(index):
        end = _point_key(parts[index][-1])
        incoming = ends.get(end, [])
        outgoing = starts.get(end, [])
        if len(incoming) != 1 or len(outgoing) != 1:
            return None
        candidate = outgoing[0]
        if candidate == index or candidate in used:
            return None
        # Не склеивать сегмент, который сразу разворачивает линию назад:
        # это дубль дороги в обратную сторону, и стрелка станет неверной.
        if _point_key(parts[candidate][-1]) == _point_key(parts[index][0]):
            return None
        return candidate

    # Сначала цепочки со свободным началом, потом замкнутые кольца.
    starts_order = sorted(
        range(len(parts)),
        key=lambda index: _point_key(parts[index][0]) not in ends,
        reverse=True,
    )
    for first in starts_order:
        if first in used:
            continue
        used.add(first)
        chain = [parts[first]]
        current = first
        while True:
            following = can_follow(current)
            if following is None:
                break
            used.add(following)
            chain.append(parts[following])
            current = following
        chains.append(chain)
    return chains


def _merge_undirected(parts):
    """Сливает двухсторонние части через узлы степени 2."""
    if len(parts) < 2:
        return [[part] for part in parts]
    incidents = {}
    for index, part in enumerate(parts):
        incidents.setdefault(_point_key(part[0]), []).append((index, 0))
        incidents.setdefault(_point_key(part[-1]), []).append((index, 1))

    links = {}
    for entries in incidents.values():
        if len(entries) != 2:
            continue
        first, second = entries
        if first[0] == second[0]:
            continue
        links[first] = second
        links[second] = first

    used = set()
    chains = []
    for first in range(len(parts)):
        if first in used:
            continue
        used.add(first)
        chain = [(first, False)]

        def extend(at_end):
            part_index, reverse = chain[-1] if at_end else chain[0]
            if at_end:
                endpoint = 0 if reverse else 1
            else:
                endpoint = 1 if reverse else 0
            other = links.get((part_index, endpoint))
            if other is None:
                return False
            other_index, other_endpoint = other
            if other_index in used:
                return False
            used.add(other_index)
            # Общая точка должна оказаться навстречу текущему концу.
            if at_end:
                other_reverse = other_endpoint == 1
                chain.append((other_index, other_reverse))
            else:
                other_reverse = other_endpoint == 0
                chain.insert(0, (other_index, other_reverse))
            return True

        while extend(True):
            pass
        while extend(False):
            pass
        chains.append([
            _reverse_if(parts[index], reverse)
            for index, reverse in chain
        ])
    return chains


def _multi_geometry(parts):
    from osgeo import ogr

    multi = ogr.Geometry(ogr.wkbMultiLineString)
    added = 0
    for points in parts:
        if len(points) < 2:
            continue
        line = ogr.Geometry(ogr.wkbLineString)
        for point in points:
            line.AddPoint_2D(point[0], point[1])
        multi.AddGeometry(line)
        added += 1
    return multi if added else None


def _pack_file(path, key_fields, cancel=None):
    from osgeo import ogr

    source = ogr.Open(str(path))
    if source is None:
        raise OSError(f"Cannot open {path}")
    layer = source.GetLayer(0)
    definition = layer.GetLayerDefn()
    field_definitions = []
    for index in range(definition.GetFieldCount()):
        source_field = definition.GetFieldDefn(index)
        field = ogr.FieldDefn(source_field.GetName(), source_field.GetType())
        field.SetWidth(source_field.GetWidth())
        field.SetPrecision(source_field.GetPrecision())
        field_definitions.append(field)
    spatial_ref = layer.GetSpatialRef()
    if spatial_ref is not None:
        spatial_ref = spatial_ref.Clone()
    field_names = [field.GetName() for field in field_definitions]
    index_of = {
        name.lower(): index for index, name in enumerate(field_names)
    }
    name_indexes = [
        index_of[name.lower()] for name in _name_fields(field_names)
        if name.lower() in index_of
    ]
    ref_index = index_of.get("ref")
    name_index = index_of.get("name")
    other_name_indexes = [
        index for index in name_indexes
        if index not in (name_index, ref_index)
    ]
    special_names = _special_name_ids(
        source, layer, field_names, name_index, ref_index,
        other_name_indexes,
    )
    check_all_names = special_names is None
    tile_size = TILE_SIZE
    if spatial_ref is not None and not spatial_ref.IsGeographic():
        tile_size = PROJECTED_TILE_SIZE
    field_types = [field.GetType() for field in field_definitions]
    key_indexes = [index_of.get(name) for name in key_fields]
    oneway_position = (
        key_fields.index("oneway") if "oneway" in key_fields else None
    )

    groups = {}
    processed = 0
    try:
        for feature in layer:
            if cancel is not None and processed % 1000 == 0 and cancel.is_set():
                raise InterruptedError("Preparation cancelled")
            processed += 1
            geometry = feature.GetGeometryRef()
            parts = _line_parts(geometry)
            if not parts:
                continue

            key_values = tuple(
                feature.GetFieldAsString(index).strip()
                if index is not None else ""
                for index in key_indexes
            )
            name_value = _feature_text(feature, name_index)
            ref = _feature_text(feature, ref_index)
            if (check_all_names or name_value or ref
                    or (special_names and feature.GetFID() in special_names)):
                names = tuple(
                    feature.GetFieldAsString(index).strip()
                    for index in name_indexes
                )
            else:
                names = ()
            named = bool(ref) or any(names)
            oneway_value = (
                key_values[oneway_position]
                if oneway_position is not None else ""
            )
            # oneway входит в ключ только у highway_main.  У именованных
            # односторонних дорог направление ещё и проверяется при
            # слиянии (конец → начало), а безымянные части не сливаются
            # и сохраняют исходную ориентацию.
            if named:
                key = ("named",) + key_values + (names, ref)
            else:
                first = parts[0][0]
                tile = (
                    math.floor(first[0] / tile_size),
                    math.floor(first[1] / tile_size),
                )
                key = ("unnamed",) + key_values + (tile,)
            group = groups.get(key)
            if group is None:
                group = {
                    "attrs": _attribute_values(feature, field_types),
                    "parts": [],
                    "named": named,
                    "directional": oneway_value in ("yes", "-1"),
                }
                groups[key] = group
            group["parts"].extend(parts)
    finally:
        feature = None
        geometry = None
        layer = None
        definition = None
        source = None

    written = _write(
        path, field_definitions, spatial_ref, groups, cancel=cancel
    )
    return written


def _write(path, field_definitions, spatial_ref, groups, cancel=None):
    from osgeo import ogr

    path = Path(path)
    temporary = path.with_name(f"{path.stem}.packed{path.suffix}")
    temporary.unlink(missing_ok=True)
    driver = ogr.GetDriverByName("SQLite")
    dataset = driver.CreateDataSource(
        str(temporary), options=["SPATIALITE=YES"]
    )
    if dataset is None:
        raise OSError(f"Cannot create {temporary}")
    layer = dataset.CreateLayer(
        path.stem, spatial_ref, ogr.wkbMultiLineString,
        options=["SPATIAL_INDEX=YES"],
    )
    if layer is None:
        dataset = None
        temporary.unlink(missing_ok=True)
        raise OSError(f"Cannot create layer in {temporary}")
    for field in field_definitions:
        layer.CreateField(field)

    written = 0
    try:
        layer.StartTransaction()
        out_definition = layer.GetLayerDefn()
        for group in groups.values():
            if group["named"]:
                if group["directional"]:
                    chains = _merge_directed(group["parts"])
                else:
                    chains = _merge_undirected(group["parts"])
                geometries = [_multi_geometry([_chain_points(chain)])
                              for chain in chains]
            else:
                # Безымянные части не склеиваются: каждая сохраняет
                # собственное направление.
                geometries = [_multi_geometry(group["parts"])]
            for geometry in geometries:
                if geometry is None:
                    continue
                feature = ogr.Feature(out_definition)
                for index, value in enumerate(group["attrs"]):
                    if value is not None:
                        feature.SetField(index, value)
                feature.SetGeometry(geometry)
                layer.CreateFeature(feature)
                written += 1
                if (cancel is not None and written % 200 == 0
                        and cancel.is_set()):
                    raise InterruptedError("Preparation cancelled")
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
