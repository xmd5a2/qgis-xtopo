"""Подавление наложений значков POI: расчёт порогов при подготовке данных.

QGIS не разводит значки разных слоёв: кластерный рендерер группирует
точки только внутри одного слоя, а препятствия для подписей берутся
только из провайдеров подписей.  Поэтому значок подавляется условием
правила рендерера: он скрывается, если рядом есть более приоритетная
точка — или точка того же слоя, стоящая ближе к началу координат,
иначе значки одного слоя (населённые пункты, хижины) накладывались бы
друг на друга.

Приоритет — порядок слоёв в дереве проекта: верхний в списке рисуется
поверх и побеждает.  Исключение — пик без именных тегов: его ранг
опускается ниже населённых пунктов, чтобы он не скрывал их значки и
подписи.  Раньше правило искало соседей само, выражением
``overlay_nearest`` по служебному слою ``poi icons``; выражение
вызывалось на каждый объект, а радиус поиска рос вместе с масштабом,
и слой ``peak`` на мелком масштабе рисовался десятки секунд.  Теперь
решение принимается при подготовке данных: для каждой точки считается
порог — расстояние до ближайшего подавителя в метрах, — и кладётся в
поле ``hide`` слоя.  Правило читает поле и сравнивает порог с
радиусом подавления (размер значка, приведённый к масштабу), поэтому
отрисовка больше не зависит от числа соседей.

Порог зависит от диапазона масштаба (у диапазона свой набор видимых
точек) и от уровня детализации (сосед должен быть виден, иначе значок
исчезал бы из-за объекта, который не рисуется), поэтому в поле лежат
слоты по всем диапазонам и уровням: слот — номер диапазона × 5 +
(уровень − 2), пустой слот означает «подавителя нет».  Уровень
детализации остаётся переменной проекта, и смена детализации в панели
не требует пересборки данных.

Расстояние подавления — размер значка правила в миллиметрах, переведённый
в метры через @map_scale.  Все POI-слои шаблона — в EPSG:4326: добавлен
множитель cos(широты), потому что градус долготы короче градуса
широты.  Слои в другой проекции не поддерживаются — расстояния перестанут
совпадать с расстоянием до соседа.

Служебные слои ``poi icons`` остались: в них видно, какие точки могут
подавлять, и по ним считается порог.  Они лежат в дереве проекта, но
скрыты от пользователя.

В служебный слой попадают и значки, нарисованные на объектах линий и
полигонов: MarkerLine с CentralPoint (мосты) и CentroidFill (кладбища).
Фильтр правила скрыл бы весь объект вместе со значком, поэтому такие
значки не вытесняются сами, а участвуют как препятствия.  Их ранг —
место слоя в дереве проекта, как у точечных POI.  На объект нужна одна
точка — центр линии или площадки, так что ни подготовка, ни отрисовка
не замедляются.
"""

import io
import heapq
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from .cpu import cpu_workers
from .peak_placement import NAME_TAGS
from .resources import RESOURCES_DIR, template_path

AUX_LAYER_NAME = "poi icons"
AUX_FILE_NAME = "poi_icons.gpkg"
# Служебный файл прежней схемы с двумя системами координат; удаляется
# при сборке, чтобы не оставался в каталоге проекта.
LEGACY_AUX_FILE_NAME_3857 = "poi_icons_3857.gpkg"

# Предельный масштаб для последнего диапазона: выше него значки уже
# не различаются, а пороги считать больше не нужно.
MAX_SUPPRESSION_SCALE = 20_000_000
# Минимальное расстояние между точками, при котором одна вытесняет
# другую.  То же значение, что и в выражении шаблона: совпадающие
# по координатам точки не вытесняют друг друга.
MIN_SUPPRESSION_DISTANCE = 0.001

# Видимая часть холста SVG по имени файла: считается один раз за сеанс.
_SVG_SCALE_CACHE = {}
# Данные шаблона проекта: читаются один раз за сеанс.  Полное XML-дерево
# не хранится — только слои со значками, препятствия и источники.
_TEMPLATE_DATA = {}
# Переменные проекта из шаблона: тоже читаются один раз за сеанс.
_TEMPLATE_VARIABLES = {}

