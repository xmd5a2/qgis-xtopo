import bz2
import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import osmium_prefilter, osmium_tool
from .osm_convert import AREA_MAN_MADE, FieldNames


# Порядок слоёв драйвера OSM: драйвер читает файл потоково
# (nodes -> ways -> relations) и раскладывает объекты по этим слоям.
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
OPL_ESCAPE = re.compile(r"%([0-9a-fA-F]+)%")

# Слои, где восстанавливаются way-члены отношений.  Драйвер GDAL не
# отдаёт отдельным объектом замкнутый внешний член мультиполигона: он
# входит в геометрию отношения.  Если у самого way есть собственные теги
# (постоянная вода внутри пересыхающего водохранилища, граница с
# admin_level), эти теги теряются и объект пропадает из слоя.  osmium
# достаёт такие члены по тегам отношений; совпадающие по слою с полным
# отношением не добавляются, чтобы не плодить дубликаты.
RELATION_MEMBER_LAYERS = (
    "water", "water_intermittent", "admin_level_2", "admin_level_4",
)

# Для этих слоёв osmium ищет ещё и сами way: граница может быть внешним
# членом отношения другого уровня (город, admin_level=7), и тогда GDAL
# забирает её в геометрию этого отношения, хотя собственный тег way
# относится к стране или региону.
RELATION_MEMBER_WAY_LAYERS = ("admin_level_2", "admin_level_4")


class PbfError(RuntimeError):
    pass


# Год GDAL, в котором появилось датасетное чтение с указанием слоя.
MINIMAL_GDAL = 3060000


def _gdal():
    from osgeo import gdal, ogr

    from .memory import gdal_cache_mb

    gdal.UseExceptions()
    ogr.UseExceptions()
    gdal.SetConfigOption("GDAL_CACHEMAX", str(gdal_cache_mb()))
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

