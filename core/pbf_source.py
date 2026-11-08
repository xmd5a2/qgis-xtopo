import bz2
import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import osmium_prefilter
from .osm_convert import FieldNames


LAYER_NAMES = ("points", "lines", "multipolygons", "multilinestrings", "other_relations")

# Порядок опроса слоёв при INTERLEAVED_READING: драйвер читает файл
# потоково (nodes -> ways -> relations), и слои нужно опрашивать по
# одному объекту за цикл строго в этом порядке.  Чтение слоя до конца
# подряд ломает сборку мультиполигонов: в охвате с 52 реляциями-ледниками
# доходило лишь 1-2 (остальные молча терялись вместе с way-полигонами).
INTERLEAVED_LAYER_ORDER = (
    "points", "lines", "multilinestrings", "multipolygons", "other_relations",
)

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


# Запасной конфиг OSM-драйвера, если системный osmconf.ini не найден.
# Главное отличие от стандартного — ele не исключается из other_tags:
# без этого у пиков нет высоты, и подписи с отметками не работают.
_FALLBACK_OSM_CONFIG = """[points]
osm_id=yes
attributes=name,barrier,highway,ref,address,is_in,place,man_made
unsignificant=created_by,converted_by,source,time,ele,attribution
ignore=created_by,converted_by,source,time,note,todo,openGeoDB:,fixme,FIXME
"""


def _system_osm_config():
    """Путь к системному osmconf.ini, если он доступен."""
    from osgeo import gdal

    candidates = []
    for name in ("OSM_CONFIG_FILE", "GDAL_DATA"):
        value = gdal.GetConfigOption(name) or os.environ.get(name)
        if value:
            path = Path(value)
            candidates.append(
                path if path.name == "osmconf.ini" else path / "osmconf.ini"
            )
    candidates.extend([
        Path(sys.prefix) / "share" / "gdal" / "osmconf.ini",
        Path("/usr/share/gdal/osmconf.ini"),
        Path("/usr/local/share/gdal/osmconf.ini"),
    ])
    try:
        import qgis

        prefix = Path(qgis.__file__).resolve().parent.parent
        candidates.append(prefix / "share" / "gdal" / "osmconf.ini")
    except Exception:
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def ensure_osm_config(config):
    """Готовит osmconf.ini, сохраняющий тег ele у точек.

    Стандартный конфиг GDAL перечисляет ele в ``ignore``, поэтому
    ``other_tags`` его не содержит.  Копия системного конфига без этого
    тега возвращает высоту в слой пиков.
    """
    source = _system_osm_config()
    if source is not None:
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = _FALLBACK_OSM_CONFIG
    lines = []
    in_points = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_points = stripped == "[points]"
        if in_points and line.startswith("ignore="):
            keys = [
                key for key in line[len("ignore="):].split(",")
                if key.strip() != "ele"
            ]
            line = "ignore=" + ",".join(keys)
        lines.append(line)
    content = "\n".join(lines) + "\n"
    target = Path(config.config_dir) / "osmconf.ini"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
    return target


def _set_osm_config(path):
    from osgeo import gdal

    gdal.SetConfigOption(
        "OSM_CONFIG_FILE", str(path) if path is not None else None
    )


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
    # Сначала известные поля драйвера OSM: они важнее одноимённых тегов из
    # other_tags, иначе тег в другом регистре переименовал бы «name».
    properties = {}
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
    for key, value in parse_tags(_field(feature, "other_tags")).items():
        if key not in properties:
            properties[key] = value
    return properties


def _point_count(geometry):
    """Число точек геометрии с учётом колец полигона.

    У полигона внутри GEOMETRYCOLLECTION GetPointCount() возвращает ноль,
    хотя кольца содержат точки, поэтому у полигонов считаем их иначе.
    """
    if geometry is None:
        return 0
    if geometry.GetGeometryName() not in ("POLYGON", "MULTIPOLYGON"):
        return geometry.GetPointCount()
    total = 0
    parts = (
        [geometry]
        if geometry.GetGeometryName() == "POLYGON"
        else [geometry.GetGeometryRef(index) for index in range(geometry.GetGeometryCount())]
    )
    for part in parts:
        if part is None:
            continue
        for ring in range(part.GetGeometryCount()):
            total += part.GetGeometryRef(ring).GetPointCount()
    return total


def _is_degenerate(geometry):
    """Часть без точек — рисовать её нечем.

    У полигона с пустым кольцом IsEmpty() возвращает False, хотя точек
    нет, поэтому emptiness проверяем по числу точек.
    """
    if geometry is None:
        return True
    if geometry.IsEmpty():
        return True
    return _point_count(geometry) == 0


