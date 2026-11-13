"""Служебный слой POI-значков для подавления наложений между слоями.

QGIS не разводит значки разных слоёв: кластерный рендерер группирует
точки только внутри одного слоя, а препятствия для подписей берутся
только из провайдеров подписей.  Поэтому значок подавляется фильтром
правила рендерера: он скрывается, если рядом есть более приоритетная
точка — или точка того же слоя, стоящая ближе к началу координат,
иначе значки одного слоя (населённые пункты, хижины) накладывались бы
друг на друга.

Приоритет — порядок слоёв в дереве проекта: верхний в списке рисуется
поверх и побеждает.  Чтобы не сравнивать каждый слой с каждым, все
POI-точки собираются в служебные слои ``poi icons`` с полем ``rank``
(1 — самый приоритетный).  Правило слоя с рангом R скрывает точку,
когда рядом есть точка с меньшим рангом или с рангом R, но с
координатами меньшими, чем у текущей.  ``overlay_nearest`` не даёт
доступа к геометрии текущего объекта (``geometry(@parent)`` в его
аргументах — NULL), поэтому родитель передаётся через
``with_variable('pg', $geometry, ...)``, а кандидаты возвращаются
массивами координат и ранга.

Сосед должен быть виден в текущем масштабе и уровне детализации, иначе
значок исчезал бы из-за объекта, который не рисуется.  Видимость
считается по правилам шаблона, а служебный слой свой для каждого
диапазона масштаба: у диапазона свой набор видимых точек, и правило
ссылается на слой своего диапазона.

Служебный слой один: все POI-слои шаблона — в EPSG:4326, и
overlay_nearest считает расстояния в градусах.  Слой в другой
проекции сломал бы сравнение с max_distance (метры против градусов),
поэтому рецепты не должны перепроецировать слои со значками.

Точки, которые не могут подавить ни одного соседа, в служебный слой не
попадают: overlay_nearest перебирает весь слой целиком, поэтому размер
слоя прямо складывается в время отрисовки.

Текущий диапазон масштаба выбирается снаружи вызова overlay_nearest:
внутри его аргументов выражения считаются в контексте служебного
слоя, где @map_scale не видно.  Уровень детализации, наоборот, доступен
в подвыражении как переменная проекта — порог видимости хранится в
поле ``lod`` и сравнивается на лету, поэтому смена детализации в панели
не требует пересборки данных.

Расстояние подавления — размер значка правила в миллиметрах, переведённый
в единицы слоя через @map_scale.  Все POI-слои шаблона — в EPSG:4326:
добавлен множитель cos(широты), потому что градус долготы короче градуса
широты, а QGIS сравнивает расстояния плоско в единицах слоя.  Слои в
другой проекции не поддерживаются — расстояния перестанут совпадать с
max_distance.

В служебный слой попадают и значки, нарисованные на объектах линий и
полигонов: MarkerLine с CentralPoint (мосты) и CentroidFill (кладбища).
Фильтр правила скрыл бы весь объект вместе со значком, поэтому такие
значки не вытесняются сами, а участвуют как препятствия с рангом выше
любого POI.  На объект нужна одна точка — центр линии или площадки,
так что ни подготовка, ни отрисовка не замедляются.
"""

import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .resources import RESOURCES_DIR, template_path

AUX_LAYER_NAME = "poi icons"
AUX_FILE_NAME = "poi_icons.gpkg"
# Служебный файл прежней схемы с двумя системами координат; удаляется
# при сборке, чтобы не оставался в каталоге проекта.
LEGACY_AUX_FILE_NAME_3857 = "poi_icons_3857.gpkg"

# Видимая часть холста SVG по имени файла: считается один раз за сеанс.
_SVG_SCALE_CACHE = {}

LOD_LEVELS = (3, 4, 5, 6)
# Ранг, который никого не подавляет: подставляется в подвыражении для
# точек, скрытых текущим уровнем детализации.
NEVER_RANK = 99999

# Ранг значка-препятствия на линии/полигоне. Меньше 1 — выше любого POI;
# шаг делает ранги уникальными для _prune_band.
OBSTACLE_RANK_STEP = 0.01

# Слои, у которых значок моста рисуется на линии и вытесняется внутри
# своего слоя через поле icon_hide.
BRIDGE_ICON_LAYERS = (
    "highway main bridge", "track bridge", "path bridge", "railway bridge",
)


@dataclass
class PoiRule:
    """Правило отрисовки: фильтр, диапазон масштаба, размер значка."""

    key: str = ""
    filter: str = ""
    minimum: float = 0.0  # нижняя граница знаменателя, включительно
    maximum: float = 0.0  # верхняя граница знаменателя, исключительно
    size: float = 0.0
    children: list = field(default_factory=list)

    @property
    def is_group(self):
        return bool(self.children)


@dataclass
class PoiLayer:
    """Слой со значками: ранг по дереву, источник данных, правила."""

    name: str
    rank: float = 0
    source: str = ""
    crs: str = "EPSG:4326"
    root: PoiRule = field(default_factory=PoiRule)
    minimum: float = 0.0  # границы знаменателя по видимости слоя
    maximum: float = 0.0
    labels: list = field(default_factory=list)
    # Point — обычный слой значков; Line/Polygon — значок рисуется
    # на объекте линии или полигона (мост, кладбище).
    geometry: str = "Point"

    @property
    def icon_size(self):
        """Размер значка слоя: максимальный среди листовых правил."""
        sizes = [leaf.size for leaf in leaves(self.root)]
        return max(sizes) if sizes else 0.0


@dataclass
class PoiLabelRule:
    """Правило подписи: фильтр и диапазон масштаба."""

    key: str = ""
    filter: str = ""
    minimum: float = 0.0
    maximum: float = 0.0