# Стандартный список closed_ways_are_polygons из osmconf.ini.  Нужен,
# только если строки в конфиге нет вовсе: создавать её надо целиком,
# иначе GDAL потеряет площадные ключи по умолчанию.
_DEFAULT_POLYGON_KEYS = (
    "aeroway", "amenity", "boundary", "building", "craft", "geological",
    "historic", "landuse", "leisure", "military", "natural", "office", "place",
    "shop", "sport", "tourism", "highway=platform", "public_transport=platform",
)


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
    """Готовит osmconf.ini для чтения PBF.

    Копия системного конфига без ``ele`` в ``ignore`` возвращает высоту
    в слой пиков.  В ``closed_ways_are_polygons`` дописываются площадные
    ``man_made`` из ``AREA_MAN_MADE``: без них замкнутые резервуары и
    силосы приходили линиями и пропадали в полигональных слоях.
    """
    source = _system_osm_config()
    if source is not None:
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = _FALLBACK_OSM_CONFIG
    additions = [f"man_made={value}" for value in sorted(AREA_MAN_MADE)]
    present = any(
        line.strip().startswith("closed_ways_are_polygons=")
        for line in text.splitlines()
    )
    inserted = present
    lines = []
    in_points = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and not inserted:
            # Строки нет в конфиге: создаём её в общей секции, иначе
            # GDAL останется без площадных ключей по умолчанию.
            lines.append(
                "closed_ways_are_polygons="
                + ",".join(_DEFAULT_POLYGON_KEYS + tuple(additions))
            )
            inserted = True
        if stripped.startswith("["):
            in_points = stripped == "[points]"
        if stripped.startswith("closed_ways_are_polygons="):
            tokens = [
                token.strip()
                for token in stripped.split("=", 1)[1].split(",")
                if token.strip()
            ]
            for pair in additions:
                if pair not in tokens:
                    tokens.append(pair)
            line = "closed_ways_are_polygons=" + ",".join(tokens)
            inserted = True
        if in_points and stripped.startswith("ignore="):
            keys = [
                key for key in stripped[len("ignore="):].split(",")
                if key.strip() != "ele"
            ]
            line = "ignore=" + ",".join(keys)
        lines.append(line)
    if not inserted:
        lines.append(
            "closed_ways_are_polygons="
            + ",".join(_DEFAULT_POLYGON_KEYS + tuple(additions))
        )
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
            missing = re.match(r'^!\s*"([^"]+)"$', raw)
            if missing:
                # [!"key"] — тега нет; в отличие от [key!="value"],
                # отсутствие тега здесь как раз и требуется.
                conditions.append((missing.group(1), "!", None, None, None))
                continue
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
    if operator == "!":
        return key not in properties
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
        self.keys_by_kind = {}
        self.other = []
        # Разобранные запросы по видам объектов.  Подбор идёт по этим
        # таблицам: здесь нет кортежей-ключей и проверок под условие,
        # которые индекс уже решил.
        self._eq_tables = {}
        self._exists_tables = {}
        self._other_by_kind = {}
        # Запросы, которым нужны члены отношений: драйвер PBF их не
        # отдаёт.  Это ограничение режима, а не ошибка данных, поэтому
        # список попадает в информационные заметки.
        self.unsupported = []
        for name, text in query_texts.items():
            filters = [item for item in parse_query(text) if not item.special]
            if not filters:
                if "r.relations" in text:
                    self.unsupported.append(name)
                continue
            spec = QuerySpec(name, filters)
            self.specs.append(spec)
            for query_filter in filters:
                if not query_filter.conditions:
                    self.other.append((spec, query_filter))
                    self._add_other(query_filter.kind, spec, query_filter)
                    continue
                key, operator, value, regex_operator, regex = query_filter.conditions[0]
                if operator == "=":
                    index = (query_filter.kind, key, "eq", value)
                elif operator is None and regex_operator is None:
                    index = (query_filter.kind, key, "exists", None)
                else:
                    self.other.append((spec, query_filter))
                    self._add_other(query_filter.kind, spec, query_filter)
                    continue
                self.by_key.setdefault(index, []).append((spec, query_filter))
                self.keys_by_kind.setdefault(query_filter.kind, set()).add(key)
                self._add_indexed(
                    query_filter, spec, index, key, value,
                )

    def _add_indexed(self, query_filter, spec, index, key, value):
        """Запомнить запрос в таблице по виду объекта.

        Простые запросы (одно условие) проверять при подборе не нужно:
        условие уже выполнено — объект попал в таблицу по самому
        значению поля.  Остальные проверяются как раньше.
        """
        simple = len(query_filter.conditions) == 1
        item = (spec, query_filter, simple)
        kind = query_filter.kind
        if index[2] == "eq":
            table = self._eq_tables.setdefault(kind, {}).setdefault(key, {})
            table.setdefault(value, []).append(item)
        else:
            table = self._exists_tables.setdefault(kind, {}).setdefault(key, [])
            table.append(item)

    def _add_other(self, kind, spec, query_filter):
        """Запрос без условия по значению: проверяется целиком."""
        self._other_by_kind.setdefault(kind, []).append(
            (spec, query_filter, False)
        )

    def matching_names(self, kind, properties):
        """Слои, в которые попадает объект с такими свойствами.

        Объектов в выгрузке миллионы, а запросов — сотни, поэтому
        подбор идёт по индексу: сначала по значению поля, потом по
        наличию поля.  Условие, по которому объект попал в таблицу,
        выполняется по построению, поэтому простые запросы (с одним
        условием) не проверяются повторно.
        """
        names = set()
        for key, table in self._eq_tables.get(kind, {}).items():
            value = properties.get(key)
            if value is None:
                continue
            items = table.get(value)
            if not items:
                continue
            for spec, query_filter, simple in items:
                if spec.name in names:
                    continue
                if simple or _matches_properties(
                    kind, properties, query_filter
                ):
                    names.add(spec.name)
        for key, items in self._exists_tables.get(kind, {}).items():
            if properties.get(key) is None:
                continue
            for spec, query_filter, simple in items:
                if spec.name in names:
                    continue
                if simple or _matches_properties(
                    kind, properties, query_filter
                ):
                    names.add(spec.name)
        for spec, query_filter, simple in self._other_by_kind.get(kind, ()):
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
    # Ожидаемые особенности режима PBF: это не предупреждения, а пояснения
    # к журналу (грубый фильтр, объединение выгрузок).
    notes: list = field(default_factory=list)
    dataset: object = None
    temp_files: list = field(default_factory=list)
    # Исходные PBF: по ним fallback названий границ читает теги отношений,
    # которые драйвер GDAL выбрасывает вместе с неполной геометрией.
    source_paths: list = field(default_factory=list)

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


