import bz2
import gzip
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


LAYER_NAMES = ("points", "lines", "multipolygons", "multilinestrings", "other_relations")

# Запас вокруг bbox при грубом фильтре GDAL (в градусах и доля от размера
# области).  Фильтр отсекает большую часть файла ещё до построения
# геометрий: на больших PBF это в разы быстрее полного перебора.
FILTER_MARGIN_DEGREES = 0.05
FILTER_MARGIN_RATIO = 0.1

INTERNAL_FIELDS = {
    "osm_id", "osm_way_id", "z_order", "land_area", "all_tags", "other_tags",
}
HSTORE_PATTERN = re.compile(r'"((?:\\.|[^"\\])*)"\s*=>\s*"((?:\\.|[^"\\])*)"')


class PbfError(RuntimeError):
    pass


def _gdal():
    from osgeo import gdal, ogr

    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr


def _unescape(value):
    return (
        value.replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


def parse_tags(value):
    if not value:
        return {}
    text = str(value).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return {str(key): str(item) for key, item in data.items()}
        except (ValueError, TypeError):
            return {}
    result = {}
    for key, item in HSTORE_PATTERN.findall(text):
        result[_unescape(key)] = _unescape(item)
    return result


@dataclass
class QueryFilter:
    kind: str
    conditions: list
    special: str = ""


def parse_query(query_text):
    filters = []
    pattern = re.compile(r"(node|way|relation)\s*((?:\[[^\]]*\]\s*)+)")
    for match in pattern.finditer(query_text):
        kind = match.group(1)
        conditions = []
        for raw in re.findall(r"\[([^\]]*)\]", match.group(2)):
            raw = raw.strip()
            condition = re.match(
                r'^"([^"]+)"\s*(?:(!?=)\s*"([^"]*)"|(!?~)\s*"([^"]*)")?$',
                raw,
            )
            if condition:
                key, operator, value, regex_operator, regex = condition.groups()
                conditions.append((key, operator, value, regex_operator, regex))
            else:
                conditions.append((raw.strip('"'), None, None, None, None))
        filters.append(QueryFilter(kind, conditions))
    if re.search(r"node\s*\(\s*r\.relations", query_text):
        filters.append(QueryFilter("node", [], special="relation_member"))
    return filters


def _condition_matches(properties, condition):
    key, operator, value, regex_operator, regex = condition
    if key not in properties:
        return operator == "!=" or regex_operator == "!~"
    actual = str(properties[key])
    if operator == "=":
        return actual == value
    if operator == "!=":
        return actual != value
    if regex_operator == "~":
        return re.search(regex or "", actual) is not None
    if regex_operator == "!~":
        return re.search(regex or "", actual) is None
    return True


def _matches_properties(kind, properties, query_filter):
    if kind != query_filter.kind:
        return False
    return all(_condition_matches(properties, condition) for condition in query_filter.conditions)


def _feature_kind(feature):
    properties = feature.get("properties") or {}
    kind = properties.get("@type")
    if kind in ("node", "way", "relation"):
        return kind
    osm_id = str(properties.get("@id", ""))
    if "/" in osm_id:
        return osm_id.split("/", 1)[0]
    geometry = feature.get("geometry") or {}
    if geometry.get("type") == "Point":
        return "node"
    return "way"


def matches(feature, query_filter):
    return _matches_properties(
        _feature_kind(feature), feature.get("properties") or {}, query_filter
    )


def filter_features(features, query_text):
    filters = [item for item in parse_query(query_text) if not item.special]
    if not filters:
        return []
    return [
        feature for feature in features
        if any(matches(feature, query_filter) for query_filter in filters)
    ]


@dataclass
class QuerySpec:
    name: str
    filters: list


class QueryIndex:
    def __init__(self, query_texts):
        self.specs = []
        self.by_key = {}
        self.other = []
        self.warnings = []
        for name, text in query_texts.items():
            filters = [item for item in parse_query(text) if not item.special]
            if not filters:
                if "r.relations" in text:
                    self.warnings.append(
                        f"{name}: relation-member queries are not supported in PBF mode; "
                        "use online Overpass for this layer."
                    )
                continue
            spec = QuerySpec(name, filters)
            self.specs.append(spec)
            for query_filter in filters:
                if not query_filter.conditions:
                    self.other.append((spec, query_filter))
                    continue
                key, operator, value, regex_operator, regex = query_filter.conditions[0]
                if operator == "=":
                    index = (query_filter.kind, key, "eq", value)
                elif operator is None and regex_operator is None:
                    index = (query_filter.kind, key, "exists", None)
                else:
                    self.other.append((spec, query_filter))
                    continue
                self.by_key.setdefault(index, []).append((spec, query_filter))

    def matching_names(self, kind, properties):
        candidates = []
        seen = set()
        for key, value in properties.items():
            for index in (
                (kind, key, "eq", value),
                (kind, key, "exists", None),
            ):
                for item in self.by_key.get(index, []):
                    identity = id(item)
                    if identity not in seen:
                        seen.add(identity)
                        candidates.append(item)
        for item in self.other:
            identity = id(item)
            if identity not in seen:
                seen.add(identity)
                candidates.append(item)
        names = set()
        for spec, query_filter in candidates:
            if spec.name in names:
                continue
            if _matches_properties(kind, properties, query_filter):
                names.add(spec.name)
        return names


@dataclass
class PbfProbe:
    total: int
    in_bbox: int
    matched: int
    elapsed: float


@dataclass
class PbfDataset:
    feature_files: dict
    warnings: list = field(default_factory=list)
    dataset: object = None
    temp_files: list = field(default_factory=list)

    def close(self):
        self.dataset = None
        for path in self.temp_files:
            Path(path).unlink(missing_ok=True)
        self.temp_files = []


def _decompress(source, temp_dir, stem="source", cancel=None):
    suffix = source.suffix.lower()
    if suffix in (".bz2", ".gz"):
        opener = bz2.open if suffix == ".bz2" else gzip.open
        destination = Path(temp_dir) / f"{stem}.osm"
        try:
            with opener(source, "rb") as reader, destination.open("wb") as writer:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("PBF decompression cancelled")
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination, [destination]
    if suffix == ".o5m":
        raise PbfError("The .o5m format is not supported without osmium; use .pbf or .osm.")
    return source, []


def _bbox_geometry(ogr, bbox):
    wkt = (
        "POLYGON (("
        f"{bbox.lon_min} {bbox.lat_min},"
        f"{bbox.lon_max} {bbox.lat_min},"
        f"{bbox.lon_max} {bbox.lat_max},"
        f"{bbox.lon_min} {bbox.lat_max},"
        f"{bbox.lon_min} {bbox.lat_min}"
        "))"
    )
    return ogr.CreateGeometryFromWkt(wkt)


def _kind_for_layer(layer_name, feature):
    if layer_name == "points":
        return "node"
    if layer_name == "lines":
        return "way"
    if layer_name == "multipolygons":
        way_id = feature.GetField("osm_way_id")
        if way_id not in (None, "", 0, "0"):
            return "way"
        return "relation"
    return "relation"


def _field(feature, name):
    index = feature.GetFieldIndex(name)
    return feature.GetField(index) if index >= 0 else None


def _feature_properties(feature, definition, kind):
    properties = parse_tags(_field(feature, "other_tags"))
    for index in range(definition.GetFieldCount()):
        name = definition.GetFieldDefn(index).GetName()
        if name in INTERNAL_FIELDS:
            continue
        value = feature.GetField(index)
        if value is not None and str(value) != "":
            properties[name] = str(value)

    osm_id = _field(feature, "osm_id")
    if kind == "way":
        osm_id = _field(feature, "osm_way_id") or osm_id
    if osm_id in (None, "", 0, "0"):
        return {}
    properties["@id"] = f"{kind}/{osm_id}"
    properties["@type"] = kind
    return properties


def _feature_geometry(feature):
    geometry = feature.GetGeometryRef()
    if geometry is None or geometry.IsEmpty():
        return None
    try:
        return json.loads(geometry.ExportToJson())
    except Exception:
        return None


def _open_dataset(gdal, source, temp_dir):
    gdal.SetConfigOption("CPL_TMPDIR", str(temp_dir))
    options = [
        "INTERLEAVED_READING=YES",
        "USE_CUSTOM_INDEXING=YES",
        "MAX_TMPFILE_SIZE=100",
        "TAGS_FORMAT=JSON",
    ]
    dataset = gdal.OpenEx(str(source), gdal.OF_VECTOR, open_options=options)
    if dataset is not None:
        return dataset
    options = [option for option in options if not option.startswith("TAGS_FORMAT")]
    return gdal.OpenEx(str(source), gdal.OF_VECTOR, open_options=options)


def _filter_extent(bbox):
    """Область грубого фильтра: bbox с запасом вокруг.

    Запас нужен, чтобы объекты, пересекающие границу области, пришли с
    целой геометрией: иначе GDAL обрезает линии по фильтру, и полигоны
    relations собираются из незамкнутых колец.  Всё, что попало в запас,
    отсекается точным проверкой пересечения с самим bbox.
    """
    width = bbox.lon_max - bbox.lon_min
    height = bbox.lat_max - bbox.lat_min
    margin = max(FILTER_MARGIN_DEGREES, FILTER_MARGIN_RATIO * max(width, height))
    return (
        bbox.lon_min - margin,
        bbox.lon_max + margin,
        bbox.lat_min - margin,
        bbox.lat_max + margin,
    )


def _apply_spatial_filter(layers, source, bbox):
    """Включает грубый фильтр GDAL по области.

    Фильтр работает только для .pbf: драйвер OSM не умеет фильтровать
    XML-файлы .osm и с фильтром не отдаёт ни одного объекта.  Такие файлы
    читаются полностью, как и раньше.
    """
    if Path(source).suffix.lower() != ".pbf":
        return False
    extent = _filter_extent(bbox)
    for layer in layers:
        layer.SetSpatialFilterRect(*extent)
    return True


def _expected_objects(source, bbox):
    """Оценка числа объектов в той части файла, которая читается."""
    try:
        total = max(1, source.stat().st_size // 70)
    except OSError:
        return 1
    try:
        if Path(source).suffix.lower() != ".pbf":
            return total
        from .pbf_fast import _header_bbox

        with source.open("rb") as handle:
            box = _header_bbox(handle)
    except Exception:
        return total
    if not box:
        return total
    left, right, top, bottom = box
    file_area = max(1e-9, (right - left) * (top - bottom))
    extent = _filter_extent(bbox)
    area = max(0.0, (extent[1] - extent[0]) * (extent[3] - extent[2]))
    share = min(1.0, area / file_area)
    return max(1, int(total * share))


def prepare_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                    progress=None, sources=None):
    gdal, ogr = _gdal()
    paths = [Path(item).expanduser() for item in (sources or [config.pbf_path])]
    for source in paths:
        if not source.exists():
            raise FileNotFoundError(f"OSM extract not found: {source}")
    if not query_names:
        return PbfDataset({}, [])

    config.ensure_directories()
    query_texts = query_texts or {}
    index = QueryIndex({name: query_texts[name] for name in query_names if name in query_texts})
    bbox_geometry = _bbox_geometry(ogr, bbox)
    pbf_dir = config.temp_dir / "pbf"
    pbf_dir.mkdir(parents=True, exist_ok=True)
    handles = {}
    feature_files = {}
    temp_files = []
    features_written = 0
    features_seen = 0
    seen = set()
    total_files = len(paths)
    # Rough object density of OSM extracts (about 70 bytes per node, way and
    # relation).  It is only used to show a progress scale while GDAL decodes
    # the file, so a rough guess is enough.  With the spatial filter only the
    # objects of the filtered area are read, so the scale is taken for that
    # part of the file.
    expected = sum(_expected_objects(source, bbox) for source in paths)
    last_percent = 0

    try:
        for position, original in enumerate(paths, 1):
            source, temporary = _decompress(
                original, config.temp_dir, stem=f"source_{position}", cancel=cancel
            )
            temp_files.extend(temporary)
            if progress:
                progress(
                    f"Opening {source.name} with GDAL OSM driver "
                    f"({position}/{total_files})"
                )

            dataset = _open_dataset(gdal, source, config.temp_dir)
            if dataset is None:
                raise PbfError(
                    f"GDAL could not open {source.name}. "
                    "Make sure the OSM/PBF driver is available in this QGIS build."
                )

            try:
                layers = [
                    layer for layer in (dataset.GetLayerByName(name) for name in LAYER_NAMES)
                    if layer is not None
                ]
                # Два прохода.  Первый — грубый фильтр GDAL по области с
                # запасом: драйвер не строит геометрию объектов, которые
                # заведомо далеко от bbox, и до файла в 500 МБ и больше дело
                # не доходит целиком.  Второй — точная проверка пересечения
                # с самим bbox: в выход попадают только кандидаты, реально
                # пересекающие область.
                _apply_spatial_filter(layers, source, bbox)
                while True:
                    has_features = False
                    for layer in layers:
                        definition = layer.GetLayerDefn()
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError("PBF processing cancelled")
                            feature = layer.GetNextFeature()
                            if feature is None:
                                break
                            has_features = True
                            features_seen += 1
                            if features_seen % 10000 == 0 and progress:
                                percent = min(
                                    99, features_seen * 100 // max(1, expected)
                                )
                                if percent - last_percent >= 5:
                                    last_percent = percent
                                    progress(
                                        f"PBF: processed {features_seen} "
                                        f"objects ({percent}%)"
                                    )

                            raw_geometry = feature.GetGeometryRef()
                            if raw_geometry is None or raw_geometry.IsEmpty():
                                continue
                            try:
                                if not raw_geometry.Intersects(bbox_geometry):
                                    continue
                            except Exception:
                                continue

                            kind = _kind_for_layer(layer.GetName(), feature)
                            properties = _feature_properties(feature, definition, kind)
                            if not properties:
                                continue

                            matched = index.matching_names(kind, properties)
                            if not matched:
                                continue
                            # Геометрию в JSON переводим только для объектов,
                            # которые действительно попадут в выход.
                            geometry = _feature_geometry(feature)
                            if geometry is None:
                                continue
                            identity = (layer.GetName(), properties.get("@id"))
                            if identity[1]:
                                if identity in seen:
                                    continue
                                seen.add(identity)
                            obj = {
                                "type": "Feature",
                                "properties": properties,
                                "geometry": geometry,
                            }
                            for name in matched:
                                handle = handles.get(name)
                                if handle is None:
                                    path = pbf_dir / f"{name}.ndjson"
                                    handle = path.open("w", encoding="utf-8")
                                    handles[name] = handle
                                    feature_files[name] = path
                                json.dump(obj, handle, ensure_ascii=False, separators=(",", ":"))
                                handle.write("\n")
                            features_written += 1
                    if not has_features:
                        break
            finally:
                dataset = None
            if progress and total_files > 1:
                progress(f"PBF: file {position}/{total_files} processed")
    finally:
        for handle in handles.values():
            handle.close()

    for name, path in list(feature_files.items()):
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            del feature_files[name]

    warnings = list(index.warnings)
    warnings.append(
        "PBF mode reads the extract with a coarse bbox filter: only the objects "
        "of the area are decoded, the rest of the file is skipped."
    )
    if total_files > 1:
        warnings.append(f"GeoFabrik: {total_files} карты обработаны как один источник.")
    if progress:
        progress(f"PBF: {features_seen} objects, {features_written} feature matches")
    return PbfDataset(
        feature_files=feature_files,
        warnings=warnings,
        temp_files=temp_files,
    )


def probe_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                  progress=None, sources=None):
    gdal, ogr = _gdal()
    paths = [Path(item).expanduser() for item in (sources or [config.pbf_path])]
    for source in paths:
        if not source.exists():
            raise FileNotFoundError(f"OSM extract not found: {source}")

    config.ensure_directories()
    query_texts = query_texts or {}
    index = QueryIndex({name: query_texts[name] for name in query_names if name in query_texts})
    bbox_geometry = _bbox_geometry(ogr, bbox)
    total = 0
    in_bbox = 0
    matched = 0
    seen = set()
    started = time.monotonic()

    for position, original in enumerate(paths, 1):
        stem = f"probe_{time.monotonic_ns()}_{position}"
        source, temporary = _decompress(
            original, config.temp_dir, stem=stem, cancel=cancel
        )
        dataset = _open_dataset(gdal, source, config.temp_dir)
        if dataset is None:
            for path in temporary:
                Path(path).unlink(missing_ok=True)
            raise PbfError(f"GDAL could not open {source.name}")

        try:
            layers = [
                layer for layer in (dataset.GetLayerByName(name) for name in LAYER_NAMES)
                if layer is not None
            ]
            # Тот же грубый фильтр, что и при подготовке: оценка не должна
            # перебирать весь файл.
            _apply_spatial_filter(layers, source, bbox)
            while True:
                has_features = False
                for layer in layers:
                    definition = layer.GetLayerDefn()
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError("PBF probe cancelled")
                        feature = layer.GetNextFeature()
                        if feature is None:
                            break
                        has_features = True
                        total += 1
                        if total % 50000 == 0 and progress:
                            progress(f"PBF: проверено {total} объектов")
                        geometry = feature.GetGeometryRef()
                        if geometry is None or geometry.IsEmpty():
                            continue
                        try:
                            if not geometry.Intersects(bbox_geometry):
                                continue
                        except Exception:
                            continue
                        in_bbox += 1
                        kind = _kind_for_layer(layer.GetName(), feature)
                        properties = _feature_properties(feature, definition, kind)
                        if properties and index.matching_names(kind, properties):
                            identity = (layer.GetName(), properties.get("@id"))
                            if identity[1]:
                                if identity in seen:
                                    continue
                                seen.add(identity)
                            matched += 1
                if not has_features:
                    break
        finally:
            dataset = None
            for path in temporary:
                Path(path).unlink(missing_ok=True)

    return PbfProbe(total, in_bbox, matched, time.monotonic() - started)