@dataclass
class _Point:
    """Точка служебного слоя: координаты, ранг и диапазоны видимости."""

    rank: float
    lon: float
    lat: float
    name: str = ""
    fid: int = -1
    geometry: str = "Point"
    hidden: set = field(default_factory=set)
    bands: dict = field(default_factory=dict)  # диапазон -> минимальный LOD
    # диапазон -> размер значка в мм (радиус, в котором он вытесняет)
    sizes: dict = field(default_factory=dict)


def _scale_bounds(element):
    """Границы знаменателя масштаба правила: (нижняя, верхняя).

    В XML scalemindenom — нижняя граница (включительно), scalemaxdenom —
    верхняя (исключительно); 0 означает отсутствие ограничения.
    """
    lower = float(element.get("scalemindenom") or 0)
    upper = float(element.get("scalemaxdenom") or 0)
    # scalemindenom=1 в шаблоне означает «без нижней границы», а не
    # настоящий масштаб 1:1 — иначе появляется бесполезный диапазон 0–1.
    if lower <= 1.0:
        lower = 0.0
    return lower, upper


def _svg_content_scale(name):
    """Доля холста SVG, занятая видимым рисунком (1.0 — на весь холст).

    QGIS рисует SVG в квадрате размера символа, но у части значков
    вокруг фигуры прозрачные поля: треугольник пика занимает ~72 %
    холста, кружок станции ~78 %.  Вытеснение должно опираться на
    видимую часть, иначе значок «занимает» пустые углы и скрывает
    соседей, которым там хватает места.
    """
    key = Path(name or "").name
    cached = _SVG_SCALE_CACHE.get(key)
    if cached is not None:
        return cached
    scale = 1.0
    path = RESOURCES_DIR / "icons" / key
    if key and path.exists():
        rendered = _rendered_content_scale(path)
        if rendered:
            scale = rendered
    _SVG_SCALE_CACHE[key] = scale
    return scale


def _rendered_content_scale(path, side=64):
    """Отношение непрозрачного bbox к холсту после рендера SVG."""
    try:
        from qgis.PyQt.QtGui import QImage, QPainter
        from qgis.PyQt.QtSvg import QSvgRenderer
    except ImportError:
        return None
    image = QImage(side, side, QImage.Format_ARGB32)
    image.fill(0)
    renderer = QSvgRenderer(str(path))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    bounds = _alpha_bounds(image)
    if bounds is None:
        return None
    width, height = bounds
    return max(width, height) / side


def _alpha_bounds(image):
    """Габариты непрозрачных пикселей картинки: (ширина, высота)."""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    try:
        bits = image.constBits()
        bits.setsize(image.byteCount())
        raw = bytes(bits)
    except Exception:  # noqa: BLE001 - другая версия PyQt
        raw = None
    min_x, min_y, max_x, max_y = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            if raw is not None:
                alpha = raw[y * stride + x * 4 + 3]
            else:
                alpha = image.pixelColor(x, y).alpha()
            if alpha <= 16:
                continue
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
    if max_x < 0:
        return None
    return max_x - min_x + 1, max_y - min_y + 1


def _marker_size(symbol_element):
    """Размер значка в миллиметрах из элемента symbol шаблона.

    Для SVG учитывается только видимая часть холста: прозрачные поля
    вокруг рисунка не должны участвовать в вытеснении соседей.
    """
    if symbol_element is None:
        return 0.0
    for layer in symbol_element.findall("layer"):
        if layer.get("class") not in ("SimpleMarker", "SvgMarker"):
            continue
        options = {
            option.get("name"): option.get("value")
            for option in layer.findall("Option/Option")
        }
        try:
            size = float(options.get("size") or 0)
        except ValueError:
            return 0.0
        if layer.get("class") == "SvgMarker":
            size *= _svg_content_scale(options.get("name") or "")
        return size
    return 0.0


def _layer_crs(element):
    """EPSG-код слоя из шаблона."""
    spatial = element.find("srs/spatialrefsys")
    if spatial is None:
        return "EPSG:4326"
    authid = spatial.findtext("authid") or spatial.get("authid")
    return authid or "EPSG:4326"


def _renderer_element(element):
    """Внутренний рендерер слоя: у кластера он вложен в внешний."""
    outer = element.find("renderer-v2")
    if outer is None:
        return None
    inner = outer.find("renderer-v2")
    return inner if inner is not None else outer


def _symbol_sizes(renderer):
    """Размеры значков символов правил по их именам."""
    sizes = {}
    symbols = renderer.find("symbols")
    if symbols is None:
        return sizes
    for symbol in symbols.findall("symbol"):
        sizes[symbol.get("name")] = _marker_size(symbol)
    return sizes


def _parse_rules(rules_element, sizes, minimum, maximum):
    """Дерево правил слоя с абсолютными границами масштаба.

    Границы родителя наследуются детьми: правило без своих границ
    видно в том же диапазоне, что и ветка, в которой оно лежит.
    """
    def parse(node, inherited_min, inherited_max):
        lower, upper = _scale_bounds(node)
        effective_min = max(inherited_min, lower)
        if inherited_max and upper:
            effective_max = min(inherited_max, upper)
        else:
            effective_max = inherited_max or upper
        rule = PoiRule(
            key=node.get("key") or "",
            filter=node.get("filter") or "",
            minimum=effective_min,
            maximum=effective_max,
            size=sizes.get(node.get("symbol"), 0.0),
        )
        for child in node.findall("rule"):
            if child.get("checkstate") == "0":
                continue
            rule.children.append(parse(child, effective_min, effective_max))
        return rule

    root = PoiRule(minimum=minimum, maximum=maximum)
    for node in rules_element.findall("rule"):
        # checkstate="0" — правило выключено в шаблоне: QGIS его
        # не рисует, поэтому и в расчёте видимости оно не участвует.
        if node.get("checkstate") == "0":
            continue
        root.children.append(parse(node, minimum, maximum))
    return root