def _clean_geometry(geometry):
    """Убрать вырожденные части составной геометрии.

    GDAL собирает отношения с разнородными членами в GEOMETRYCOLLECTION, куда
    попадают и полигоны без точек. Такие объекты бесполезны: часть без точек
    не рисуется, а коллекция целиком не совпадает с типом слоя и даёт
    предупреждение при записи. Вырожденные части отбрасываем, а если
    осталась одна часть — берём её.

    Остальные части оставляем коллекцией: собирать из них мультитип нельзя,
    слой ожидает свой тип, а мультитип в него не вставляется.
    """
    if geometry is None or geometry.IsEmpty():
        return None
    if geometry.GetGeometryName() != "GEOMETRYCOLLECTION":
        return geometry
    parts = []
    for index in range(geometry.GetGeometryCount()):
        part = geometry.GetGeometryRef(index)
        if _is_degenerate(part):
            continue
        parts.append(part)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return geometry


def _feature_geometry_json(feature):
    """Геометрия объекта в виде готового JSON.

    Строка берётся прямо из GDAL: разбирать и собирать JSON заново
    для каждой геометрии (особенно крупных полигонов) дорого.
    """
    geometry = _clean_geometry(feature.GetGeometryRef())
    if geometry is None:
        return None
    try:
        return geometry.ExportToJson()
    except Exception:
        return None