LOD_LEVELS = (2, 3, 4, 5, 6)
# Уровни, начиная с заданного: подавитель с уровнем L попадает в пороги
# всех уровней не ниже L.
_LEVELS_FROM = {
    lod: tuple(level for level in LOD_LEVELS if level >= lod)
    for lod in range(0, max(LOD_LEVELS) + 2)
}
# Соседние ячейки сетки: вокруг точки.
_NEIGHBOUR_OFFSETS = tuple(
    (dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
)
# Ранг, который никого не подавляет: подставляется в подвыражении для
# точек, скрытых текущим уровнем детализации.
NEVER_RANK = 99999

# Слои, у которых значок вытесняется соседом того же слоя через поле
# icon_hide: мосты (значок в середине линии) и кладбища (значок в
# центре площадки).
SELF_ICON_LAYERS = (
    "highway main bridge", "track bridge", "path bridge", "railway bridge",
    "cemetery",
)
# Поле с индексами диапазонов, где значок моста скрыт.
ICON_HIDE_FIELD = "icon_hide"

# Слои, у которых приоритет внутри слоя задаётся данными объекта, а не
# только координатами: у населённых пунктов важнее город, чем посёлок,
# а внутри одного типа — большее население.
SELF_RANKED_LAYERS = ("places main", "places main major")

# Слои населённых пунктов: безымянный пик не должен их подавлять.
SETTLEMENT_LAYERS = ("places main", "places main major")
PEAK_LAYER = "peak"

# Ранг типа населённого пункта: меньше — важнее.  Порядок тот же, что
# в иерархии place в OSM: город, посёлок, деревня, хутор; пригород и
# квартал — части города, отдельное жильё — мельче всех.
PLACE_RANKS = {
    "city": 0,
    "town": 1,
    "village": 2,
    "hamlet": 3,
    "suburb": 4,
    "neighbourhood": 5,
    "isolated_dwelling": 6,
}
# Границы населения для пункта без типа места: тот же порядок, что и в
# шаблоне (ветка «by population»).
POPULATION_RANKS = (
    (100000, 0), (10000, 1), (1000, 2), (100, 3), (0, 6),
)


@dataclass
class PoiRule:
    """Правило отрисовки: фильтр, диапазон масштаба, размер значка."""

    key: str = ""
    filter: str = ""
    symbol: str = ""  # имя символа правила в шаблоне
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
    # Есть ли у объекта именной тег; для пиков без имени ранг понижается.
    named: bool = True
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
    if raw is not None:
        try:
            import numpy
        except ImportError:
            numpy = None
        if numpy is not None:
            try:
                pixels = numpy.frombuffer(raw, dtype="uint8").reshape(height, stride)
                alpha = pixels[:, : width * 4].reshape(height, width, 4)[:, :, 3]
            except ValueError:
                pass
            else:
                rows, columns = numpy.nonzero(alpha > 16)
                if rows.size == 0:
                    return None
                return (
                    int(columns.max() - columns.min() + 1),
                    int(rows.max() - rows.min() + 1),
                )
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
            symbol=node.get("symbol") or "",
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


def _template_data(path=None):
    """Компактные данные шаблона: значки, препятствия и источники.

    XML шаблона занимает десятки мегабайт, а полное дерево — сотни.
    Поэтому шаблон читается потоково: каждый ``maplayer`` разбирается и
    сразу превращается в правила, после чего элемент освобождается.
    В памяти остаются только нужные для подготовки данные.
    """
    path = Path(path or template_path())
    cached = _TEMPLATE_DATA.get(path)
    if cached is not None:
        return cached
    with zipfile.ZipFile(path) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".qgs"))
        payload = archive.read(name)
    order = []
    sources = {}
    layers = []
    obstacles = []
    for _event, element in ElementTree.iterparse(
        io.BytesIO(payload), events=("end",)
    ):
        if element.tag == "layer-tree-layer":
            layer_name = element.get("name")
            if layer_name:
                order.append(layer_name)
            element.clear()
        elif element.tag == "maplayer":
            name = element.findtext("layername")
            if name and not name.startswith(AUX_LAYER_NAME):
                sources[name] = element.findtext("datasource") or ""
                layer = _poi_layer(element, name)
                if layer is not None:
                    layers.append(layer)
                obstacle = _obstacle_layer(element, name)
                if obstacle is not None:
                    obstacles.append(obstacle)
            element.clear()
    positions = {
        name: index for index, name in enumerate(order, start=1)
    }
    layers.sort(key=lambda item: positions.get(item.name, 10**6))
    obstacles.sort(key=lambda item: positions.get(item.name, 10**6))
    for index, item in enumerate(layers, start=1):
        # Ранг — место слоя в дереве проекта: приоритет задаёт порядок
        # слоёв, а не отдельная нумерация только среди POI.
        item.rank = positions.get(item.name, 10**6 + index)
    for index, layer in enumerate(obstacles, start=1):
        layer.rank = positions.get(layer.name, 10**6 + index)
    data = {
        "poi": layers,
        "obstacles": obstacles,
        "sources": sources,
    }
    _TEMPLATE_DATA[path] = data
    return data


def read_template_poi(path=None):
    """Слои шаблона со значками: ранг по дереву, источник, правила.

    Порядок рангов — порядок следования слоёв в дереве проекта: чем выше
    слой в списке, тем он приоритетнее.  В список попадают только слои,
    которые действительно рисуют значки (маркер с ненулевым размером).
    Значки на линиях и полигонах читает :func:`read_template_obstacles`.
    """
    return list(_template_data(path)["poi"])