def _label_rules(element):
    """Правила подписей слоя: фильтр и диапазон масштаба."""
    if element.get("labelsEnabled") != "1":
        return []
    labeling = element.find("labeling")
    if labeling is None or labeling.get("type") != "rule-based":
        return []
    rules_element = labeling.find("rules")
    if rules_element is None:
        return []
    result = []

    def walk(node):
        children = node.findall("rule")
        if children:
            for child in children:
                walk(child)
            return
        if node.get("checkstate") == "0":
            return
        minimum, maximum = _scale_bounds(node)
        result.append(PoiLabelRule(
            key=node.get("key") or "",
            filter=node.get("filter") or "",
            minimum=minimum,
            maximum=maximum,
        ))

    for node in rules_element.findall("rule"):
        walk(node)
    return result


def read_template_poi(path=None):
    """Слои шаблона со значками: ранг по дереву, источник, правила.

    Порядок рангов — порядок следования слоёв в дереве проекта: чем выше
    слой в списке, тем он приоритетнее.  В список попадают только слои,
    которые действительно рисуют значки (маркер с ненулевым размером).
    Значки на линиях и полигонах читает :func:`read_template_obstacles`.
    """
    path = Path(path or template_path())
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".qgs"))
        root = ElementTree.fromstring(archive.read(name))

    order = [
        element.get("name")
        for element in root.find("layer-tree-group").iter()
        if element.tag == "layer-tree-layer" and element.get("name")
    ]

    layers = []
    for element in root.iter("maplayer"):
        name = element.findtext("layername")
        if not name or name.startswith(AUX_LAYER_NAME):
            continue
        renderer = _renderer_element(element)
        if renderer is None:
            continue
        sizes = _symbol_sizes(renderer)
        rules_element = renderer.find("rules")
        minimum = maximum = 0.0
        if element.get("hasScaleBasedVisibilityFlag") == "1":
            # minScale — верхняя граница знаменателя (до какого масштаба
            # слой виден), maxScale — нижняя.
            maximum = float(element.get("minScale") or 0)
            minimum = float(element.get("maxScale") or 0)
            if minimum <= 1.0:
                minimum = 0.0
        if rules_element is not None:
            tree = _parse_rules(rules_element, sizes, minimum, maximum)
        else:
            symbol = renderer.find("symbol")
            if symbol is None:
                symbol = renderer.find("symbols/symbol")
            size = _marker_size(symbol)
            if size <= 0:
                continue
            leaf = PoiRule(size=size, minimum=minimum, maximum=maximum)
            tree = PoiRule(
                minimum=minimum, maximum=maximum, children=[leaf],
            )
        if not _has_icon(tree):
            continue
        layers.append(PoiLayer(
            name=name,
            rank=0,
            source=element.findtext("datasource") or "",
            crs=_layer_crs(element),
            root=tree,
            minimum=minimum,
            maximum=maximum,
            labels=_label_rules(element),
        ))
    layers.sort(key=lambda item: order.index(item.name) if item.name in order else 10**6)
    for index, item in enumerate(layers, start=1):
        item.rank = index
    return layers


def _has_icon(tree):
    """Есть ли в дереве правил хотя бы один значок."""
    if tree.size > 0:
        return True
    return any(_has_icon(child) for child in tree.children)


def _nested_marker_size(symbol_element):
    """Размер значка внутри MarkerLine или CentroidFill.

    ``_marker_size`` смотрит только на непосредственные слои символа: у
    линии значок вложен в MarkerLine, у полигона — в CentroidFill.
    Берём самый крупный значок, чтобы препятствие не оказалось меньше
    нарисованного.
    """
    if symbol_element is None:
        return 0.0
    best = 0.0
    for layer in symbol_element.iter("layer"):
        if layer.get("class") not in ("SimpleMarker", "SvgMarker"):
            continue
        options = {
            option.get("name"): option.get("value")
            for option in layer.findall("Option/Option")
        }
        try:
            size = float(options.get("size") or 0)
        except ValueError:
            continue
        if layer.get("class") == "SvgMarker":
            size *= _svg_content_scale(options.get("name") or "")
        best = max(best, size)
    return best


def _obstacle_symbol_sizes(renderer):
    """Размеры вложенных значков символов правил по именам."""
    sizes = {}
    symbols = renderer.find("symbols")
    if symbols is None:
        return sizes
    for symbol in symbols.findall("symbol"):
        sizes[symbol.get("name")] = _nested_marker_size(symbol)
    return sizes


def _marker_placements(element):
    """Значения placements у слоёв MarkerLine."""
    placements = []
    for layer in element.iter("layer"):
        if layer.get("class") != "MarkerLine":
            continue
        for option in layer.findall("Option/Option"):
            if option.get("name") == "placements":
                placements.append(option.get("value") or "")
    return placements


def _obstacle_geometry(element):
    """Тип значка-препятствия: Line, Polygon или None.

    Берём только значки, которые рисуются в одной точке объекта:
    MarkerLine с CentralPoint (мосты) и CentroidFill (кладбища).  Ленты
    стрелок вдоль рек и линии электропередачи не обрабатываются: у них
    значков вдоль линии много, и одна точка в середине соврала бы.
    """
    geometry = element.get("geometry") or ""
    if geometry == "Line":
        return "Line" if "CentralPoint" in _marker_placements(element) else None
    if geometry == "Polygon":
        return (
            "Polygon"
            if element.find(".//layer[@class='CentroidFill']") is not None
            else None
        )
    return None