def _open_dataset(gdal, source, temp_dir, custom_indexing=True):
    """Открывает PBF драйвером OSM.

    ``custom_indexing`` — собственный индекс GDAL для узлов.  Он быстрее
    обычного, но требует строго возрастающих id узлов.  Карты, склеенные
    osmium из пересекающихся выгрузок, содержат повторяющиеся id (в
    пограничных объектах версии выгрузок различаются), и на таких файлах
    индекс падает с «Non increasing node id» — чтение прерывается.
    Тогда файл открывают заново без индекса.
    """
    gdal.SetConfigOption("CPL_TMPDIR", str(temp_dir))
    options = [
        "INTERLEAVED_READING=YES",
        "USE_CUSTOM_INDEXING=YES" if custom_indexing else "USE_CUSTOM_INDEXING=NO",
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

    Порядок углов — как у ``OGRLayer::SetSpatialFilterRect``
    (minx, miny, maxx, maxy): Python-обёртка GDAL принимает именно его,
    а ``GetEnvelope()`` отдаёт углы в порядке (minx, maxx, miny, maxy), и
    эти порядки нельзя смешивать.
    """
    width = bbox.lon_max - bbox.lon_min
    height = bbox.lat_max - bbox.lat_min
    margin = max(FILTER_MARGIN_DEGREES, FILTER_MARGIN_RATIO * max(width, height))
    return (
        bbox.lon_min - margin,
        bbox.lat_min - margin,
        bbox.lon_max + margin,
        bbox.lat_max + margin,
    )


def _extend_box(box, min_x, max_x, min_y, max_y):
    """Расширяет рамку прочитанных объектов.

    Аргументы — в порядке GetEnvelope, рамка хранится как
    (min_x, min_y, max_x, max_y).
    """
    if box is None:
        return (min_x, min_y, max_x, max_y)
    return (
        min(box[0], min_x),
        min(box[1], min_y),
        max(box[2], max_x),
        max(box[3], max_y),
    )


def _filter_delivered(read_box, area):
    """Доставил ли фильтр драйвера нужную область.

    Драйвер OSM выбирает блоки файла, пересекающиеся с областью фильтра,
    и отдаёт объекты целиком.  Поэтому рамка прочитанного должна
    пересекаться с самой областью.  В некоторых выгрузках индекс PBF
    битый, и драйвер отдаёт блоки из другого места файла: тогда такой
    фильтр только мешает, и файл нужно читать целиком.

    ``area`` — прямоугольник в порядке (слева, справа, снизу, сверху).
    """
    if read_box is None:
        return False
    left, right, bottom, top = area
    min_x, min_y, max_x, max_y = read_box
    return not (
        max_x < left or min_x > right or max_y < bottom or min_y > top
    )


def _apply_spatial_filter(layers, source, bbox):
    """Включает грубый фильтр GDAL по области.

    Фильтр работает только для .pbf: драйвер OSM не умеет фильтровать
    XML-файлы .osm и с фильтром не отдаёт ни одного объекта.  Такие файлы
    читаются полностью, как и раньше.

    Слой ``points`` фильтровать нельзя: по документации драйвера OSM
    фильтр, заданный на нём, действует и на остальные слои, и у них
    «пропадают вершины».  Из-за этого теряются объекты, геометрия которых
    выходит далеко за пределы области: отношения с большим кольцом
    (границы регионов, заповедники) отбрасываются целиком, а линии,
    пересекающие край области, приходят обрезанными.  Поэтому фильтр
    ставится на остальные слои, а ``points`` читается без него — точная
    отсечка по самому bbox делается отдельно, при разборе объектов.
    """
    if Path(source).suffix.lower() != ".pbf":
        return False
    min_x, min_y, max_x, max_y = _filter_extent(bbox)
    for layer in layers:
        if layer.GetName() == "points":
            continue
        layer.SetSpatialFilterRect(min_x, min_y, max_x, max_y)
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
    area = max(0.0, (extent[2] - extent[0]) * (extent[3] - extent[1]))
    share = min(1.0, area / file_area)
    return max(1, int(total * share))


def prepare_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                    progress=None, sources=None, use_osmium=True):
    """Читает объекты OSM из PBF и раскладывает их по слоям.

    Собственный индекс GDAL для узлов требует строго возрастающих id.
    Карты, склеенные osmium из пересекающихся выгрузок, содержат
    повторяющиеся id (в пограничных объектах версии выгрузок различаются),
    и на таких файлах индекс падает — чтение прерывается.  Тогда всё
    читается заново без индекса: он медленнее, но работает.

    Если доступен osmium, область сначала вырезается из выгрузок:
    чтение большой карты целиком занимает минуты, а вырезка — секунды.
    """
    from osgeo import gdal

    previous = gdal.GetConfigOption("OSM_CONFIG_FILE")
    osm_config = ensure_osm_config(config)
    if osm_config is not None:
        _set_osm_config(osm_config)
    try:
        paths = [
            Path(item).expanduser()
            for item in (sources or [config.pbf_path])
        ]
        temporary = []
        unfiltered = set()
        if use_osmium and paths:
            paths, temporary, covering = osmium_prefilter.prefilter_sources(
                paths, bbox, config, progress=progress, cancel=cancel
            )
            unfiltered = {str(Path(item).expanduser()) for item in covering}
        try:
            dataset = _prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=paths,
                _custom_indexing=True, unfiltered=unfiltered,
            )
        except RuntimeError:
            if progress:
                progress(
                    "PBF: индекс GDAL не справился с повторяющимися id, "
                    "файл читается заново без индекса"
                )
            dataset = _prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=paths,
                _custom_indexing=False, unfiltered=unfiltered,
            )
        dataset.temp_files.extend(temporary)
        return dataset
    finally:
        _set_osm_config(previous)


def _prepare_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                      progress=None, sources=None, _custom_indexing=True,
                      unfiltered=None):
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
    field_names = {name: FieldNames() for name in query_names}
    temp_files = []
    features_written = 0
    features_seen = 0
    seen = set()
    total_files = len(paths)
    # Файлы, которые читаются целиком: геометрия объектов в них лежит за
    # пределами области, и фильтр драйвера отбросил бы их целиком.
    unfiltered = {str(Path(item).expanduser()) for item in (unfiltered or ())}
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

            area = (
                bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max
            )
            dataset = None
            filter_enabled = True
            # Файлы горных массивов читаются целиком: их вершины лежат за
            # пределами области, и фильтр драйвера их бы отбросил.
            whole_file = str(source) in unfiltered
            if whole_file and progress:
                progress(
                    f"{source.name}: файл читается целиком "
                    "(горные массивы, геометрия вне области)"
                )
            try:
                while True:
                    dataset = _open_dataset(
                        gdal, source, config.temp_dir,
                        custom_indexing=_custom_indexing,
                    )
                    if dataset is None:
                        raise PbfError(
                            f"GDAL could not open {source.name}. "
                            "Make sure the OSM/PBF driver is available in this QGIS build."
                        )
                    layers = [
                        layer for layer in (
                            dataset.GetLayerByName(name)
                            for name in INTERLEAVED_LAYER_ORDER
                        )
                        if layer is not None
                    ]
                    definitions = {
                        layer: layer.GetLayerDefn() for layer in layers
                    }
                    # Два прохода.  Первый — грубый фильтр GDAL по области
                    # с запасом: драйвер не строит геометрию объектов,
                    # которые заведомо далеко от bbox, и до файла в 500 МБ
                    # и больше дело не доходит целиком.  Второй — точная
                    # проверка пересечения с самим bbox: в выход попадают
                    # только кандидаты, реально пересекающие область.
                    use_filter = (
                        filter_enabled
                        and not whole_file
                        and _apply_spatial_filter(layers, source, bbox)
                    )
                    read_box = None
                    has_features = False
                    # Снимок состояния для отката: если фильтр драйвера
                    # не сработал, файл читается заново целиком, и запись
                    # текущего прохода надо отменить.  Откат затрагивает
                    # только текущий файл: результаты предыдущих файлов
                    # остаются в силе.
                    marks = {
                        name: handle.tell() for name, handle in handles.items()
                    }
                    file_ids = set()
                    seen_before = features_seen
                    written_before = features_written
                    # Слои опрашиваются по одному объекту за цикл
                    # (см. INTERLEAVED_LAYER_ORDER): иначе драйвер теряет
                    # мультиполигоны.
                    while True:
                        any_feature = False
                        for layer in layers:
                            if cancel is not None and cancel.is_set():
                                raise InterruptedError(
                                    "PBF processing cancelled"
                                )
                            feature = layer.GetNextFeature()
                            if feature is None:
                                continue
                            any_feature = True
                            has_features = True
                            features_seen += 1
                            if use_filter:
                                raw = feature.GetGeometryRef()
                                if raw is not None and not raw.IsEmpty():
                                    try:
                                        read_box = _extend_box(
                                            read_box, *raw.GetEnvelope()
                                        )
                                    except Exception:
                                        pass
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

                            definition = definitions[layer]
                            kind = _kind_for_layer(layer.GetName(), feature)
                            properties = _feature_properties(feature, definition, kind)
                            if not properties:
                                continue

                            matched = index.matching_names(kind, properties)
                            if not matched:
                                continue
                            # Геометрию в JSON переводим только для объектов,
                            # которые действительно попадут в выход.
                            geometry = _feature_geometry_json(feature)
                            if geometry is None:
                                continue
                            identity = (layer.GetName(), properties.get("@id"))
                            if identity[1]:
                                if identity in seen:
                                    continue
                                seen.add(identity)
                                file_ids.add(identity)
                            base_json = json.dumps(
                                properties, ensure_ascii=False, separators=(",", ":")
                            )
                            for name in matched:
                                handle = handles.get(name)
                                if handle is None:
                                    path = pbf_dir / f"{name}.ndjson"
                                    # Файл открыт в двоичном режиме: позиция
                                    # записи нужна побайтовая, чтобы при
                                    # откате обрезать файл до нужного места.
                                    handle = path.open("wb")
                                    handles[name] = handle
                                    feature_files[name] = path
                                # Имена полей приводятся к уникальным без учёта
                                # регистра: иначе SQLite не создаёт таблицу.
                                resolved = field_names[name].apply(properties)
                                properties_json = (
                                    base_json
                                    if resolved is properties
                                    else json.dumps(
                                        resolved, ensure_ascii=False,
                                        separators=(",", ":"),
                                    )
                                )
                                handle.write(
                                    (
                                        '{"type":"Feature","properties":'
                                        + properties_json
                                        + ',"geometry":'
                                        + geometry
                                        + "}\n"
                                    ).encode("utf-8")
                                )
                            features_written += 1
                        if not any_feature:
                            break
                    if use_filter and not _filter_delivered(read_box, area):
                        # Фильтр драйвера не доставил нужную область: файл
                        # закрываем и читаем заново целиком.  Сбросить фильтр
                        # на том же датасете нельзя — драйвер продолжает
                        # отдавать выбранные ранее блоки.
                        if progress:
                            progress(
                                f"{source.name}: фильтр по области не работает, "
                                "файл читается целиком"
                            )
                        # Откат затрагивает только текущий файл: записи
                        # предыдущих файлов остаются в силе.
                        for name, handle in list(handles.items()):
                            if name in marks:
                                handle.flush()
                                handle.seek(marks[name])
                                handle.truncate()
                                handle.seek(0, os.SEEK_END)
                            else:
                                handle.close()
                                del handles[name]
                                path = feature_files.pop(name, None)
                                if path is not None:
                                    path.unlink(missing_ok=True)
                        seen.difference_update(file_ids)
                        features_seen = seen_before
                        features_written = written_before
                        last_percent = 0
                        dataset = None
                        use_filter = False
                        filter_enabled = False
                        continue
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
                layer for layer in (
                    dataset.GetLayerByName(name)
                    for name in INTERLEAVED_LAYER_ORDER
                )
                if layer is not None
            ]
            definitions = {
                layer: layer.GetLayerDefn() for layer in layers
            }
            # Тот же грубый фильтр, что и при подготовке: оценка не должна
            # перебирать весь файл.  Чтение — тем же round-robin протоколом.
            _apply_spatial_filter(layers, source, bbox)
            while True:
                any_feature = False
                for layer in layers:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("PBF probe cancelled")
                    feature = layer.GetNextFeature()
                    if feature is None:
                        continue
                    any_feature = True
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
                    definition = definitions[layer]
                    properties = _feature_properties(feature, definition, kind)
                    if properties and index.matching_names(kind, properties):
                        identity = (layer.GetName(), properties.get("@id"))
                        if identity[1]:
                            if identity in seen:
                                continue
                            seen.add(identity)
                        matched += 1
                if not any_feature:
                    break
        finally:
            dataset = None
            for path in temporary:
                Path(path).unlink(missing_ok=True)

    return PbfProbe(total, in_bbox, matched, time.monotonic() - started)