def _poi_layer(element, name):
    """Слой значков из ``maplayer`` шаблона или None."""
    renderer = _renderer_element(element)
    if renderer is None:
        return None
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
            return None
        leaf = PoiRule(size=size, minimum=minimum, maximum=maximum)
        tree = PoiRule(
            minimum=minimum, maximum=maximum, children=[leaf],
        )
    if not _has_icon(tree):
        return None
    return PoiLayer(
        name=name,
        rank=0,
        source=element.findtext("datasource") or "",
        crs=_layer_crs(element),
        root=tree,
        minimum=minimum,
        maximum=maximum,
        labels=_label_rules(element),
    )


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
    площадки добавляется в служебный слой с рангом по месту слоя в
    дереве проекта, и обычные значки ниже него скрываются.  На объект
    нужна одна точка, поэтому это не замедляет ни подготовку, ни
    отрисовку.
    """
    return list(_template_data(path)["obstacles"])


def _obstacle_layer(element, name):
    """Препятствие со значка линии или полигона или None."""
    kind = _obstacle_geometry(element)
    if kind is None:
        return None
    renderer = _renderer_element(element)
    if renderer is None:
        return None
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
            return None
        leaf = PoiRule(size=size, minimum=minimum, maximum=maximum)
        tree = PoiRule(
            minimum=minimum, maximum=maximum, children=[leaf],
        )
    if not _has_icon(tree):
        return None
    return PoiLayer(
        name=name,
        source=element.findtext("datasource") or "",
        crs=_layer_crs(element),
        root=tree,
        minimum=minimum,
        maximum=maximum,
        geometry=kind,
    )


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
        f"\"{ICON_HIDE_FIELD}\", ';'), '{index}'), false), false, true)"
    )


def icon_enabled_expression_for(indexes):
    """Видимость значка в нескольких диапазонах масштаба.

    Значок слоя может быть один на несколько диапазонов (у кладбищ
    значок один, а диапазонов несколько).  Решение принимается по
    верхней границе диапазона — так же, как у мостов, — поэтому
    значок скрыт, если он скрыт в любом из диапазонов.
    """
    if not indexes:
        return "true"
    if len(indexes) == 1:
        return icon_enabled_expression(indexes[0])
    terms = " or ".join(
        "array_contains(string_to_array("
        f"\"{ICON_HIDE_FIELD}\", ';'), '{index}')"
        for index in indexes
    )
    return f"if(coalesce({terms}, false), false, true)"


def icon_symbol_bands(layer, bands):
    """Диапазоны масштаба по символам значка слоя.

    Возвращает словарь (имя символа) -> список индексов диапазонов,
    в которых этот символ рисуется.
    """
    result = {}
    for leaf in leaves(layer.root):
        if not leaf.symbol:
            continue
        indexes = result.setdefault(leaf.symbol, set())
        indexes.update(band_indexes(leaf.minimum, leaf.maximum, bands))
    return {
        symbol: sorted(indexes) for symbol, indexes in result.items()
    }


def base_filter(filter_text, suppression):
    """Отбрасывает условие подавления из фильтра правила.

    Возвращает исходное условие видимости: в шаблоне подавление
    добавлено в конец фильтра, поэтому его можно снять точным
    совпадением суффикса, без разбора скобок.  ``None`` — подавления
    в фильтре нет (слои, где значки не вытесняются).
    """
    if not suppression or suppression == "false":
        return None
    condition = f"NOT ({suppression})"
    if filter_text == condition:
        return ""
    suffix = f") AND {condition}"
    if filter_text.startswith("(") and filter_text.endswith(suffix):
        return filter_text[1:-len(suffix)]
    return None


def data_filter(base, data_suppression):
    """Фильтр правила с подавлением по данным слоя."""
    condition = f"NOT ({data_suppression})"
    if not base:
        return condition
    return f"({base}) AND {condition}"


def extend_filter(filter_text, layer, leaf, bands):
    """Добавляет к фильтру правила подавление значка."""
    suppression = suppression_expression(layer, leaf, bands)
    if suppression == "false":
        return filter_text
    condition = f"NOT ({suppression})"
    if filter_text:
        return f"({filter_text}) AND {condition}"
    return condition


def hide_field_name():
    """Имя поля с порогами подавления значка в слое POI.

    В поле лежат пороги сразу по всем диапазонам масштаба и уровням
    детализации: значение для диапазона N и уровня L лежит в слоте
    ``N * len(LOD_LEVELS) + (L - 2)``.  Пустой слот — подавителя
    нет.  Одно поле на слой: добавление колонок в Spatialite стоит
    около сотни миллисекунд, а слоёв десятки.
    """
    return "hide"


def _layer_band_indexes(layer, bands):
    """Диапазоны масштаба, в которых слою нужен порог подавления.

    Это диапазоны правил значка и диапазоны правил подписи: подпись
    скрывается вместе со значком, поэтому порог нужен и там, где
    значок не рисуется.
    """
    indexes = set()
    for leaf in leaves(layer.root):
        indexes.update(band_indexes(leaf.minimum, leaf.maximum, bands))
    for label in layer.labels:
        for leaf in leaves(layer.root):
            if _leaf_intersects(leaf, label):
                indexes.update(
                    band_indexes(leaf.minimum, leaf.maximum, bands)
                )
    return sorted(indexes)


def _leaf_intersects(leaf, label):
    """Пересекается ли диапазон правила значка с диапазоном подписи."""
    if label.maximum and leaf.minimum >= label.maximum:
        return False
    if label.minimum and leaf.maximum and leaf.maximum <= label.minimum:
        return False
    return True


def hide_slot_index(band_index):
    """Смещение слота диапазона в поле слоя (кратно числу уровней).

    Порядок слотов общий для всех слоёв: два слоя шаблона могут
    читать один и тот же файл (например, «barrier gate 15-» и
    «barrier gate 15+»), и тогда их значения должны ложиться в
    одни и те же слоты.
    """
    return band_index * len(LOD_LEVELS)


def hide_value_expression(band_index):
    """Порог подавления для текущего уровня детализации.

    Пустое поле или пустой слот — подавителя нет, порог бесконечный.
    """
    offset = hide_slot_index(band_index)
    return (
        f"coalesce(to_real(NULLIF(array_get(string_to_array("
        f"\"{hide_field_name()}\", ';'), "
        f"{offset} + @level_of_detail - {LOD_LEVELS[0]}), '')), 1e30)"
    )


def suppression_data_expression(layer, leaf, bands):
    """Условие «значок подавлен» по данным слоя.

    То же условие, что давало выражение с overlay_nearest: значок
    скрыт, если радиус подавления (размер значка, приведённый к
    масштабу) больше расстояния до ближайшего подавителя.  Расстояние
    лежит в данных слоя, поэтому выражение не ищет соседей по
    пространственному индексу — оно просто читает поле.
    """
    terms = []
    for index in band_indexes(leaf.minimum, leaf.maximum, bands):
        lower, upper = bands[index]
        scale = []
        if lower:
            scale.append(f"@map_scale >= {lower:g}")
        if upper:
            scale.append(f"@map_scale < {upper:g}")
        term = (
            f"@map_scale * {leaf.size:g} / 1000 > "
            f"{hide_value_expression(index)}"
        )
        if scale:
            term = "(" + " AND ".join(scale) + f") AND {term}"
        terms.append(term)
    if not terms:
        return "false"
    if len(terms) == 1:
        return terms[0]
    return "(" + " OR ".join(terms) + ")"


def layer_suppression_data_expression(layer, bands, minimum=None, maximum=None):
    """Подавление по данным слоя для правила подписи.

    Условие собирается из листовых правил слоя: подпись скрывается
    вместе со значком.
    """
    expressions = []
    for leaf in leaves(layer.root):
        if minimum is not None or maximum is not None:
            if minimum and leaf.maximum and leaf.maximum <= minimum:
                continue
            if maximum and leaf.minimum >= maximum:
                continue
        expression = suppression_data_expression(layer, leaf, bands)
        if expression != "false" and expression not in expressions:
            expressions.append(expression)
    if not expressions:
        return "false"
    if len(expressions) == 1:
        return expressions[0]
    return "(" + " OR ".join(expressions) + ")"


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


def restore_base_filters(layers, bands):
    """Убирает автоматическое подавление из прочитанных правил шаблона.

    При сборке служебного слоя нужны только исходные условия видимости.
    Иначе правила ссылались бы на ещё не построенный служебный слой.

    Подавление в шаблоне может быть двух видов: прежнее выражение с
    overlay_nearest или новое, проверка данных.  Снимается то, что
    есть: условие формируется тем же кодом, что и при записи
    шаблона, поэтому его можно удалить точным совпадением суффикса,
    без разбора скобок.
    """
    for layer in layers:
        for leaf in leaves(layer.root):
            for suppression in (
                suppression_expression(layer, leaf, bands),
                suppression_data_expression(layer, leaf, bands),
            ):
                if suppression == "false":
                    continue
                condition = f"NOT ({suppression})"
                if leaf.filter == condition:
                    leaf.filter = ""
                    break
                suffix = f") AND {condition}"
                if leaf.filter.startswith("(") and leaf.filter.endswith(suffix):
                    leaf.filter = leaf.filter[1:-len(suffix)]
                    break


def template_variables(path=None):
    """Переменные проекта из шаблона (уровень детализации и прочие)."""
    path = Path(path or template_path())
    cached = _TEMPLATE_VARIABLES.get(path)
    if cached is not None:
        return dict(cached)
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
    _TEMPLATE_VARIABLES[path] = variables
    return dict(variables)


def _source_path(config, source_text):
    """Файл данных слоя шаблона в каталоге проекта."""
    relative = (source_text or "").split("|", 1)[0]
    if not relative:
        return None
    path = Path(relative)
    if not path.is_absolute():
        path = Path(config.layers_dir) / relative
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
    # Шаблон уже разобран для значков: второй раз 54 МБ XML не читаем.
    sources = _template_data()["sources"]
    return {name: sources[name] for name in names if name in sources}


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


def _in_scale(rule, scale):
    """Попадает ли масштаб в диапазон правила.

    Границы наследуются от ветки: у группы они уже учтены, поэтому
    правило вне диапазона не может дать ни совпадения, ни размера.
    """
    if rule.minimum and scale < rule.minimum:
        return False
    if rule.maximum and scale > rule.maximum:
        return False
    return True


class _RuleEvaluator:
    """Вычисление видимости точки по дереву правил слоя.

    Повторяет семантику QGIS: правило подходит, если его фильтр истинен
    и масштаб попадает в диапазон; ветка ELSE срабатывает, только если
    ни один предыдущий сосед не подошёл.
    """

    def __init__(self, project, layer):
        from qgis.core import QgsExpression

        # Класс запоминается один раз: импорт внутри проверки правила
        # стоил миллионы обращений к загрузчику модулей.
        self._expression_class = QgsExpression
        self._expressions = {}
        self._contexts = {
            level: _evaluation_context(project, layer, level)
            for level in LOD_LEVELS
        }

    def _expression(self, text):
        if text not in self._expressions:
            if not text or text.strip().upper() == "ELSE":
                self._expressions[text] = None
            else:
                self._expressions[text] = self._expression_class(text)
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
                # Ветка вне масштаба не может дать совпадения: границы
                # группы наследуются детьми.  Проверка идёт до вычисления
                # фильтра — иначе на каждый слой считались выражения
                # заведомо пустых диапазонов.  Пропущенный сосед не
                # считается «подошедшим», поэтому ELSE может сработать.
                if not _in_scale(child, scale):
                    continue
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
        if not _in_scale(rule, scale):
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
        ranked = info.name in SELF_RANKED_LAYERS
        start = len(points)
        priorities = []
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
            named = (
                _feature_is_named(feature)
                if info.name == PEAK_LAYER
                else True
            )
            points.append(_Point(
                rank=info.rank,
                lon=lon,
                lat=lat,
                name=info.name,
                fid=feature.id(),
                geometry=info.geometry,
                named=named,
                bands={index: level for index, level, _size in visible},
                sizes={index: size for index, _level, size in visible},
            ))
            if ranked:
                priorities.append(_settlement_priority(feature))
        if ranked:
            _apply_priorities(points[start:], priorities, info.rank)
    _lower_unnamed_peaks(points)
    return points


def _feature_is_named(feature):
    """Есть ли у объекта любой именной тег (включая ``name:*``)."""
    fields = feature.fields()
    for index in range(fields.count()):
        key = fields.at(index).name().lower()
        if key in NAME_TAGS or key.startswith("name:"):
            if feature.attribute(index) not in (None, ""):
                return True
    return False


def _lower_unnamed_peaks(points):
    """Ставит безымянные пики после всех населённых пунктов.

    Пик без имени не должен скрывать значок или подпись населённого
    пункта.  Ранг остаётся меньше следующего целого — места
    нижележащего слоя в дереве, — поэтому порядок относительно
    остальных объектов не меняется.
    """
    unnamed = [
        point for point in points
        if point.name == PEAK_LAYER and not point.named
    ]
    if not unnamed:
        return
    ranks = [
        point.rank for point in points
        if point.name in SETTLEMENT_LAYERS
    ]
    if not ranks:
        return
    limit = max(ranks)
    rank = (limit + math.floor(limit) + 1.0) / 2.0
    for point in unnamed:
        point.rank = rank


def _apply_priorities(points, priorities, rank):
    """Расставляет ранги точек слоя по приоритету объектов.

    Ранг слоя — целое число, место слоя в дереве проекта.  Точки слоя
    получают дробную часть: их порядок внутри слоя задаётся
    приоритетом объекта, а положение слоя относительно остальных
    слоёв не меняется.
    """
    order = sorted(
        range(len(points)),
        key=lambda index: (
            priorities[index], points[index].lon, points[index].lat,
        ),
    )
    total = len(points)
    for position, index in enumerate(order, start=1):
        points[index].rank = rank + position / (total + 1)


def _feature_value(feature, name):
    """Значение поля объекта (None, если такого поля нет)."""
    if feature.fields().lookupField(name) < 0:
        return None
    value = feature.attribute(name)
    return None if value is None or value == "" else value


def _population(value):
    """Население числом (0, если его нет или оно не число)."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _settlement_priority(feature):
    """Приоритет населённого пункта: тип места, потом население.

    Город вытесняет посёлок, посёлок — деревню, деревня — хутор.
    Внутри одного типа важнее тот, у кого больше население; при
    равном населении — тот, кто стоит раньше по координатам (тот же
    порядок, что и у остальных слоёв).

    Пункт без типа места получает ранг по населению — тем же
    порядком, что и в шаблоне (ветка «by population»).
    """
    place = str(_feature_value(feature, "place") or "")
    population = _population(_feature_value(feature, "population"))
    rank = PLACE_RANKS.get(place)
    if rank is None:
        for limit, value in POPULATION_RANKS:
            if population >= limit:
                rank = value
                break
    return rank, -population


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

    # Радиус и косинус широты считаются один раз на точку: в поиске
    # соседей они нужны для каждой пары.
    limits = [
        (point.sizes.get(index, 0.0) if point.sizes else 0.0) * scale / 1000.0
        for point in entries
    ]
    cosines = [math.cos(math.radians(point.lat)) for point in entries]
    max_limit = max(limits, default=0.0)
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
                    # Расстояние между точками в метрах: та же метрика,
                    # что и в выражении шаблона, — с cos(широты).  Без
                    # него расстояние завышалось, и служебный слой терял
                    # точки-подавители: значки накладывались.
                    distance = math.hypot(
                        (neighbour.lon - point.lon) * cosines[position],
                        neighbour.lat - point.lat,
                    ) * 111320.0
                    if distance > limits[other]:
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