def read_template_obstacles(path=None):
    """Значки на линиях и полигонах: мосты, кладбища.

    QGIS не умеет скрыть отдельный маркер MarkerLine/CentroidFill:
    фильтр правила действует на объект целиком, и вместе со значком
    пропала бы вся линия или полигон.  Поэтому такие значки не
    вытесняются сами, а участвуют как препятствия: центр линии или
    площадки добавляется в служебный слой с рангом выше любого POI, и
    обычные значки рядом с ними скрываются.  На объект нужна одна
    точка, поэтому это не замедляет ни подготовку, ни отрисовку.
    """
    path = Path(path or template_path())
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".qgs"))
        root = ElementTree.fromstring(archive.read(name))

    obstacles = []
    for element in root.iter("maplayer"):
        name = element.findtext("layername")
        if not name or name.startswith(AUX_LAYER_NAME):
            continue
        kind = _obstacle_geometry(element)
        if kind is None:
            continue
        renderer = _renderer_element(element)
        if renderer is None:
            continue
        sizes = _obstacle_symbol_sizes(renderer)
        rules_element = renderer.find("rules")
        minimum = maximum = 0.0
        if element.get("hasScaleBasedVisibilityFlag") == "1":
            maximum = float(element.get("minScale") or 0)
            minimum = float(element.get("maxScale") or 0)
            if minimum <= 1.0:
                minimum = 0.0
        if rules_element is not None:
            tree = _parse_rules(rules_element, sizes, minimum, maximum)
        else:
            symbol = renderer.find("symbol")
            if symbol is None:
                symbol = renderer.find("symbols/symbol")
            size = _nested_marker_size(symbol)
            if size <= 0:
                continue
            leaf = PoiRule(size=size, minimum=minimum, maximum=maximum)
            tree = PoiRule(
                minimum=minimum, maximum=maximum, children=[leaf],
            )
        if not _has_icon(tree):
            continue
        obstacles.append(PoiLayer(
            name=name,
            source=element.findtext("datasource") or "",
            crs=_layer_crs(element),
            root=tree,
            minimum=minimum,
            maximum=maximum,
            geometry=kind,
        ))
    # Ранг меньше 1: препятствие вытесняет любой POI, но остаются
    # уникальными, чтобы _prune_band не путал их с точками того же слоя.
    for index, layer in enumerate(obstacles, start=1):
        layer.rank = index * OBSTACLE_RANK_STEP
    return obstacles


def _icon_anchor(geometry, kind):
    """Точка, где нарисован значок линии или полигона."""
    if geometry is None or geometry.isEmpty():
        return None
    if kind == "Line":
        point = geometry.interpolate(geometry.length() / 2.0)
    elif kind == "Polygon":
        point = geometry.pointOnSurface()
    else:
        point = geometry
    if point is None or point.isEmpty():
        return None
    return point.asPoint()


def leaves(tree):
    """Листовые правила дерева — те, что рисуют значок."""
    if not tree.children:
        return [tree] if tree.size > 0 else []
    result = []
    for child in tree.children:
        result.extend(leaves(child))
    return result


def canonical_bands(layers):
    """Канонические диапазоны масштаба: список (нижняя, верхняя).

    Строятся по границам правил POI-слоёв, правил подписей и по
    границам видимости самих слоёв, чтобы видимость соседа совпадала с
    его собственными правилами.  0 в верхней границе последнего
    диапазона означает «без ограничения».
    """
    bounds = set()
    for layer in layers:
        bounds.add(layer.minimum)
        bounds.add(layer.maximum)
        for leaf in leaves(layer.root):
            bounds.add(leaf.minimum)
            bounds.add(leaf.maximum)
        for label in layer.labels:
            bounds.add(label.minimum)
            bounds.add(label.maximum)
    bounds.discard(0.0)
    ordered = sorted(bounds)
    bands = []
    previous = 0.0
    for value in ordered:
        bands.append((previous, float(value)))
        previous = float(value)
    bands.append((previous, 0.0))
    return bands


def band_indexes(minimum, maximum, bands):
    """Индексы канонических диапазонов, которые покрывает диапазон."""
    indexes = []
    for index, (lower, upper) in enumerate(bands):
        if minimum and lower < minimum:
            continue
        if maximum and (upper == 0 or upper > maximum):
            continue
        indexes.append(index)
    return indexes


def aux_layer_name(index):
    """Имя служебного слоя диапазона масштаба."""
    return f"{AUX_LAYER_NAME} {index}"


def aux_table_name(index):
    """Имя таблицы служебного слоя внутри GeoPackage."""
    return f"band{index}"


def suppression_expression(layer, leaf, bands):
    """Условие «рядом есть более приоритетный видимый значок».

    Истинно, когда точку надо скрыть.  Текущий диапазон масштаба
    выбирается снаружи: @map_scale в аргументах overlay_nearest
    недоступен, доступен он только в фильтре правила.  Для каждого
    диапазона, который покрывает правило, выбираются соседи из
    служебного слоя этого диапазона.

    array_min по рангам соседей отвечает на вопрос «есть ли рядом более
    приоритетный объект», а coalesce подставляет наш ранг, когда соседей
    нет, — точка остаётся.
    """
    indexes = band_indexes(leaf.minimum, leaf.maximum, bands)
    if not indexes:
        return "false"
    rank = layer.rank
    terms = []
    for index in indexes:
        lower, upper = bands[index]
        scale = []
        if lower:
            scale.append(f"@map_scale >= {lower:g}")
        if upper:
            scale.append(f"@map_scale < {upper:g}")
        term = _nearest_expression(leaf.size, index, rank)
        if scale:
            term = "(" + " AND ".join(scale) + f") AND {term}"
        terms.append(term)
    if len(terms) == 1:
        return terms[0]
    return "(" + " OR ".join(terms) + ")"