def _kind_for_layer(layer_name, feature, internal=None):
    """Вид объекта слоя: точка, линия или отношение.

    ``internal`` — индексы служебных полей слоя: индекс поля берётся из
    кэша, а не ищется по имени на каждом объекте.
    """
    if layer_name == "points":
        return "node"
    if layer_name == "lines":
        return "way"
    if layer_name == "multipolygons":
        index = None if internal is None else internal.get("osm_way_id")
        way_id = (
            feature.GetField(index) if index is not None
            else feature.GetField("osm_way_id")
        )
        if way_id not in (None, "", 0, "0"):
            return "way"
        return "relation"
    return "relation"


def _source_properties(properties, allowed):
    """Свойства для записи в источник: только нужные рецепту.

    ``allowed`` — точные имена полей; названия (``name``, ``name:*``)
    сохраняются всегда: от них зависит переключение языка карты.
    """
    if allowed is None:
        return properties
    return {
        key: value for key, value in properties.items()
        if key in allowed or key.lower().startswith("name")
    }


def _write_matches(handles, feature_files, field_names, pbf_dir, matched,
                   properties, geometry, field_sets):
    """Записывает объект в NDJSON всех подходящих запросов."""
    base_json = None
    for name in matched:
        handle = handles.get(name)
        if handle is None:
            path = pbf_dir / f"{name}.ndjson"
            # Файл открыт в двоичном режиме: позиция записи нужна
            # побайтовая, чтобы при откате обрезать файл до нужного
            # места.
            handle = path.open("wb")
            handles[name] = handle
            feature_files[name] = path
        # В источник пишем только поля, которые нужны рецепту:
        # GeoJSON-драйвер разбирает все свойства, и сотни лишних тегов
        # стоили десятки секунд на каждом сохранении.
        allowed = field_sets.get(name)
        if allowed is None:
            source_properties = properties
            if base_json is None:
                base_json = json.dumps(
                    properties, ensure_ascii=False, separators=(",", ":"),
                )
            source_json = base_json
        else:
            source_properties = _source_properties(properties, allowed)
            source_json = json.dumps(
                source_properties, ensure_ascii=False,
                separators=(",", ":"),
            )
        # Имена полей приводятся к уникальным без учёта регистра: иначе
        # SQLite не создаёт таблицу.
        resolved = field_names[name].apply(source_properties)
        properties_json = (
            source_json
            if resolved is source_properties
            else json.dumps(
                resolved, ensure_ascii=False, separators=(",", ":"),
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


def _unescape_opl(value):
    """Декодирует значение OPL (``%<hex>%``, включая не-ASCII)."""
    def decode(match):
        try:
            return chr(int(match.group(1), 16))
        except (ValueError, OverflowError):
            return match.group(0)

    return OPL_ESCAPE.sub(decode, value)


def _opl_tags(token):
    """Теги из поля OPL, начинающегося с ``T``."""
    tags = {}
    for item in token[1:].split(","):
        key, separator, value = item.partition("=")
        if separator and key:
            tags[_unescape_opl(key)] = _unescape_opl(value)
    return tags


def _parse_opl_objects(text):
    """Узлы, way и отношения из вывода ``osmium ... -f opl``.

    Принимает строку или итерируемое из строк: файл может быть большим,
    а разбор потоковый.  Возвращает три словаря: координаты узлов,
    контуры и теги way, участников и теги отношений.
    """
    if isinstance(text, str):
        text = text.splitlines()
    nodes = {}
    ways = {}
    relations = {}
    for line in text:
        line = line.rstrip("\r\n")
        if line.startswith("n"):
            match = re.match(r"n(\d+)", line)
            if not match:
                continue
            lon = lat = None
            for token in line.split(" ")[1:]:
                if token.startswith("x"):
                    try:
                        lon = float(token[1:])
                    except ValueError:
                        lon = None
                elif token.startswith("y"):
                    try:
                        lat = float(token[1:])
                    except ValueError:
                        lat = None
            if lon is not None and lat is not None:
                nodes[int(match.group(1))] = (lon, lat)
        elif line.startswith("w"):
            match = re.match(r"w(\d+)", line)
            if not match:
                continue
            refs = []
            tags = {}
            for token in line.split(" ")[1:]:
                if token.startswith("N"):
                    refs = [
                        int(item[1:]) for item in token[1:].split(",")
                        if item.startswith("n") and item[1:].isdigit()
                    ]
                elif token.startswith("T"):
                    tags = _opl_tags(token)
            ways[int(match.group(1))] = (refs, tags)
        elif line.startswith("r"):
            match = re.match(r"r(\d+)", line)
            if not match:
                continue
            members = []
            tags = {}
            for token in line.split(" ")[1:]:
                if token.startswith("M"):
                    for item in token[1:].split(","):
                        member = re.match(
                            r"^([nwr])(\d+)(?:@(\S*))?$", item
                        )
                        if member:
                            members.append((
                                member.group(1),
                                int(member.group(2)),
                                member.group(3) or "",
                            ))
                elif token.startswith("T"):
                    tags = _opl_tags(token)
            relations[int(match.group(1))] = (members, tags)
    return nodes, ways, relations


def _member_filters(index, names, kind):
    """Фильтры osmium для объектов указанного вида, у которых нужны члены.

    Берутся только положительные условия ``[key="value"]``: они дают
    надмножество объектов запроса, а точное совпадение (включая
    ``intermittent``) проверяется тем же ``QueryIndex``, что и при
    обычном чтении PBF.
    """
    specs = {spec.name: spec for spec in index.specs}
    expressions = []
    for name in names:
        spec = specs.get(name)
        if spec is None:
            continue
        for query_filter in spec.filters:
            if query_filter.kind != kind:
                continue
            parts = []
            for key, operator, value, _regex_operator, _regex in (
                query_filter.conditions
            ):
                if operator != "=" or not value:
                    continue
                if any(item in key or item in value for item in (",", "=")):
                    continue
                parts.append(f"{key}={value}")
            expression = f"{kind[:1]}/" + ",".join(parts)
            if parts and expression not in expressions:
                expressions.append(expression)
    return expressions


def _relation_member_filters(index, names):
    return _member_filters(index, names, "relation")


def _way_member_filters(index, names):
    return _member_filters(index, names, "way")


def _ring_geometry(points):
    """Полигон из замкнутого кольца координат; None, если он вырожден."""
    from osgeo import ogr

    if len(points) < 4 or points[0] != points[-1]:
        return None
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat in points:
        ring.AddPoint_2D(lon, lat)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    if polygon.IsEmpty() or polygon.GetArea() <= 0:
        return None
    return polygon


def _recover_relation_member_ways(config, index, handles, feature_files,
                                  field_names, pbf_dir, field_sets, seen,
                                  paths, bbox_geometry, cancel, progress,
                                  warnings):
    """Дописывает way-члены отношений, которых не отдал драйвер GDAL.

    Драйвер строит мультиполигон из членов отношения и не возвращает
    внешние way отдельными объектами.  Если у way есть собственные
    площадные теги, он пропадает из своего слоя: например, постоянная
    вода внутри пересыхающего водохранилища остаётся дыркой в отношении.
    osmium достаёт отношения по тем же тегам, а члены — вместе с ними.
    Для границ way ищутся и напрямую: член отношения другого уровня
    (город, admin_level=7) забирается в его геометрию, хотя собственный
    тег way относится к стране или региону.

    Объект добавляется только в те слои, которые не покрывает полное
    отношение и в которые его уже не записал основной проход: иначе те
    же полигоны дублировались бы.  Работает без osmium молча: основной
    путь чтения PBF от этого не зависит.
    """
    names = [name for name in RELATION_MEMBER_LAYERS if name in field_sets]
    if not names:
        return 0
    expressions = _relation_member_filters(index, names)
    way_names = [
        name for name in names if name in RELATION_MEMBER_WAY_LAYERS
    ]
    expressions += _way_member_filters(index, way_names)
    if not expressions:
        return 0
    executable = osmium_tool.find_osmium(config.temp_dir)
    if not executable:
        return 0
    nodes = {}
    ways = {}
    relations = {}
    for position, source in enumerate(paths, 1):
        # OPL пишется в файл: у большой выгрузки вывод в память занял бы
        # сотни мегабайт, хотя разбор всё равно потоковый.
        dump = pbf_dir / f"member_ways_{position}.opl"
        command = [
            str(executable), "tags-filter", "--no-progress", "-f", "opl",
            "-o", str(dump), str(source), *expressions,
        ]
        try:
            code, _stdout, stderr = osmium_tool.run_process(
                command, cancel=cancel
            )
            if code != 0:
                message = stderr.decode("utf-8", "replace").strip()
                if message:
                    warnings.append(f"PBF: osmium tags-filter: {message}")
                continue
            with dump.open("r", encoding="utf-8", errors="replace") as reader:
                part_nodes, part_ways, part_relations = _parse_opl_objects(
                    reader
                )
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001 - восстановление не критично
            warnings.append(
                f"PBF: way-члены отношений не прочитаны: {exc}"
            )
            continue
        finally:
            dump.unlink(missing_ok=True)
        nodes.update(part_nodes)
        ways.update(part_ways)
        relations.update(part_relations)
    if not ways:
        return 0

    # Слои, которые уже покрывают отношения-владельцы way: их повторно
    # добавлять не нужно.  Неполное отношение драйвер выбрасывает целиком,
    # поэтому его члены не считаются покрытыми.
    relation_layers = {}
    for members, tags in relations.values():
        properties = dict(tags)
        properties["@type"] = "relation"
        layers = set(index.matching_names("relation", properties))
        if not layers:
            continue
        if any(
            kind == "w" and ref not in ways
            for kind, ref, _role in members
        ):
            continue
        for kind, ref, role in members:
            if kind == "w" and role.lower() == "outer":
                relation_layers.setdefault(ref, set()).update(layers)

    written = 0
    for way_id, (refs, tags) in ways.items():
        if not tags or len(refs) < 4 or refs[0] != refs[-1]:
            continue
        properties = dict(tags)
        properties["@type"] = "way"
        properties["@id"] = f"way/{way_id}"
        matched = set(index.matching_names("way", properties)) & set(names)
        if not matched:
            continue
        # Way уже пришёл отдельным объектом: восстанавливать нечего.
        if any(
            (layer, properties["@id"]) in seen
            for layer in ("lines", "multipolygons")
        ):
            continue
        matched -= relation_layers.get(way_id, set())
        matched = {
            name for name in matched if (name, properties["@id"]) not in seen
        }
        if not matched:
            continue
        points = [nodes.get(ref) for ref in refs]
        if any(point is None for point in points):
            continue
        polygon = _ring_geometry(points)
        if polygon is None:
            continue
        if bbox_geometry is not None and not polygon.Intersects(bbox_geometry):
            continue
        geometry = _feature_geometry_json(polygon)
        if geometry is None:
            continue
        _write_matches(
            handles, feature_files, field_names, pbf_dir, matched,
            properties, geometry, field_sets,
        )
        for name in matched:
            seen.add((name, properties["@id"]))
        written += 1
    if written and progress:
        progress(
            f"PBF: восстановлены way-члены отношений: объектов {written}"
        )
    return written


def _field_entries(definition, cache=None):
    """Поля слоя без служебных, с кэшем на время чтения файла.

    ``definition.GetFieldDefn(index).GetName()`` — вызов SWIG на каждое
    поле каждого объекта.  На миллионе объектов это десятки секунд,
    хотя набор полей у слоя не меняется.
    """
    key = ("fields", getattr(definition, "GetName", lambda: id(definition))())
    entries = cache.get(key) if cache is not None else None
    if entries is None:
        entries = []
        for index in range(definition.GetFieldCount()):
            name = definition.GetFieldDefn(index).GetName()
            if name not in INTERNAL_FIELDS:
                entries.append((index, name))
        if cache is not None:
            cache[key] = entries
    return entries


def _internal_indexes(definition, cache=None):
    """Индексы служебных полей слоя, один раз на файл."""
    definition_key = getattr(definition, "GetName", lambda: id(definition))()
    key = ("internal", definition_key)
    indexes = cache.get(key) if cache is not None else None
    if indexes is None:
        indexes = {}
        for name in ("osm_id", "osm_way_id", "other_tags"):
            index = definition.GetFieldIndex(name)
            if index >= 0:
                indexes[name] = index
        if cache is not None:
            cache[key] = indexes
    return indexes


def _feature_properties(feature, definition, kind, fields=None, internal=None):
    if fields is None:
        fields = _field_entries(definition)
    if internal is None:
        internal = _internal_indexes(definition)
    # Сначала известные поля драйвера OSM: они важнее одноимённых тегов из
    # other_tags, иначе тег в другом регистре переименовал бы «name».
    properties = {}
    for index, name in fields:
        value = feature.GetFieldAsString(index)
        if value:
            properties[name] = value

    osm_id = _field_at(feature, internal.get("osm_id"))
    if kind == "way":
        osm_id = _field_at(feature, internal.get("osm_way_id")) or osm_id
    if osm_id in (None, "", 0, "0"):
        return {}
    properties["@id"] = f"{kind}/{osm_id}"
    properties["@type"] = kind
    for key, value in parse_tags(
        _field_at(feature, internal.get("other_tags"))
    ).items():
        if key not in properties:
            properties[key] = value
    return properties


def _field_at(feature, index):
    return feature.GetField(index) if index is not None else None


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


def _feature_geometry_json(geometry):
    """Геометрия объекта в виде готового JSON.

    Строка берётся прямо из GDAL: разбирать и собирать JSON заново
    для каждой геометрии (особенно крупных полигонов) дорого.
    """
    geometry = _clean_geometry(geometry)
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

    С включёнными исключениями GDAL бросает ошибку вместо ``None``,
    поэтому ошибка открытия переводится в понятное сообщение.
    """
    gdal.SetConfigOption("CPL_TMPDIR", str(temp_dir))
    options = [
        "INTERLEAVED_READING=YES",
        "USE_CUSTOM_INDEXING=YES" if custom_indexing else "USE_CUSTOM_INDEXING=NO",
        "MAX_TMPFILE_SIZE=100",
        "TAGS_FORMAT=JSON",
    ]
    dataset, error = _open_with_options(gdal, source, options)
    if dataset is not None:
        return dataset
    options = [option for option in options if not option.startswith("TAGS_FORMAT")]
    dataset, error = _open_with_options(gdal, source, options)
    if dataset is None:
        raise PbfError(
            f"GDAL could not open {Path(source).name}: {error}. "
            "Make sure the OSM/PBF driver is available in this QGIS build."
        )
    return dataset


def _open_with_options(gdal, source, options):
    """Открывает файл драйвером OSM; (None, причина) — открыть не удалось."""
    try:
        return gdal.OpenEx(str(source), gdal.OF_VECTOR, open_options=options), ""
    except Exception as exc:  # noqa: BLE001 - причина идёт в сообщение
        return None, str(exc).strip() or type(exc).__name__


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


def _dataset_reading_supported():
    """Есть ли в GDAL датасетное чтение с указанием слоя.

    ``Dataset.GetNextFeature(include_layer=True)`` появился в GDAL 3.6.
    Без него остаётся опрос слоёв по кругу, а на больших выгрузках он
    обрывается переполнением очередей слоёв.
    """
    try:
        from osgeo import gdal

        return int(gdal.VersionInfo("VERSION_NUM")) >= MINIMAL_GDAL
    except Exception:
        return False


def _require_dataset_reading():
    """Проверка версии GDAL перед чтением PBF.

    В GDAL старше 3.6 объекты приходится брать опросом слоёв по кругу:
    очередь слоя переполняется, чтение обрывается, и часть объектов
    теряется молча.  Лучше явная ошибка, чем тихая потеря данных.
    """
    if not _dataset_reading_supported():
        raise PbfError(
            "Чтение PBF требует GDAL 3.6 или новее: в старой версии "
            "опрос слоёв по кругу теряет объекты."
        )


def _iter_features(dataset, cancel=None):
    """Отдаёт пары (объект, имя слоя) при чтении PBF.

    Объекты забираются датасетным ``GetNextFeature``: драйвер сам
    переключает слои и разбирает файл потоково.  Опрос слоёв по кругу
    (по одному объекту из каждого) на больших файлах обрывается: очередь
    слоя переполняется — в GDAL она вмещает 100000 объектов, — и драйвер
    обрывает чтение ошибкой «Too many features have accumulated in
    points layer».  Дальше слои lines и multipolygons остаются пустыми:
    с выгрузки Румынии в проект попадало 316 записей леса вместо 6661.
    """
    getter = getattr(dataset, "GetNextFeature", None)
    if getter is None or not _dataset_reading_supported():
        raise PbfError(
            "Чтение PBF требует GDAL 3.6 или новее: в старой версии "
            "опрос слоёв по кругу теряет объекты."
        )
    count = 0
    last_layer = ""
    last_layer_object = None
    while True:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("PBF processing cancelled")
        try:
            result = getter(include_layer=True)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            where = f" in layer {last_layer}" if last_layer else ""
            raise PbfError(
                "GDAL OSM driver failed after "
                f"{count} objects{where}: {detail}"
            ) from exc
        feature, layer = result[0], result[1]
        if feature is None:
            return
        count += 1
        if layer is not None:
            if layer is not last_layer_object:
                last_layer_object = layer
                last_layer = layer.GetName()
        else:
            last_layer = ""
        yield feature, last_layer


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
                    progress=None, sources=None, use_osmium=True,
                    field_sets=None):
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

    _require_dataset_reading()
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
        field_sets = _default_field_sets(query_names, field_sets)
        try:
            dataset = _prepare_dataset(
                config, bbox, query_names, query_texts=query_texts,
                cancel=cancel, progress=progress, sources=paths,
                _custom_indexing=True, unfiltered=unfiltered,
                field_sets=field_sets,
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
                field_sets=field_sets,
            )
        dataset.temp_files.extend(temporary)
        return dataset
    finally:
        _set_osm_config(previous)


def _default_field_sets(query_names, field_sets=None):
    """Поля источника для каждого запроса; None — не фильтровать."""
    if field_sets is not None:
        return field_sets
    from . import query_recipes

    return {
        name: query_recipes.source_fields(name) for name in query_names
    }


def _prepare_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                      progress=None, sources=None, _custom_indexing=True,
                      unfiltered=None, field_sets=None):
    gdal, ogr = _gdal()
    paths = [Path(item).expanduser() for item in (sources or [config.pbf_path])]
    for source in paths:
        if not source.exists():
            raise FileNotFoundError(f"OSM extract not found: {source}")
    if not query_names:
        return PbfDataset({}, [])

    config.ensure_directories()
    query_texts = query_texts or {}
    field_sets = _default_field_sets(query_names, field_sets)
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
    warnings = []
    read_paths = []
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
            read_paths.append(source)
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
                    layers = [
                        layer for layer in (
                            dataset.GetLayerByName(name)
                            for name in INTERLEAVED_LAYER_ORDER
                        )
                        if layer is not None
                    ]
                    definitions = {
                        layer.GetName(): layer.GetLayerDefn()
                        for layer in layers
                    }
                    field_cache = {}
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
                    # Объекты забираются датасетным API: драйвер сам
                    # переключает слои, иначе на больших файлах чтение
                    # обрывается переполнением очереди слоя points
                    # (см. _iter_features).
                    for feature, layer_name in _iter_features(dataset, cancel):
                        features_seen += 1
                        raw_geometry = feature.GetGeometryRef()
                        if use_filter:
                            if (raw_geometry is not None
                                    and not raw_geometry.IsEmpty()):
                                try:
                                    read_box = _extend_box(
                                        read_box, *raw_geometry.GetEnvelope()
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

                        if raw_geometry is None or raw_geometry.IsEmpty():
                            continue
                        try:
                            if not raw_geometry.Intersects(bbox_geometry):
                                continue
                        except Exception:
                            continue

                        definition = definitions.get(layer_name)
                        if definition is None:
                            continue
                        internal = _internal_indexes(definition, field_cache)
                        kind = _kind_for_layer(
                            layer_name, feature, internal,
                        )
                        properties = _feature_properties(
                            feature, definition, kind,
                            _field_entries(definition, field_cache),
                            internal,
                        )
                        if not properties:
                            continue

                        matched = index.matching_names(kind, properties)
                        if not matched:
                            continue
                        # Геометрию в JSON переводим только для объектов,
                        # которые действительно попадут в выход.
                        geometry = _feature_geometry_json(raw_geometry)
                        if geometry is None:
                            continue
                        identity = (layer_name, properties.get("@id"))
                        if identity[1]:
                            if identity in seen:
                                continue
                            seen.add(identity)
                            file_ids.add(identity)
                        _write_matches(
                            handles, feature_files, field_names, pbf_dir,
                            matched, properties, geometry, field_sets,
                        )
                        features_written += 1
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

        features_written += _recover_relation_member_ways(
            config, index, handles, feature_files, field_names, pbf_dir,
            field_sets, seen, read_paths, bbox_geometry, cancel, progress,
            warnings,
        )
    finally:
        for handle in handles.values():
            handle.close()

    for name, path in list(feature_files.items()):
        if path.stat().st_size == 0:
            path.unlink(missing_ok=True)
            del feature_files[name]

    notes = [
        "PBF: чтение идёт с грубым фильтром по области, "
        "остальные объекты файла пропускаются."
    ]
    if total_files > 1:
        notes.append("PBF: выгрузки объединены в один источник.")
    if index.unsupported:
        notes.append(
            "PBF: запросы по членам отношений не поддерживаются и "
            "пропущены: "
            + ", ".join(sorted(index.unsupported))
            + ". Для этих слоёв нужен источник Overpass."
        )
    if progress:
        progress(f"PBF: {features_seen} objects, {features_written} feature matches")
    return PbfDataset(
        feature_files=feature_files,
        warnings=warnings,
        notes=notes,
        temp_files=temp_files,
        source_paths=[str(path) for path in paths],
    )


def _probe_source(gdal, source, temp_dir, index, bbox_geometry, bbox, cancel,
                  progress, seen):
    """Считает объекты одного файла: всего, в области и подходящих.

    Читается так же, как при подготовке: собственным индексом GDAL, а
    при отказе — без него; с грубым фильтром по области, а если фильтр
    не отдал нужную область — файл целиком.
    """
    error = ""
    for custom_indexing in (True, False):
        for use_filter in (True, False):
            dataset = None
            try:
                dataset = _open_dataset(
                    gdal, source, temp_dir, custom_indexing=custom_indexing
                )
                layers = [
                    layer for layer in (
                        dataset.GetLayerByName(name)
                        for name in INTERLEAVED_LAYER_ORDER
                    )
                    if layer is not None
                ]
                definitions = {
                    layer.GetName(): layer.GetLayerDefn() for layer in layers
                }
                field_cache = {}
                filtered = use_filter and _apply_spatial_filter(
                    layers, source, bbox
                )
                # Свой набор идентификаторов на попытку: при откате от
                # фильтра файл читается заново, и объекты не должны
                # считаться дважды.
                local = set()
                counts = _count_features(
                    dataset, definitions, index, bbox_geometry, bbox, cancel,
                    progress, filtered, local,
                )
                if filtered and not counts["delivered"]:
                    # Фильтр драйвера не доставил нужную область: файл
                    # читается заново целиком.
                    continue
                seen.update(local)
                return counts["total"], counts["in_bbox"], counts["matched"]
            except RuntimeError as exc:
                error = str(exc).strip() or type(exc).__name__
                continue
            finally:
                dataset = None
    raise PbfError(
        f"GDAL could not read {Path(source).name}: {error}. "
        "Make sure the OSM/PBF driver is available in this QGIS build."
    )


def _count_features(dataset, definitions, index, bbox_geometry, bbox, cancel,
                    progress, filtered, seen):
    """Счётчики объектов файла и признак, что фильтр отдал область."""
    total = 0
    in_bbox = 0
    matched = 0
    read_box = None
    field_cache = {}
    for feature, layer_name in _iter_features(dataset, cancel):
        total += 1
        if total % 50000 == 0 and progress:
            progress(f"PBF: проверено {total} объектов")
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            continue
        if filtered:
            try:
                read_box = _extend_box(read_box, *geometry.GetEnvelope())
            except Exception:
                pass
        try:
            if not geometry.Intersects(bbox_geometry):
                continue
        except Exception:
            continue
        in_bbox += 1
        kind = _kind_for_layer(layer_name, feature)
        definition = definitions.get(layer_name)
        if definition is None:
            continue
        properties = _feature_properties(
            feature, definition, kind,
            _field_entries(definition, field_cache),
            _internal_indexes(definition, field_cache),
        )
        if properties and index.matching_names(kind, properties):
            identity = (layer_name, properties.get("@id"))
            if identity[1]:
                if identity in seen:
                    continue
                seen.add(identity)
            matched += 1
    area = (bbox.lon_min, bbox.lon_max, bbox.lat_min, bbox.lat_max)
    return {
        "total": total,
        "in_bbox": in_bbox,
        "matched": matched,
        "delivered": _filter_delivered(read_box, area) if filtered else True,
    }


def probe_dataset(config, bbox, query_names, query_texts=None, cancel=None,
                  progress=None, sources=None):
    gdal, ogr = _gdal()
    _require_dataset_reading()
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
        try:
            counts = _probe_source(
                gdal, source, config.temp_dir, index, bbox_geometry, bbox,
                cancel, progress, seen,
            )
        finally:
            for path in temporary:
                Path(path).unlink(missing_ok=True)
        total += counts[0]
        in_bbox += counts[1]
        matched += counts[2]

    return PbfProbe(total, in_bbox, matched, time.monotonic() - started)