def _hidden_icon_bands(points, bands):
    """Диапазоны, где значок проигрывает соседу того же слоя.

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
            and point.name in SELF_ICON_LAYERS
            and point.geometry in ("Line", "Polygon")
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
    """Пишет поле icon_hide в слои со значком на объекте.

    Поле хранит через точку с запятой индексы диапазонов, где значок
    скрыт.  Выражение шаблона выбирает нужный индекс по @map_scale.

    Запись идёт через SQLite напрямую: обновление строки через GDAL в
    Spatialite переписывает геометрию и пространственный индекс и стоит
    около сотни миллисекунд на объект, через SQLite — около сотни
    микросекунд.  Геометрия при этом не меняется: триггеры Spatialite
    висят на `UPDATE OF "GEOMETRY"`, а поле icon_hide — другое поле.
    """
    hidden = _hidden_icon_bands(points, bands)
    import sqlite3

    updates = {}
    for (name, fid), indexes in hidden.items():
        updates.setdefault(name, []).append(
            (fid, ";".join(str(index) for index in sorted(indexes)))
        )
    for info in layers:
        if info.name not in SELF_ICON_LAYERS:
            continue
        if info.geometry not in ("Line", "Polygon"):
            continue
        path = _source_path(config, info.source)
        if path is None:
            continue
        connection = sqlite3.connect(str(path))
        try:
            table = _sqlite_table(connection, path)
            columns = {
                row[1] for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            }
            if ICON_HIDE_FIELD not in columns:
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{ICON_HIDE_FIELD}" TEXT'
                )
            else:
                # Мост мог перестать вытесняться: старые значения
                # надо сбросить, иначе значок останется скрытым.
                connection.execute(
                    f'UPDATE "{table}" SET "{ICON_HIDE_FIELD}" = NULL'
                )
            values = updates.get(info.name, ())
            if values:
                primary = _sqlite_primary_key(connection, table)
                connection.executemany(
                    f'UPDATE "{table}" SET "{ICON_HIDE_FIELD}" = ? '
                    f'WHERE "{primary}" = ?',
                    [(value, fid) for fid, value in values],
                )
            connection.commit()
        finally:
            connection.close()
    return hidden


def _band_radius(band, candidates, band_index):
    """Предельный радиус подавления в диапазоне, метры.

    Внутри диапазона значок подавляется, пока радиус подавления
    (размер значка, приведённый к масштабу) не перестал покрывать
    расстояние до подавителя.  Радиус растёт вместе с масштабом, и
    больше верхней границы диапазона он не нужен: в следующем
    диапазоне свой набор подавителей.
    """
    lower, upper = band
    sizes = [point.sizes.get(band_index, 0.0) for point in candidates]
    size = max(sizes) if sizes else 0.0
    if not size:
        return 0.0
    if upper:
        return size * upper / 1000.0
    # Последний диапазон: масштаб не ограничен сверху, но значок
    # дальше предельного масштаба уже не различим.
    return size * MAX_SUPPRESSION_SCALE / 1000.0


def _cell_distance(cx, cy, gx, gy, cell_lon, cell_lat, cos_lat):
    """Наименьшее расстояние до точки в ячейке сетки, метры."""
    if cx > gx:
        dlon = (cx - gx - 1) * cell_lon
    elif cx < gx:
        dlon = (gx - cx - 1) * cell_lon
    else:
        dlon = 0.0
    if cy > gy:
        dlat = (cy - gy - 1) * cell_lat
    elif cy < gy:
        dlat = (gy - cy - 1) * cell_lat
    else:
        dlat = 0.0
    dlon *= 111320.0 * cos_lat
    dlat *= 111320.0
    return math.sqrt(dlon * dlon + dlat * dlat)


def _heap_cells(gx, gy, radius, cell_lon, cell_lat, cos_lat, best):
    """Ячейки сетки по возрастанию расстояния до них.

    Нужен, когда радиус больше ячейки: подавитель может лежать в
    нескольких ячейках, и перебирать квадрат вокруг точки дорого.
    Обход останавливается, когда до ячейки дальше найденного порога.
    """
    heap = [(0.0, gx, gy)]
    seen = {(gx, gy)}
    while heap:
        distance, cx, cy = heapq.heappop(heap)
        if distance > radius:
            break
        nearest = best[LOD_LEVELS[0]]
        if nearest is not None and nearest <= distance:
            break
        yield cx, cy
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nx = cx + dx
                ny = cy + dy
                if (nx, ny) in seen:
                    continue
                seen.add((nx, ny))
                heapq.heappush(heap, (
                    _cell_distance(nx, ny, gx, gy, cell_lon, cell_lat,
                                   cos_lat), nx, ny,
                ))


def _nearest_thresholds(grid, lon, lat, radius, cell_lon, cell_lat):
    """Пороги до ближайших подавителей по уровням детализации.

    Расстояния — в метрах, те же, что считает выражение шаблона:
    разница долгот на широте точки, умноженная на 111320 и на
    косинус широты, и разница широт на 111320.

    Размер ячейки сетки обычно не меньше радиуса поиска, поэтому
    подавитель в радиусе лежит в соседних ячейках: тогда достаточно
    блока 5×5.  По широте размер ячейки ограничен 1000 м, а радиус
    последнего диапазона бывает больше — тогда ячейки обходятся по
    расстоянию.  Оба способа просматривают надмножество ячеек,
    которые может задеть радиус, а кандидаты за радиусом
    отбрасываются, поэтому пороги не меняются.

    В кандидаты попадают только те, кто встаёт раньше: обход идёт по
    порядку (ранг, долгота, широта), поэтому в сетке лежат лишь
    точки с большим приоритетом — ровно те, кого выражение считает
    подавителями.

    В сетке лежит тройка (точка, меридиан, уровень): меридиан —
    111320 на косинус широты кандидата, уровень — его минимальный
    LOD в этом диапазоне.  Оба считаются один раз на кандидата, а
    не на каждую проверку.
    """
    cos_lat = math.cos(math.radians(lat))
    gx = int(lon // cell_lon)
    gy = int(lat // cell_lat)
    best = {level: None for level in LOD_LEVELS}
    # Ячейка по широте может быть меньше радиуса: размер сетки
    # ограничен 1000 м, а радиус последнего диапазона больше.  Тогда
    # подавитель в радиусе может лежать в нескольких ячейках от
    # точки, и блок 5×5 его не покроет — идёт обход по расстоянию.
    if radius <= cell_lat * 111320.0:
        cells = (
            (gx + dx, gy + dy)
            for dx in (-2, -1, 0, 1, 2)
            for dy in (-2, -1, 0, 1, 2)
        )
    else:
        cells = _heap_cells(
            gx, gy, radius, cell_lon, cell_lat, cos_lat, best,
        )
    for cx, cy in cells:
        for candidate, meridian, lod in grid.get((cx, cy), ()):
            dlon = candidate.lon - lon
            dlat = candidate.lat - lat
            metric = math.sqrt(
                (dlon * 111320.0 * cos_lat) ** 2
                + (dlat * 111320.0) ** 2
            )
            if metric > radius:
                continue
            if metric <= MIN_SUPPRESSION_DISTANCE:
                # Точка на том же месте не вытесняет: выражение
                # шаблона отбрасывает такие кандидаты.
                continue
            # Конверт поиска overlay_nearest задаётся широтой
            # кандидата: он тоже ограничивает подавление.
            value = max(
                metric,
                meridian * abs(dlon),
                meridian * abs(dlat),
            )
            levels = _LEVELS_FROM.get(lod)
            if levels is None:
                levels = tuple(
                    level for level in LOD_LEVELS if level >= lod
                )
            current = best.get(lod)
            if current is not None and value >= current:
                continue
            for level in levels:
                previous = best[level]
                if previous is None or value < previous:
                    best[level] = value
    return best


def _suppression_distances(points, records, bands, needed=None, cancel=None):
    """Пороги подавления для точек POI.

    Возвращает словарь (имя слоя, fid) -> {диапазон: {уровень: метры}}.
    Порог — расстояние до ближайшего подавителя: значок скрыт, когда
    радиус подавления (размер значка, приведённый к масштабу) больше
    этого расстояния.

    ``needed`` — словарь (имя слоя -> диапазоны): порог считается
    только там, где слой его читает.  Подавители при этом учитываются
    все: сосед может быть из любого слоя.
    """
    order = sorted(points, key=lambda item: (item.rank, item.lon, item.lat))
    result = {}
    # Широта для сетки берётся по всем точкам, а не только по кандидатам:
    # иначе ячейка по долготе вышла бы уже нужной для точки-вопроса.
    latitude_limit = max(abs(point.lat) for point in points)
    cos_limit = max(math.cos(math.radians(latitude_limit)), 0.01)
    for index, band in enumerate(bands):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        candidates = records.get(index) or []
        if not candidates:
            continue
        member = {id(point) for point in candidates}
        radius = _band_radius(band, candidates, index)
        if radius <= 0:
            continue
        # Ячейка сетки: одна ячейка на радиус поиска.  Мельче — больше
        # колец при поиске далёкого подавителя, крупнее — больше
        # лишних кандидатов в просмотренной ячейке.  Размер ячейки на
        # результат не влияет: нижняя оценка расстояния до ячейки
        # остаётся корректной при любой сетке.
        cell_lat = min(max(radius, 150.0), 1000.0) / 111320.0
        latitude_max = max(abs(point.lat) for point in candidates)
        cell_lon = cell_lat / max(math.cos(math.radians(latitude_max)), 0.01)
        # Сетка занятых ячеек: ячейка не меньше радиуса поиска, поэтому
        # любой подавитель в радиусе лежит в одной из девяти ячеек
        # вокруг точки.  Если все они пусты, подавителей рядом нет и
        # порогов не будет — искать нечего.  Запас в 0.1 % — на
        # округление при разбиении на ячейки.
        occupied_lat = radius * 1.001 / 111320.0
        occupied_lon = occupied_lat / cos_limit
        occupied = set()
        grid = {}
        for point in order:
            if point.geometry == "Point" and _band_needed(
                needed, point, index,
            ):
                ox = int(point.lon // occupied_lon)
                oy = int(point.lat // occupied_lat)
                near = False
                for dx, dy in _NEIGHBOUR_OFFSETS:
                    if (ox + dx, oy + dy) in occupied:
                        near = True
                        break
                if near:
                    thresholds = _nearest_thresholds(
                        grid, point.lon, point.lat, radius, cell_lon, cell_lat,
                    )
                    values = {
                        level: value for level, value in thresholds.items()
                        if value is not None
                    }
                    if values:
                        key = (point.name, point.fid)
                        result.setdefault(key, {})[index] = values
            if id(point) in member:
                grid.setdefault(
                    (int(point.lon // cell_lon), int(point.lat // cell_lat)),
                    [],
                ).append((
                    point,
                    111320.0 * math.cos(math.radians(point.lat)),
                    point.bands.get(index, 2),
                ))
                occupied.add(
                    (int(point.lon // occupied_lon),
                     int(point.lat // occupied_lat)),
                )
    return result


def _band_needed(needed, point, index):
    """Нужен ли порог этого диапазона слою точки."""
    if needed is None:
        return True
    indexes = needed.get(point.name)
    return indexes is not None and index in indexes


def _write_suppression(config, layers, distances, bands, cancel=None):
    """Пишет пороги подавления в слои POI.

    Поле одно на слой: в нём слоты по диапазонам масштаба и уровням
    детализации.  Порядок слотов общий для всех слоёв: два слоя
    шаблона могут читать один и тот же файл (например, «barrier gate
    15-» и «barrier gate 15+»), и тогда их значения дописываются в
    одни и те же слоты — диапазоны у таких слоёв не пересекаются.

    Запись идёт через SQLite напрямую: обновление одной строки через
    GDAL в Spatialite стоит около сотни миллисекунд (переписывается
    геометрия и пространственный индекс), через SQLite — около сотни
    микросекунд.

    Файлов десятки, а коммит каждого ждёт диск: пишутся они
    параллельно, иначе ожидание диска идёт впустую.
    """
    import sqlite3

    by_layer = {}
    for (name, fid), per_band in distances.items():
        by_layer.setdefault(name, {})[fid] = per_band

    by_file = {}
    for info in layers:
        path = _source_path(config, info.source)
        if path is None:
            continue
        by_file.setdefault(path, []).append(info)

    slots = len(bands) * len(LOD_LEVELS)
    prepared = {}
    for path, group in by_file.items():
        values = {}
        for info in group:
            indexes = set(_layer_band_indexes(info, bands))
            if not indexes:
                continue
            for fid, per_band in by_layer.get(info.name, {}).items():
                parts = values.get(fid)
                if parts is None:
                    parts = [""] * slots
                    values[fid] = parts
                for index, levels in per_band.items():
                    if index not in indexes:
                        # Диапазон слою не нужен: ни значок, ни подпись
                        # в нём не рисуются.
                        continue
                    offset = hide_slot_index(index)
                    for level, value in levels.items():
                        parts[offset + level - LOD_LEVELS[0]] = repr(value)
        prepared[path] = values

    def write(path):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Preparation cancelled")
        connection = sqlite3.connect(str(path))
        try:
            table = _sqlite_table(connection, path)
            columns = {
                row[1] for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                )
            }
            if hide_field_name() not in columns:
                connection.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN '
                    f'"{hide_field_name()}" TEXT'
                )
            values = prepared[path]
            if values:
                primary = _sqlite_primary_key(connection, table)
                connection.executemany(
                    f'UPDATE "{table}" SET "{hide_field_name()}" = ? '
                    f'WHERE "{primary}" = ?',
                    [
                        (";".join(parts).rstrip(";"), fid)
                        for fid, parts in values.items()
                    ],
                )
            connection.commit()
        finally:
            connection.close()

    paths = list(prepared)
    if len(paths) < 2:
        for path in paths:
            write(path)
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    workers = min(cpu_workers(limit=8), len(paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(write, path) for path in paths]):
            future.result()


def _sqlite_table(connection, path):
    """Имя таблицы слоя в файле Spatialite."""
    rows = connection.execute(
        "SELECT f_table_name FROM geometry_columns"
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise ValueError(f"в {path} нет слоёв с геометрией")
    raise ValueError(f"в {path} несколько слоёв: {[r[0] for r in rows]}")


def _sqlite_primary_key(connection, table):
    """Имя первичного ключа таблицы."""
    for row in connection.execute(f'PRAGMA table_info("{table}")'):
        if row[5]:
            return row[1]
    return "fid"


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
            # Поля по номеру, а не по имени: поиск имени на каждой
            # записи обходится дороже самой записи.
            indexes = {
                definition.GetFieldDefn(number).GetName(): number
                for number in range(definition.GetFieldCount())
            }
            layer.StartTransaction()
            for point in items:
                feature = ogr.Feature(definition)
                feature.SetField(indexes["rank"], point.rank)
                feature.SetField(indexes["lod"], point.bands[index])
                feature.SetField(indexes["layer"], point.name)
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
    report(f"POI: точек {len(points)}")
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
    needed = {
        info.name: set(_layer_band_indexes(info, bands)) for info in layers
    }
    distances = _suppression_distances(
        points, records, bands, needed=needed, cancel=cancel,
    )
    report(f"POI: пороги подавления, {len(distances)} объектов")
    _write_suppression(config, layers, distances, bands, cancel=cancel)
    report("POI: пороги подавления записаны в слои")
    _write_aux(config, records, "EPSG:4326", AUX_FILE_NAME)
    (Path(config.vector_dir) / LEGACY_AUX_FILE_NAME_3857).unlink(missing_ok=True)
    report(
        f"POI: служебные слои значков, {total} записей "
        f"для {len(layers)} слоёв и {len(obstacles)} препятствий"
    )
    return total