def _nearest_expression(size, index, rank):
    """Есть ли рядом видимый значок, который эту точку вытесняет.

    overlay_nearest не даёт доступа к геометрии текущего объекта:
    ``geometry(@parent)`` внутри его аргументов равно NULL.  Поэтому
    родительская точка передаётся через ``with_variable('pg', ...)``,
    а кандидаты возвращаются массивами координат и ранга.  Правило
    приоритета и точное расстояние в метрах считаются снаружи вызова,
    где ``@pg`` и ``@element`` доступны.

    В выборку попадают более приоритетные объекты (меньший ранг) и
    точки того же слоя, стоящие «раньше» по координатам — иначе значки
    одного слоя (населённые пункты, хижины) накладывались бы друг на
    друга.  Расстояние сравнивается с размером видимой части значка:
    max_distance у overlay_nearest — только градусный конверт для
    пространственного индекса, метрическая проверка отсекает пустые
    углы конверта.
    """
    envelope = (
        f"{size:g} * @map_scale / 1000 / 111320 / "
        f"cos(radians(y($geometry)))"
    )
    metric = (
        f"sqrt(((@element[0] - x(@pg)) * 111320 * "
        f"cos(radians(y(@pg)))) ^ 2 + "
        f"((@element[1] - y(@pg)) * 111320) ^ 2)"
    )
    ahead = (
        f"(@element[2] < {rank} OR (@element[2] = {rank} AND "
        f"(@element[0] < x(@pg) OR "
        f"(@element[0] = x(@pg) AND @element[1] < y(@pg)))))"
    )
    return (
        f"with_variable('pg', $geometry, try(array_length(array_filter("
        f"overlay_nearest('{aux_layer_name(index)}', "
        f"expression:=array(x($geometry), y($geometry), "
        f"if(\"lod\" <= @level_of_detail, \"rank\", {NEVER_RANK})), "
        f"limit:=-1, max_distance:={envelope}), "
        f"{ahead} AND {metric} < {size:g} * @map_scale / 1000 "
        f"AND {metric} > 0.001)) > 0, false))"
    )


def icon_enabled_expression(index):
    """Видимость значка моста в одном диапазоне масштаба.

    Значок рисуется MarkerLine в середине линии, поэтому фильтр правила
    скрыл бы весь мост.  Поле ``icon_hide`` заполняется при подготовке
    данных: в нём перечислены диапазоны масштаба, где у объекта есть
    сосед того же слоя с меньшими координатами.  Правило значка своё
    для каждого диапазона, поэтому ``@map_scale`` (в data-defined
    свойствах символа он недоступен) не нужен.  Свойство ``enabled`` у
    MarkerLine отключает только значок, линии остаются.
    """
    return (
        "if(coalesce(array_contains(string_to_array("
        f"\"icon_hide\", ';'), '{index}'), false), false, true)"
    )


def extend_filter(filter_text, layer, leaf, bands):
    """Добавляет к фильтру правила подавление значка."""
    suppression = suppression_expression(layer, leaf, bands)
    if suppression == "false":
        return filter_text
    condition = f"NOT ({suppression})"
    if filter_text:
        return f"({filter_text}) AND {condition}"
    return condition


def layer_suppression_expression(layer, bands, minimum=None, maximum=None):
    """Подавление по правилам значка слоя.

    Подпись скрывается вместе со значком: условие собирается из листовых
    правил слоя, а текущий диапазон масштаба выбирается снаружи, где
    @map_scale доступен.  Для правила подписи берутся только те правила
    значка, чей диапазон масштаба пересекается с диапазоном подписи:
    остальные всё равно неактивны на этом масштабе, а условие от них
    только удлиняет выражение.
    """
    expressions = []
    for leaf in leaves(layer.root):
        if minimum is not None or maximum is not None:
            if minimum and leaf.maximum and leaf.maximum <= minimum:
                continue
            if maximum and leaf.minimum >= maximum:
                continue
        expression = suppression_expression(layer, leaf, bands)
        if expression != "false" and expression not in expressions:
            expressions.append(expression)
    if not expressions:
        return "false"
    if len(expressions) == 1:
        return expressions[0]
    return "(" + " OR ".join(expressions) + ")"


def extend_label_filter(filter_text, layer, bands, minimum=None, maximum=None):
    """Скрывает подпись вместе со значком того же объекта."""
    suppression = layer_suppression_expression(layer, bands, minimum, maximum)
    if suppression == "false":
        return filter_text
    condition = f"NOT ({suppression})"
    if filter_text:
        return f"({filter_text}) AND {condition}"
    return condition


def restore_base_filters(layers, bands):
    """Убирает автоматическое подавление из прочитанных правил шаблона.

    При сборке служебного слоя нужны только исходные условия видимости.
    Иначе правила ссылались бы на ещё не построенный служебный слой.
    Условие формируется тем же кодом, что и при записи шаблона, поэтому
    его можно удалить точным совпадением суффикса, без разбора скобок.
    """
    for layer in layers:
        for leaf in leaves(layer.root):
            suppression = suppression_expression(layer, leaf, bands)
            if suppression == "false":
                continue
            condition = f"NOT ({suppression})"
            if leaf.filter == condition:
                leaf.filter = ""
                continue
            suffix = f") AND {condition}"
            if leaf.filter.startswith("(") and leaf.filter.endswith(suffix):
                leaf.filter = leaf.filter[1:-len(suffix)]


def template_variables(path=None):
    """Переменные проекта из шаблона (place_density и прочие)."""
    path = Path(path or template_path())
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".qgs"))
        text = archive.read(name).decode("utf-8")
    names = re.search(r'<variableNames type="QStringList">(.*?)</variableNames>', text, re.S)
    values = re.search(r'<variableValues type="QStringList">(.*?)</variableValues>', text, re.S)
    variables = {}
    if names and values:
        keys = re.findall(r"<value>(.*?)</value>", names.group(1))
        vals = re.findall(r"<value>(.*?)</value>", values.group(1))
        for key, value in zip(keys, vals):
            try:
                variables[key] = int(value)
            except ValueError:
                variables[key] = value
    return variables


def _source_path(config, source_text):
    """Файл данных слоя шаблона в каталоге проекта."""
    relative = (source_text or "").split("|", 1)[0]
    if not relative:
        return None
    path = Path(relative)
    if not path.is_absolute():
        path = Path(config.project_dir) / relative
    return path if path.exists() else None


def _open_source(config, source_text, name):
    """Открывает локальный источник шаблона с его параметрами URI."""
    from qgis.core import QgsVectorLayer

    path = _source_path(config, source_text)
    if path is None:
        return None
    source = str(path)
    if "|" in source_text:
        source += "|" + source_text.split("|", 1)[1]
    layer = QgsVectorLayer(source, name, "ogr")
    return layer if layer.isValid() else None


def _overlay_sources(layers):
    """Источники не-POI слоёв, упомянутых в исходных фильтрах."""
    names = set()
    poi_names = {layer.name for layer in layers}
    for layer in layers:
        for leaf in leaves(layer.root):
            names.update(re.findall(
                r"overlay_\w+\(\s*'([^']+)'", leaf.filter,
            ))
    names -= poi_names
    names = {
        name for name in names
        if not name.startswith(AUX_LAYER_NAME)
    }
    if not names:
        return {}
    with zipfile.ZipFile(template_path()) as archive:
        xml_name = next(
            name for name in archive.namelist() if name.endswith(".qgs")
        )
        root = ElementTree.fromstring(archive.read(xml_name))
    sources = {}
    for element in root.iter("maplayer"):
        name = element.findtext("layername")
        if name in names:
            sources[name] = element.findtext("datasource") or ""
    return sources


def _evaluation_context(project, layer, level):
    """Контекст выражения правила для данного уровня детализации."""
    from qgis.core import (
        QgsExpressionContext, QgsExpressionContextScope,
        QgsExpressionContextUtils,
    )

    context = QgsExpressionContext()
    context.appendScope(QgsExpressionContextUtils.globalScope())
    context.appendScope(QgsExpressionContextUtils.projectScope(project))
    context.appendScope(QgsExpressionContextUtils.layerScope(layer))
    # Переменная проекта переопределяется отдельным скопом: у
    # QgsExpressionContext нет setVariable, а LOD надо менять при
    # вычислении видимости точки.
    scope = QgsExpressionContextScope("poi")
    scope.setVariable("level_of_detail", level)
    context.appendScope(scope)
    return context


class _RuleEvaluator:
    """Вычисление видимости точки по дереву правил слоя.

    Повторяет семантику QGIS: правило подходит, если его фильтр истинен
    и масштаб попадает в диапазон; ветка ELSE срабатывает, только если
    ни один предыдущий сосед не подошёл.
    """

    def __init__(self, project, layer):
        self._expressions = {}
        self._contexts = {
            level: _evaluation_context(project, layer, level)
            for level in LOD_LEVELS
        }

    def _expression(self, text):
        from qgis.core import QgsExpression

        if text not in self._expressions:
            if not text or text.strip().upper() == "ELSE":
                self._expressions[text] = None
            else:
                self._expressions[text] = QgsExpression(text)
        return self._expressions[text]

    def _matches(self, rule, feature, level):
        expression = self._expression(rule.filter)
        if expression is None:
            return True
        context = self._contexts[level]
        context.setFeature(feature)
        value = expression.evaluate(context)
        if expression.hasEvalError():
            # QGIS правило с невычислимым фильтром не применяет.
            return False
        return bool(value)

    def visible(self, tree, feature, level, scale):
        """Видна ли точка при текущем уровне детализации и масштабе."""
        return self.icon_size(tree, feature, level, scale) > 0

    def icon_size(self, tree, feature, level, scale):
        """Размер значка подходящего листа (0 — правило не рисуется)."""
        return self._descend(tree, feature, level, scale) or 0.0

    def _descend(self, rule, feature, level, scale):
        if rule.children:
            matched = False
            best = 0.0
            for child in rule.children:
                if child.filter.strip().upper() == "ELSE":
                    allowed = not matched
                else:
                    allowed = self._matches(child, feature, level)
                    if allowed:
                        matched = True
                if allowed:
                    size = self._descend(child, feature, level, scale)
                    if size > best:
                        best = size
            return best
        if rule.size <= 0:
            return 0.0
        # Диапазон масштаба листа уже включает ограничения веток и слоя.
        if rule.minimum and scale < rule.minimum:
            return 0.0
        if rule.maximum and scale > rule.maximum:
            return 0.0
        return rule.size


def _band_scale(band):
    """Представительный масштаб канонического диапазона."""
    lower, upper = band
    if lower:
        if upper:
            return (lower + upper) / 2.0
        return lower * 1.5
    if upper:
        return upper / 2.0
    return 25000.0


def _uses_detail_level(tree):
    """Зависит ли видимость от уровня детализации."""
    if "@level_of_detail" in tree.filter:
        return True
    return any(_uses_detail_level(child) for child in tree.children)


def _visible_bands(evaluator, tree, feature, bands, detailed):
    """Диапазоны, в которых точка видна, её LOD и размер значка.

    Возвращает список (индекс диапазона, минимальный LOD, размер значка).
    """
    result = []
    for index, band in enumerate(bands):
        scale = _band_scale(band)
        if detailed:
            level = None
            for candidate in LOD_LEVELS:
                size = evaluator.icon_size(tree, feature, candidate, scale)
                if size > 0:
                    level = candidate
                    break
        else:
            # Без зависимости от детализации достаточно одного уровня.
            size = evaluator.icon_size(tree, feature, LOD_LEVELS[0], scale)
            level = LOD_LEVELS[0] if size > 0 else None
        if level is not None:
            result.append((index, level, size))
    return result


def _collect_points(config, layers, bands, cancel=None):
    """Точки POI-слоёв с диапазонами, в которых они видны.

    Временный проект содержит также не-POI слои из исходных фильтров:
    без них overlay_within не смог бы учесть реальную видимость соседа.
    """
    from qgis.core import QgsProject

    project = QgsProject()
    project.setCustomVariables(template_variables())
    for name, source in _overlay_sources(layers).items():
        layer = _open_source(config, source, name)
        if layer is not None:
            project.addMapLayer(layer)
    points = []
    for info in layers:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        layer = _open_source(config, info.source, info.name)
        if layer is None:
            continue
        project.addMapLayer(layer)
        evaluator = _RuleEvaluator(project, layer)
        detailed = _uses_detail_level(info.root)
        for feature in layer.getFeatures():
            visible = _visible_bands(
                evaluator, info.root, feature, bands, detailed,
            )
            if not visible:
                continue
            if info.geometry == "Point":
                point = feature.geometry().asPoint()
                lon, lat = point.x(), point.y()
            else:
                anchor = _icon_anchor(feature.geometry(), info.geometry)
                if anchor is None:
                    continue
                lon, lat = anchor.x(), anchor.y()
            points.append(_Point(
                rank=info.rank,
                lon=lon,
                lat=lat,
                name=info.name,
                fid=feature.id(),
                geometry=info.geometry,
                bands={index: level for index, level, _size in visible},
                sizes={index: size for index, _level, size in visible},
            ))
    return points


def _prune_band(points, index, scale, latitude_max):
    """Оставляет точки, которые могут подавить кого-то в диапазоне.

    Точка без соседей рядом подавлять нечего: в служебном слое она
    только замедляет выборку.  Подавить можно объект более низкого
    приоритета (больший ранг) или соседа того же слоя, стоящего
    «позже» по координатам: фильтр шаблона сравнивает геометрию
    кандидата с геометрией родителя, поэтому выигрывает тот, кто есть
    в служебном слое.

    ``scale`` — верхняя граница знаменателя диапазона (0 — без
    ограничения).  Радиус берётся по размеру значка того объекта,
    который будет скрыт: в выражении шаблона расстояние сравнивается
    именно с ``size`` его правила.  Прежний общий максимум оставлял в
    слое точки, которые никого не могли подавить.
    """
    entries = [
        point for point in points
        if index in point.bands and index not in point.hidden
    ]
    if not entries:
        return []
    if not scale:
        # Диапазон без верхней границы: расстояние не ограничено.
        return entries

    def limit(point):
        size = point.sizes.get(index, 0.0) if point.sizes else 0.0
        return size * scale / 1000.0

    max_limit = max((limit(point) for point in entries), default=0.0)
    if max_limit <= 0:
        return []
    # Ячейка сетки должна покрывать весь радиус поиска: градус долготы
    # короче градуса широты, и в метрах радиус больше.
    worst = math.cos(math.radians(latitude_max))
    cell = max(max_limit / (111320.0 * max(worst, 0.01)), 1e-6)
    grid = {}
    for position, point in enumerate(entries):
        key = (int(point.lon // cell), int(point.lat // cell))
        grid.setdefault(key, []).append(position)
    keep = []
    for position, point in enumerate(entries):
        gx = int(point.lon // cell)
        gy = int(point.lat // cell)
        useful = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((gx + dx, gy + dy), ()):
                    if other == position:
                        continue
                    neighbour = entries[other]
                    # Расстояние между точками в градусах, переведённое
                    # в метры.  Метрика завышена (cos не учитывается),
                    # поэтому лишние точки в служебном слое не мешают.
                    distance = math.hypot(
                        neighbour.lon - point.lon,
                        neighbour.lat - point.lat,
                    ) * 111320.0
                    if distance > limit(neighbour):
                        continue
                    if neighbour.rank > point.rank:
                        # Более низкий приоритет соседа: эта точка
                        # может его подавить.
                        useful = True
                    elif neighbour.rank == point.rank and (
                        (neighbour.lon, neighbour.lat)
                        > (point.lon, point.lat)
                    ):
                        # Сосед того же слоя, который фильтр скроет
                        # в пользу этой точки.
                        useful = True
                    if useful:
                        break
                if useful:
                    break
            if useful:
                break
        if useful:
            keep.append(point)
    return keep


def _hidden_bridge_bands(points, bands):
    """Диапазоны, где значок моста проигрывает соседу того же слоя.

    Возвращает словарь (имя слоя, fid) -> множество индексов диапазонов.
    Побеждает объект с меньшими координатами, как у точечных POI; радиус
    сравнения — размер значка скрываемого объекта.  Соседи ищутся по
    сетке, чтобы не перебирать все пары на больших выгрузках.
    """
    result = {}
    for index, band in enumerate(bands):
        entries = [
            point for point in points
            if index in point.bands
            and point.name in BRIDGE_ICON_LAYERS
            and point.geometry == "Line"
            and point.fid >= 0
        ]
        if len(entries) < 2:
            continue
        lower, upper = band
        scale = upper if upper else _band_scale(band)

        def limit(point):
            return point.sizes.get(index, 0.0) * scale / 1000.0

        max_limit = max((limit(point) for point in entries), default=0.0)
        if max_limit <= 0:
            continue
        latitude_max = max(abs(point.lat) for point in entries)
        worst = math.cos(math.radians(latitude_max))
        cell = max(max_limit / (111320.0 * max(worst, 0.01)), 1e-6)
        grid = {}
        for position, point in enumerate(entries):
            grid.setdefault(
                (int(point.lon // cell), int(point.lat // cell)), []
            ).append(position)
        for position, point in enumerate(entries):
            gx = int(point.lon // cell)
            gy = int(point.lat // cell)
            found = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in grid.get((gx + dx, gy + dy), ()):
                        if other == position:
                            continue
                        neighbour = entries[other]
                        if neighbour.name != point.name:
                            continue
                        if (neighbour.lon, neighbour.lat) >= (
                                point.lon, point.lat):
                            continue
                        distance = math.hypot(
                            (neighbour.lon - point.lon)
                            * math.cos(math.radians(point.lat)),
                            neighbour.lat - point.lat,
                        ) * 111320.0
                        if distance <= limit(point):
                            result.setdefault(
                                (point.name, point.fid), set()
                            ).add(index)
                            found = True
                            break
                    if found:
                        break
                if found:
                    break
    return result


def _write_icon_hide(config, layers, points, bands):
    """Пишет поле icon_hide в слои мостов.

    Поле хранит через точку с запятой индексы диапазонов, где значок
    скрыт.  Выражение шаблона выбирает нужный индекс по @map_scale.
    """
    hidden = _hidden_bridge_bands(points, bands)
    from osgeo import ogr

    updates = {}
    for (name, fid), indexes in hidden.items():
        updates.setdefault(name, []).append(
            (fid, ";".join(str(index) for index in sorted(indexes)))
        )
    for info in layers:
        if info.name not in BRIDGE_ICON_LAYERS or info.geometry != "Line":
            continue
        path = _source_path(config, info.source)
        if path is None:
            continue
        dataset = ogr.Open(str(path), 1)
        if dataset is None:
            continue
        try:
            layer = dataset.GetLayer(0)
            if layer is None:
                continue
            if layer.GetLayerDefn().GetFieldIndex("icon_hide") < 0:
                layer.CreateField(ogr.FieldDefn("icon_hide", ogr.OFTString))
            else:
                result = dataset.ExecuteSQL(
                    f'UPDATE "{layer.GetName()}" SET "icon_hide" = NULL')
                if result is not None:
                    dataset.ReleaseResultSet(result)
            for fid, value in updates.get(info.name, ()):
                feature = layer.GetFeature(fid)
                if feature is None:
                    continue
                feature.SetField("icon_hide", value)
                layer.SetFeature(feature)
                feature = None
        finally:
            dataset = None
    return hidden


def _write_aux(config, records, crs, file_name):
    """Записывает служебные слои по диапазонам в GeoPackage.

    Формат — GeoPackage: драйвер SQLite не создаёт для слоя индекс
    RTree, и запрос по прямоугольнику читает всю таблицу целиком.
    """
    from osgeo import ogr, osr

    path = Path(config.vector_dir) / file_name
    path.unlink(missing_ok=True)
    target = osr.SpatialReference()
    target.ImportFromEPSG(int(crs.split(":")[1]))
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    try:
        for index, items in sorted(records.items()):
            if not items:
                continue
            layer = dataset.CreateLayer(
                aux_table_name(index), target, ogr.wkbPoint)
            for name, kind in (
                ("rank", ogr.OFTReal), ("lod", ogr.OFTInteger),
                ("layer", ogr.OFTString),
            ):
                layer.CreateField(ogr.FieldDefn(name, kind))
            definition = layer.GetLayerDefn()
            layer.StartTransaction()
            for point in items:
                feature = ogr.Feature(definition)
                feature.SetField("rank", point.rank)
                feature.SetField("lod", point.bands[index])
                feature.SetField("layer", str(point.rank))
                feature.SetGeometry(ogr.CreateGeometryFromWkt(
                    f"POINT ({point.lon:.9f} {point.lat:.9f})"))
                layer.CreateFeature(feature)
                feature = None
            layer.CommitTransaction()
    finally:
        dataset = None


def build_poi_icons(config, progress=None, cancel=None):
    """Собирает служебные слои POI-значков в каталоге проекта.

    Возвращает число записанных записей.  Служебный слой свой для
    каждого диапазона масштаба: в нём только те точки, которые видны в
    этом диапазоне и могут подавить кого-то рядом.
    """
    report = progress or (lambda message: None)
    layers = read_template_poi()
    if not layers:
        return 0
    obstacles = read_template_obstacles()
    all_layers = layers + obstacles
    bands = canonical_bands(layers)
    restore_base_filters(layers, bands)
    points = _collect_points(config, all_layers, bands, cancel=cancel)
    if not points:
        for file_name in (AUX_FILE_NAME, LEGACY_AUX_FILE_NAME_3857):
            (Path(config.vector_dir) / file_name).unlink(missing_ok=True)
        return 0
    hidden = _write_icon_hide(config, obstacles, points, bands)
    for point in points:
        point.hidden = hidden.get((point.name, point.fid), set())
    latitude_max = max(abs(point.lat) for point in points)
    records = {}
    total = 0
    for index, band in enumerate(bands):
        _lower, upper = band
        kept = _prune_band(points, index, upper, latitude_max)
        if not kept:
            continue
        for point in kept:
            records.setdefault(index, []).append(point)
            total += 1
    _write_aux(config, records, "EPSG:4326", AUX_FILE_NAME)
    (Path(config.vector_dir) / LEGACY_AUX_FILE_NAME_3857).unlink(missing_ok=True)
    report(
        f"POI: служебные слои значков, {total} записей "
        f"для {len(layers)} слоёв и {len(obstacles)} препятствий"
    )
    return total
