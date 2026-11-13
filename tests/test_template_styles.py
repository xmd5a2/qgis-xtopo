"""Инварианты шаблона проекта.

Стили задаются только в шаблоне (`automap.qgz`): ни инструментов, которые
правят шаблон, ни механизма починки готовых проектов в плагине нет.
Тесты проверяют, что нужные свойства в шаблоне есть — иначе карта будет
нарисована не так, как задумано.
"""

import re
import unittest
import zipfile
from xml.etree import ElementTree

from qgis_xtopo.core import poi_icons
from qgis_xtopo.core.resources import PACKAGE_DIR, template_path

# Слой пересыхающей воды рисуется горизонтальной штриховкой.
WATER_LAYER = "water intermittent"
WATER_COLOR = "168,224,255,255,rgb:0.6588235,0.8784314,1,1"
WATER_DISTANCE_MM = "1"
WATER_LINE_WIDTH_MM = "0.25"

# Символьный слой со штрихами изолиний.
HASH_CLASS = "HashLine"
ISOLINES_REGULAR = "[TERRAIN] isolines regular"
ISOLINES_GLACIER = "[TERRAIN] isolines glacier"
DD_PROPERTY = "offsetAlongLine"


def _template_root():
    with zipfile.ZipFile(template_path()) as archive:
        return ElementTree.fromstring(archive.read("automap.qgs"))


def _find_option(element, name):
    """Первое свойство с указанным именем (в QGIS свойства лежат в картах)."""
    if element is None:
        return None
    for option in element.iter("Option"):
        if option.get("name") == name:
            return option
    return None


def _option_value(element, name):
    option = _find_option(element, name)
    if option is None:
        return None
    return option.get("value")


def _hash_layers(layer):
    """Символьные слои HashLine внутри слоя карты."""
    return [
        item for item in layer.iter("layer")
        if item.get("class") == HASH_CLASS
    ]


def _phase_shift(hash_layer):
    """Свойство сдвига фазы штрихов, если оно задано в карте свойств."""
    ddp = hash_layer.find("data_defined_properties")
    properties = _find_option(ddp, "properties")
    return _find_option(properties, DD_PROPERTY)


class TemplateLayoutTest(unittest.TestCase):
    """Шаблон хранится один раз — в корне репозитория."""

    def test_single_copy_of_template(self):
        root = PACKAGE_DIR / "automap.qgz"
        packaged = PACKAGE_DIR / "resources" / "automap.qgz"
        self.assertTrue(template_path().exists())
        if root.exists():
            # Исходное дерево: копии в resources нет.
            self.assertFalse(packaged.exists())
        else:
            # Собранный пакет: шаблон лежит в resources.
            self.assertTrue(packaged.exists())


class TemplateFilterExpressionTest(unittest.TestCase):
    """Фильтры шаблона — валидные выражения QGIS.

    При запекании фильтров подавления в шаблон HTML-сущности однажды
    экранировали дважды: в XML осталось ``&amp;lt;``, QGIS читал
    ``&lt;``, и правило с таким фильтром не срабатывало.
    """

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        cls.root = _template_root()

    def _expressions(self):
        for element in self.root.iter():
            for attribute, value in element.attrib.items():
                if attribute == "filter" and value:
                    yield element, attribute, value

    def test_no_html_entities_in_expressions(self):
        broken = [
            (element.tag, attribute, value[:120])
            for element, attribute, value in self._expressions()
            if any(entity in value for entity in ("&lt;", "&gt;", "&quot;"))
        ]
        self.assertEqual(broken, [])

    def test_expressions_parse(self):
        from qgis.core import QgsExpression

        broken = []
        for element, attribute, value in self._expressions():
            if value.strip().upper() == "ELSE":
                continue
            expression = QgsExpression(value)
            if expression.hasParserError():
                broken.append((element.tag, attribute, value[:120]))
        self.assertEqual(broken, [])


class TunnelLayerOrderTest(unittest.TestCase):
    """Туннели — под линейными слоями, над площадными.

    Туннель проходит сквозь рельеф: изолинии и прочие линии рисуются
    поверх него, а площадные заливки (карьер, ледник, лес) — под ним.
    """

    TUNNELS = (
        "rail tunnel", "highway main tunnel",
        "river tunnel", "stream tunnel", "canal tunnel",
    )
    LINEAR_ABOVE = (
        "[TERRAIN] isolines regular", "[TERRAIN] isolines glacier",
        "highway main", "railway all", "path", "track",
        "river", "stream", "cliff", "ridge, arete",
    )
    POLYGON_BELOW = (
        "quarry", "glacier", "wood", "farmland", "scree", "bare rock",
    )

    @classmethod
    def setUpClass(cls):
        cls.order = [
            item.get("name")
            for item in cls._template_root().iter("layer-tree-layer")
        ]

    @staticmethod
    def _template_root():
        return _template_root()

    def test_tunnels_sit_below_isolines(self):
        for tunnel in self.TUNNELS:
            for linear in ("[TERRAIN] isolines regular", "[TERRAIN] isolines glacier"):
                self.assertLess(
                    self.order.index(linear), self.order.index(tunnel),
                    f"{tunnel} должен быть под {linear}",
                )

    def test_tunnels_sit_below_all_linear(self):
        for tunnel in self.TUNNELS:
            for linear in self.LINEAR_ABOVE:
                self.assertLess(
                    self.order.index(linear), self.order.index(tunnel),
                    f"{tunnel} должен быть под {linear}",
                )

    def test_tunnels_sit_above_polygon_fills(self):
        for tunnel in self.TUNNELS:
            for polygon in self.POLYGON_BELOW:
                self.assertLess(
                    self.order.index(tunnel), self.order.index(polygon),
                    f"{tunnel} должен быть над {polygon}",
                )

    def test_tunnel_group_is_contiguous(self):
        positions = [self.order.index(tunnel) for tunnel in self.TUNNELS]
        self.assertEqual(
            max(positions) - min(positions), len(positions) - 1,
            "туннельные слои должны идти подряд",
        )


class LandcoverLayerOrderTest(unittest.TestCase):
    """Порядок площадных слоёв: Topo и видимость прибрежных болот."""

    OPEN_LAND = ("farmland", "grass", "orchard", "vineyard", "farmyard")

    @classmethod
    def setUpClass(cls):
        cls.order = [
            item.get("name")
            for item in _template_root().iter("layer-tree-layer")
        ]

    def test_wetland_stays_above_water(self):
        # В Topo water=9, wetland=8, но в QGIS болото оставлено выше воды:
        # иначе прибрежные мелководья и заболоченные участки скрываются.
        self.assertLess(
            self.order.index("wetland"), self.order.index("water"),
            "болото должно оставаться выше воды",
        )

    def test_wood_and_scrub_are_above_open_land(self):
        for forest in ("wood", "scrub"):
            for open_land in self.OPEN_LAND:
                self.assertLess(
                    self.order.index(forest), self.order.index(open_land),
                    f"{forest} должен быть выше {open_land} (order 7 против 5)",
                )


class IsolinesHachureTest(unittest.TestCase):
    """Сдвиг фазы штрихов задан в шаблоне для всех изолиний."""

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in cls.root.iter("maplayer")
        }

    def _hash_layers(self, layer_name):
        layer = self.layers.get(layer_name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {layer_name}")
        return _hash_layers(layer)

    def test_regular_isolines_have_shift(self):
        hash_layers = self._hash_layers(ISOLINES_REGULAR)
        self.assertTrue(hash_layers)
        for hash_layer in hash_layers:
            shift = _phase_shift(hash_layer)
            self.assertIsNotNone(shift, "нет сдвига фазы штрихов")
            self.assertEqual(_option_value(shift, "active"), "true")
            expression = _option_value(shift, "expression") or ""
            self.assertIn("abs(sin($id", expression)
            self.assertIn("@map_scale", expression)

    def test_glacier_isolines_have_shift(self):
        hash_layers = self._hash_layers(ISOLINES_GLACIER)
        self.assertTrue(hash_layers)
        for hash_layer in hash_layers:
            self.assertIsNotNone(_phase_shift(hash_layer))

    def test_shift_is_inside_properties(self):
        """Свойство вне карты properties QGIS не читает — штрихи в колонки."""
        for layer_name in (ISOLINES_REGULAR, ISOLINES_GLACIER):
            for hash_layer in self._hash_layers(layer_name):
                ddp = hash_layer.find("data_defined_properties")
                outer = ddp.find("Option")
                for option in outer.findall("Option"):
                    self.assertNotEqual(option.get("name"), DD_PROPERTY)


class LabelPriorityTest(unittest.TestCase):
    """Название острова не должно вытесняться подписью пика."""

    ISLAND_LAYERS = ("island polygon", "island point names")
    PEAK_LAYER = "peak"

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _priorities(self, name):
        layer = self.layers.get(name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {name}")
        values = {
            int(item.get("priority", 0))
            for item in layer.iter("placement")
        }
        self.assertTrue(values, f"у слоя {name} нет размещения подписей")
        return values

    def test_island_names_are_placed_before_peaks(self):
        peak = max(self._priorities(self.PEAK_LAYER))
        for name in self.ISLAND_LAYERS:
            self.assertGreater(min(self._priorities(name)), peak)


class SettlementScaleTest(unittest.TestCase):
    """Хутора и отдельные дворы не показываются с 1:200000 и мельче."""

    LIMIT = 200000
    NAMES = ("hamlet", "isolated dwelling")

    @classmethod
    def setUpClass(cls):
        cls.layer = next(
            layer for layer in _template_root().iter("maplayer")
            if layer.findtext("layername") == "places main"
        )

    @staticmethod
    def _find(node, path):
        for rule in node.findall("rule"):
            title = rule.get("description") or rule.get("label")
            if title == path[0]:
                if len(path) == 1:
                    return rule
                return SettlementScaleTest._find(rule, path[1:])
        return None

    @classmethod
    def _leaves(cls, node):
        result = []
        for rule in node.findall("rule"):
            if rule.findall("rule"):
                result.extend(cls._leaves(rule))
            else:
                result.append(rule)
        return result

    def _branch(self, container, path):
        branch = self._find(self.layer.find(container), path)
        self.assertIsNotNone(branch, path)
        return branch

    def test_type_rules_stop_at_200k(self):
        for container in ("renderer-v2/rules", "labeling/rules"):
            for name in self.NAMES:
                with self.subTest(container=container, name=name):
                    branch = self._branch(
                        container, ["else", "else", name],
                    )
                    for rule in self._leaves(branch):
                        minimum = float(rule.get("scalemindenom") or 0)
                        maximum = float(rule.get("scalemaxdenom") or 0)
                        self.assertGreater(maximum, 0, (minimum, maximum))
                        self.assertLessEqual(
                            maximum, self.LIMIT, (minimum, maximum),
                        )
                        self.assertLess(minimum, self.LIMIT)

    def test_population_branch_excludes_them_at_200k(self):
        expected = (
            "NOT (place IN ('hamlet','isolated_dwelling') "
            "AND @map_scale >= 200000)"
        )
        for container in ("renderer-v2/rules", "labeling/rules"):
            group = self._branch(container, ["else", "by population"])
            self.assertIn(expected, group.get("filter") or "", container)


class TrackScaleTest(unittest.TestCase):
    """Треки рисуются на 1:50 000+ и зависят от уровня детализации."""

    @classmethod
    def setUpClass(cls):
        cls.layer = next(
            layer for layer in _template_root().iter("maplayer")
            if layer.findtext("layername") == "track"
        )

    def _integrity_groups(self):
        parent = next(
            rule for rule in self.layer.iter("rule")
            if "highway_integrity <=" in rule.get("filter", "")
        )
        return parent.findall("rule")

    def test_integrity_three_is_rendered_at_all_scales(self):
        for group in self._integrity_groups():
            filters = [rule.get("filter") for rule in group.findall("rule")]
            self.assertIn(
                "highway_integrity='3'", filters,
                "нет правила integrity=3 для масштаба "
                f"{group.get('scalemindenom')}",
            )

    def test_coarse_scales_depend_on_detail_level(self):
        for rule in self.layer.iter("rule"):
            if not rule.findall("rule"):
                continue
            scale_min = int(rule.get("scalemindenom") or 0)
            if scale_min >= 50000:
                self.assertIn(
                    "@level_of_detail", rule.get("filter", ""),
                    f"масштаб {scale_min}+ не зависит от level_of_detail",
                )


class ServiceRoadLodTest(unittest.TestCase):
    """Два уровня service: без service=* до 300k, с service=* до 100k."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {}
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername") or ""
            if cls._service_rule(layer) is not None:
                cls.layers[name] = layer
        cls.expected = {
            "highway main", "highway main layer >=1",
            "highway main layer <=-1", "highway main bridge",
            "highway main tunnel",
        }

    @staticmethod
    def _service_rule(layer):
        renderer = layer.find("renderer-v2")
        if renderer is None:
            return None
        inner = renderer.find("renderer-v2")
        target = inner if inner is not None else renderer
        rules = target.find("rules")
        if rules is None:
            return None

        def walk(rule):
            if rule.get("filter") == "highway = 'service'":
                return rule
            for child in rule.findall("rule"):
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(rules)

    @staticmethod
    def _branch(service, label):
        def walk(rule):
            if rule.get("label") == label:
                return rule
            for child in rule.findall("rule"):
                found = walk(child)
                if found is not None:
                    return found
            return None

        return walk(service)

    @classmethod
    def _leaves(cls, rule):
        result = []
        for child in rule.findall("rule"):
            if child.findall("rule"):
                result.extend(cls._leaves(child))
            else:
                result.append(child)
        return result

    def test_expected_layers_have_service_style(self):
        self.assertTrue(self.expected.issubset(self.layers), self.layers)

    def test_plain_service_lasts_until_300k(self):
        for name, layer in self.layers.items():
            service = self._service_rule(layer)
            plain = self._branch(service, "else")
            self.assertIsNotNone(plain, name)
            leaves = {
                int(leaf.get("scalemindenom") or 0): leaf
                for leaf in self._leaves(plain)
            }
            self.assertIn(
                "service IS NULL", leaves[50000].get("filter", ""),
                f"{name}: 50–100k должен различать service=*",
            )
            self.assertIn(
                ">= 4", leaves[50000].get("filter", ""),
                f"{name}: 50–100k должен требовать LOD 4 для service=*",
            )
            first = leaves[100000]
            if int(first.get("scalemaxdenom") or 0) == 300000:
                # Туннели и исходный слой: один диапазон с проверкой
                # масштаба внутри фильтра, иначе QGIS теряет символ.
                self.assertIn("service IS NULL", first.get("filter", ""), name)
                self.assertIn("@map_scale", first.get("filter", ""), name)
                self.assertIn(">= 3", first.get("filter", ""), name)
                self.assertIn(">= 4", first.get("filter", ""), name)
            else:
                for scale, lod in ((100000, 3), (200000, 4)):
                    self.assertIn(
                        "service IS NULL", leaves[scale].get("filter", ""),
                        f"{name}: service=* должен быть скрыт с {scale}",
                    )
                    self.assertIn(
                        f">= {lod}", leaves[scale].get("filter", ""),
                        f"{name}: {scale} должен требовать LOD {lod}",
                    )
            self.assertNotIn(
                300000, leaves,
                f"{name}: обычный service должен исчезать с 300k",
            )

    def test_tagged_service_hides_from_100k(self):
        for name, layer in self.layers.items():
            service = self._service_rule(layer)
            driveway = self._branch(service, "service driveway")
            self.assertIsNotNone(driveway, name)
            self.assertIn("@map_scale < 100000", driveway.get("filter", ""), name)
            leaves = self._leaves(driveway)
            self.assertTrue(leaves, name)
            self.assertIn(
                50000, {int(leaf.get("scalemindenom") or 0) for leaf in leaves},
                f"{name}: нет правила 50–100k",
            )
            for leaf in leaves:
                self.assertLess(
                    int(leaf.get("scalemindenom") or 0), 100000,
                    f"{name}: service=* остался на 100k+",
                )


class AerialwayContrastTest(unittest.TestCase):
    """Серые линии подъёмников на 1:100000+ остаются различимыми."""

    def test_grey_symbols_are_slightly_darker(self):
        root = _template_root()
        layers = {
            layer.findtext("layername"): layer
            for layer in root.iter("maplayer")
        }
        for name in ("aerialway cable car, gondola", "aerialway chair lift"):
            layer = layers[name]
            renderer = layer.find("renderer-v2")
            inner = renderer.find("renderer-v2")
            target = inner if inner is not None else renderer
            symbols = target.find("symbols")
            for symbol in symbols.findall("symbol"):
                if symbol.get("name") not in ("2", "3"):
                    continue
                colors = {
                    option.get("value")
                    for option in symbol.iter("Option")
                    if option.get("name") in (
                        "line_color", "color", "outline_color",
                    )
                }
                # QGIS может сохранять дробную часть rgb с разным
                # числом знаков — сравниваем устойчивый RGB-префикс.
                self.assertTrue(
                    any(color.startswith("85,85,85,255") for color in colors),
                    f"{name}, символ {symbol.get('name')}",
                )
                self.assertTrue(
                    any(color.startswith("105,105,105,255") for color in colors),
                    f"{name}, символ {symbol.get('name')}",
                )


class AerialwayGoodsStyleTest(unittest.TestCase):
    """Грузовая канатная дорога — сплошная тонкая серая линия до 1:200000."""

    @classmethod
    def setUpClass(cls):
        root = _template_root()
        layer = next(
            item for item in root.iter("maplayer")
            if item.findtext("layername") == "aerialway cable car, gondola"
        )
        renderer = layer.find("renderer-v2")
        inner = renderer.find("renderer-v2")
        cls.renderer = inner if inner is not None else renderer

    def _rules(self):
        return self.renderer.findall("rules/rule")

    def test_cableway_rules_exclude_goods(self):
        for rule in self._rules():
            if rule.get("filter") and "goods" not in rule.get("filter"):
                self.assertEqual(
                    rule.get("filter"),
                    "aerialway IN ('cable_car','gondola')",
                )

    def test_goods_rules_stop_at_200k(self):
        goods = [
            rule for rule in self._rules()
            if "goods" in (rule.get("filter") or "")
        ]
        self.assertEqual(len(goods), 3)
        self.assertEqual(
            sorted(int(rule.get("scalemaxdenom") or 0) for rule in goods),
            [50000, 100000, 200000],
        )
        self.assertIn(
            200000,
            {int(rule.get("scalemaxdenom") or 0) for rule in goods},
        )
        for rule in goods:
            self.assertLessEqual(
                int(rule.get("scalemaxdenom") or 0), 200000,
            )
            self.assertLess(
                int(rule.get("scalemindenom") or 0), 200000,
            )

    def test_goods_symbols_are_solid_and_thin(self):
        symbols = {
            symbol.get("name"): symbol
            for symbol in self.renderer.find("symbols").findall("symbol")
        }
        for name in ("4", "5", "6"):
            symbol = symbols[name]
            self.assertEqual(
                [layer.get("class") for layer in symbol.findall("layer")],
                ["SimpleLine"],
                name,
            )
            options = {
                option.get("name"): option.get("value")
                for option in symbol.find("layer").findall("Option/Option")
            }
            self.assertTrue(
                options["line_color"].startswith("105,105,105,255"), name,
            )
            self.assertEqual(options["line_style"], "solid", name)
            self.assertEqual(options["use_custom_dash"], "0", name)
            self.assertLess(float(options["line_width"]), 0.26, name)


class FunicularStyleTest(unittest.TestCase):
    """Фуникулёр рисуется как железная дорога на всех масштабах."""

    def test_railway_layers_have_funicular_branch(self):
        root = _template_root()
        layers = {
            layer.findtext("layername"): layer
            for layer in root.iter("maplayer")
        }
        for name in ("railway all", "rail tunnel"):
            renderer = layers[name].find("renderer-v2")
            branch = next(
                (
                    rule for rule in renderer.findall("rules/rule")
                    if rule.get("filter") == "railway='funicular'"
                ),
                None,
            )
            self.assertIsNotNone(branch, name)
            leaves = branch.findall("rule")
            self.assertEqual(len(leaves), 4, name)
            self.assertEqual(
                [
                    (leaf.get("scalemindenom"), leaf.get("scalemaxdenom"))
                    for leaf in leaves
                ],
                [
                    (None, "50000"),
                    ("50000", "100000"),
                    ("100000", "300000"),
                    ("300000", None),
                ],
                name,
            )
            for leaf in leaves:
                self.assertTrue(leaf.get("symbol"), name)


class IntermittentWaterStyleTest(unittest.TestCase):
    """Пересыхающая вода нарисована горизонтальной штриховкой без обводки."""

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()

    def _fill_layers(self):
        layer = None
        for item in self.root.iter("maplayer"):
            if item.findtext("layername") == WATER_LAYER:
                layer = item
                break
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {WATER_LAYER}")
        fills = []
        for renderer in layer.iter("renderer-v2"):
            for symbol in renderer.iter("symbol"):
                if symbol.get("type") != "fill":
                    continue
                for sub in symbol.findall("layer"):
                    fills.append(sub)
        self.assertTrue(fills)
        return fills

    def test_fill_is_hatch(self):
        for fill in self._fill_layers():
            self.assertEqual(fill.get("class"), "LinePatternFill")

    def test_hatch_is_horizontal(self):
        for fill in self._fill_layers():
            self.assertEqual(_option_value(fill, "angle"), "0")
            self.assertEqual(_option_value(fill, "distance_unit"), "MM")
            self.assertEqual(_option_value(fill, "distance"), WATER_DISTANCE_MM)
            self.assertEqual(
                _option_value(fill, "line_width"), WATER_LINE_WIDTH_MM
            )

    def test_hatch_keeps_fill_color(self):
        for fill in self._fill_layers():
            self.assertEqual(_option_value(fill, "color"), WATER_COLOR)


class IntermittentWaterwayWidthTest(unittest.TestCase):
    """Пересыхающие водотоки тоньше постоянных на 1:100000 и мельче."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    @staticmethod
    def _find(node, path):
        for rule in node.findall("rule"):
            title = rule.get("label") or rule.get("description")
            if title == path[0]:
                if len(path) == 1:
                    return rule
                return IntermittentWaterwayWidthTest._find(rule, path[1:])
        return None

    def _width(self, layer_name, path):
        layer = self.layers[layer_name]
        renderer = layer.find("renderer-v2")
        rule = self._find(renderer.find("rules"), path)
        self.assertIsNotNone(rule, f"{layer_name}/{'/'.join(path)}")
        symbol = next(
            symbol for symbol in renderer.findall("symbols/symbol")
            if symbol.get("name") == rule.get("symbol")
        )
        line = next(
            item for item in symbol.findall("layer")
            if item.get("class") == "SimpleLine"
        )
        return float(_option_value(line, "line_width"))

    def test_streams_are_thinner_at_100k(self):
        for label in ("100000 - 200000", "200000 - 300000",
                      "300000 - 500000"):
            regular = self._width("stream", ["default", label])
            intermittent = self._width(
                "stream intermittent", ["default", label])
            self.assertLess(intermittent, regular, label)
            self.assertAlmostEqual(intermittent, 0.12)

    def test_rivers_and_canals_are_thinner_at_100k(self):
        for label in ("100000 - 300000", "300000 - 0"):
            for regular_name, intermittent_name in (
                    ("river", "river intermittent"),
                    ("canal", "canal intermittent")):
                regular = self._width(regular_name, [label])
                intermittent = self._width(intermittent_name, [label])
                self.assertLess(intermittent, regular, (label, regular_name))
        self.assertAlmostEqual(
            self._width("river intermittent", ["100000 - 300000"]), 0.22)
        self.assertAlmostEqual(
            self._width("river intermittent", ["300000 - 0"]), 0.17)


class HillshadeVariantsTest(unittest.TestCase):
    """Два варианта отмывки: светлый для равнин и текущий для гор."""

    VAR1 = "[TERRAIN] hillshade var1"
    VAR2 = "[TERRAIN] hillshade var2"

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in cls.root.iter("maplayer")
            if (layer.findtext("layername") or "").startswith(
                "[TERRAIN] hillshade"
            )
        }
        cls.ids = {
            name: layer.findtext("id")
            for name, layer in cls.layers.items()
        }

    def _alphas(self, name):
        ramp = self.layers[name].find(".//colorrampshader")
        return {
            int(item.get("value")): int(item.get("alpha"))
            for item in ramp.findall("item")
        }

    def test_two_variants_share_raster(self):
        self.assertEqual(set(self.layers), {self.VAR1, self.VAR2})
        for layer in self.layers.values():
            self.assertEqual(
                layer.findtext("datasource"),
                "./raster/hillshade_composite.tif",
            )

    def test_var1_is_visible_by_default(self):
        checked = {
            item.get("name"): item.get("checked")
            for item in self.root.iter("layer-tree-layer")
            if item.get("name") in self.layers
        }
        self.assertEqual(checked.get(self.VAR1), "Qt::Checked")
        self.assertEqual(checked.get(self.VAR2), "Qt::Unchecked")

    def test_var1_keeps_lit_slopes(self):
        """Плоскость (190) прозрачна, освещённые склоны — светлый полутон."""
        alphas = self._alphas(self.VAR1)
        self.assertEqual(alphas.get(190), 0)
        self.assertGreater(alphas.get(255, 0), 0)

    def test_var2_keeps_current_style(self):
        alphas = self._alphas(self.VAR2)
        self.assertEqual(alphas.get(245), 5)
        self.assertEqual(alphas.get(255), 0)

    def test_layer_order_contains_both(self):
        order = [
            item.get("id") for item in self.root.find(".//layerorder")
        ]
        for layer_id in self.ids.values():
            self.assertIn(layer_id, order)
        self.assertLess(
            order.index(self.ids[self.VAR1]),
            order.index(self.ids[self.VAR2]),
        )


class AttributionLabelTest(unittest.TestCase):
    """Прямоугольник подписи атрибуции подгоняется по тексту в панели."""

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()
        cls.item = next(
            item for item in cls.root.iter("LayoutItem")
            if "OpenStreetMap" in (item.get("labelText") or "")
        )

    def test_item_background_is_on(self):
        self.assertEqual(self.item.get("background"), "true")

    def test_text_background_is_off(self):
        # Фон текстового формата не используется: нужен прямоугольник
        # самой подписи, который панель сжимает по тексту.
        background = self.item.find("text-style/background")
        self.assertIsNotNone(background)
        self.assertEqual(background.get("shapeDraw"), "0")

    def test_item_is_wide_enough_for_long_attribution(self):
        # Ширина рамки — предел переноса длинной строки; видимую заливку
        # задаёт подгонка по тексту, а не эта ширина.
        self.assertTrue(self.item.get("size").startswith("210,"))

    def test_background_is_white(self):
        color = self.item.find("BackgroundColor")
        self.assertIsNotNone(color)
        for channel in ("red", "green", "blue"):
            self.assertEqual(color.get(channel), "255")


class PoiSuppressionTest(unittest.TestCase):
    """Значки POI подавляются, если рядом более приоритетный объект.

    Подавление задаётся фильтрами правил рендерера и правил подписей
    в шаблоне: служебные слои `poi icons` собираются при подготовке
    проекта, а правила ссылаются на них по имени.
    """

    # У places main десятки листовых правил: overlay_nearest в каждом
    # замедлял отрисовку настолько, что значки не появлялись.  Поэтому
    # у этого слоя подавление отключено, у остальных работает.
    NO_SUPPRESSION_LAYERS = {"places main"}

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()
        cls.layers = poi_icons.read_template_poi()
        cls.bands = poi_icons.canonical_bands(cls.layers)

    def test_service_layers_exist_and_are_hidden(self):
        names = {
            layer.findtext("layername")
            for layer in self.root.iter("maplayer")
        }
        for index in range(len(self.bands)):
            self.assertIn(f"poi icons {index}", names)
        tree = {
            element.get("name")
            for element in self.root.iter("layer-tree-layer")
        }
        # Служебные слои не показываются пользователю: они нужны только
        # для выборки соседей.
        for name in tree:
            self.assertFalse(name.startswith("poi icons"))

    def test_service_layers_are_not_poi_layers(self):
        names = [layer.name for layer in self.layers]
        self.assertFalse([name for name in names if name.startswith("poi icons")])
        self.assertEqual(
            [layer.rank for layer in self.layers],
            list(range(1, len(self.layers) + 1)),
        )

    def test_all_icon_layers_use_one_crs(self):
        """Все слои со значками — в EPSG:4326.

        Расстояние подавления задано в градусах; слой в метрах
        (EPSG:3857) сравнивал бы его с метрами и не находил соседей.
        """
        for layer in self.layers:
            self.assertEqual(layer.crs, "EPSG:4326", layer.name)

    def test_service_layer_names_are_unique_and_referenced(self):
        """Имена служебных слоёв уникальны и существуют в шаблоне.

        Два слоя с одинаковым именем (4326 и 3857) делали выбор
        overlay_nearest неоднозначной: первым находился слой не той
        системы координат, и вытеснение не работало.
        """
        names = [
            layer.findtext("layername") or ""
            for layer in self.root.iter("maplayer")
        ]
        service = [name for name in names if name.startswith("poi icons")]
        self.assertEqual(len(service), len(set(service)), service)
        known = set(names)
        referenced = set()
        for layer in self.root.iter("maplayer"):
            text = ElementTree.tostring(layer, encoding="unicode")
            referenced.update(re.findall(
                r"overlay_nearest\(\s*'(poi icons[^']*)'", text,
            ))
        self.assertTrue(referenced, "в шаблоне нет ссылок на служебные слои")
        for name in referenced:
            self.assertIn(name, known)

    def test_service_layers_have_spatial_index(self):
        """Служебный слой лежит в GeoPackage: у SQLite нет индекса."""
        for layer in self.root.iter("maplayer"):
            name = layer.findtext("layername") or ""
            if not name.startswith("poi icons"):
                continue
            source = layer.findtext("datasource") or ""
            self.assertIn(".gpkg", source)

    def test_icon_rules_suppress_overlaps(self):
        checked = 0
        for layer in self.layers:
            if layer.name in self.NO_SUPPRESSION_LAYERS:
                continue
            for leaf in poi_icons.leaves(layer.root):
                if not poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                ):
                    # Правило не рисуется ни в одном диапазоне.
                    continue
                self.assertIn(
                    "overlay_nearest(", leaf.filter,
                    f"у слоя {layer.name} нет подавления значков",
                )
                self.assertIn("@element[2] <", leaf.filter)
                self.assertIn("@level_of_detail", leaf.filter)
                checked += 1
        self.assertTrue(checked, "в шаблоне нет правил значков POI")

    def test_icon_rules_break_ties_within_a_layer(self):
        """Равный ранг не вытеснял: значки одного слоя накладывались.

        Фильтр должен скрывать точку, если рядом стоит точка того же
        слоя (тот же ранг) с меньшими координатами — она побеждает.
        """
        checked = 0
        for layer in self.layers:
            if layer.name in self.NO_SUPPRESSION_LAYERS:
                continue
            for leaf in poi_icons.leaves(layer.root):
                if not poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                ):
                    continue
                self.assertIn('@element[2] =', leaf.filter)
                self.assertIn("with_variable('pg'", leaf.filter)
                checked += 1
        self.assertTrue(checked)

    def test_icon_rules_pick_scale_outside_overlay(self):
        """Текущий диапазон выбирается снаружи overlay_nearest.

        Аргументы overlay_nearest считаются в контексте служебного
        слоя, где @map_scale недоступен.  Поэтому диапазон выбирается
        условием правила, а в выборку соседей попадает слой нужного
        диапазона.
        """
        for layer in self.layers:
            if layer.name in self.NO_SUPPRESSION_LAYERS:
                continue
            for leaf in poi_icons.leaves(layer.root):
                if not poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                ):
                    continue
                self.assertIn("@map_scale", leaf.filter)
                inside = leaf.filter.split("overlay_nearest", 1)[1]
                self.assertIn("'poi icons ", inside)

    def test_places_main_suppression_is_disabled(self):
        """places main рисуется без overlay_nearest в правилах.

        Десятки листовых правил с overlay_nearest замедляли отрисовку
        слоя настолько, что значки не появлялись.  Остальные слои
        по-прежнему подавляют перекрытия.
        """
        layer = next(
            element for element in self.root.iter("maplayer")
            if element.findtext("layername") == "places main"
        )
        text = ElementTree.tostring(layer, encoding="unicode")
        self.assertNotIn("overlay_nearest(", text)

    def test_label_rules_suppress_overlaps(self):
        checked = 0
        for layer in self.layers:
            if layer.name in self.NO_SUPPRESSION_LAYERS:
                continue
            for rule in layer.labels:
                expected = poi_icons.layer_suppression_expression(
                    layer, self.bands, rule.minimum, rule.maximum,
                )
                if expected == "false":
                    # В этом диапазоне значок не рисуется: подавлять
                    # подписи нечем.
                    continue
                self.assertIn(
                    "overlay_nearest(", rule.filter,
                    f"у слоя {layer.name} нет подавления подписей",
                )
                checked += 1
        self.assertTrue(checked, "в шаблоне нет подписей POI")


class PoiPruneTest(unittest.TestCase):
    """Служебный слой хранит победителя кластера того же слоя."""

    def _point(self, lon, lat, rank, band=0):
        from qgis_xtopo.core.poi_icons import _Point

        return _Point(
            rank=rank,
            lon=lon,
            lat=lat,
            bands={band: 3},
            sizes={band: 2.8},
        )

    def _kept(self, points, scale=178571.0):
        from qgis_xtopo.core.poi_icons import _prune_band

        return _prune_band(points, 0, scale, 46.0)

    def test_same_layer_cluster_keeps_winner(self):
        west = self._point(6.9, 46.05, rank=5)
        east = self._point(6.9005, 46.05, rank=5)
        kept = self._kept([west, east])
        self.assertIn(west, kept)
        self.assertNotIn(east, kept)

    def test_same_layer_tie_breaks_by_latitude(self):
        south = self._point(6.9, 46.05, rank=5)
        north = self._point(6.9, 46.0505, rank=5)
        kept = self._kept([north, south])
        self.assertIn(south, kept)
        self.assertNotIn(north, kept)

    def test_far_apart_same_layer_dropped(self):
        west = self._point(6.9, 46.05, rank=5)
        east = self._point(6.94, 46.05, rank=5)
        self.assertEqual(self._kept([west, east]), [])

    def test_lower_priority_still_suppresses(self):
        top = self._point(6.9, 46.05, rank=3)
        low = self._point(6.9005, 46.05, rank=9)
        kept = self._kept([top, low])
        # Победитель хранится, проигравший в служебном слое не нужен:
        # подавлять им некого.
        self.assertIn(top, kept)
        self.assertNotIn(low, kept)

    def test_obstacle_suppresses_nearby_point(self):
        bridge = self._point(6.9, 46.05, rank=0.02)
        shelter = self._point(6.9002, 46.05, rank=8)
        kept = self._kept([bridge, shelter])
        self.assertIn(bridge, kept)
        self.assertNotIn(shelter, kept)

    def test_obstacle_without_points_is_dropped(self):
        bridge = self._point(6.9, 46.05, rank=0.02)
        far = self._point(6.94, 46.05, rank=8)
        self.assertEqual(self._kept([bridge, far]), [])


class PoiObstacleTest(unittest.TestCase):
    """Значки мостов и кладбищ участвуют в вытеснении как препятствия."""

    def test_bridge_and_cemetery_layers(self):
        obstacles = poi_icons.read_template_obstacles()
        by_name = {layer.name: layer for layer in obstacles}
        for name in ("highway main bridge", "path bridge",
                     "railway bridge", "track bridge", "cemetery"):
            self.assertIn(name, by_name)
            layer = by_name[name]
            # Выше любого POI (у них ранги с 1) и уникальны.
            self.assertLess(layer.rank, 1.0)
            self.assertGreater(layer.icon_size, 0.0)
            self.assertEqual(layer.crs, "EPSG:4326")
        self.assertEqual(by_name["cemetery"].geometry, "Polygon")
        self.assertEqual(by_name["path bridge"].geometry, "Line")
        self.assertEqual(
            len({layer.rank for layer in obstacles}), len(obstacles),
        )

    def test_anchor_of_line_is_middle(self):
        from qgis.core import QgsGeometry, QgsPointXY

        line = QgsGeometry.fromPolylineXY(
            [QgsPointXY(0, 0), QgsPointXY(2, 0)]
        )
        anchor = poi_icons._icon_anchor(line, "Line")
        self.assertIsNotNone(anchor)
        self.assertAlmostEqual(anchor.x(), 1.0)
        self.assertAlmostEqual(anchor.y(), 0.0)

    def test_anchor_of_polygon_is_inside(self):
        from qgis.core import QgsGeometry, QgsPointXY

        polygon = QgsGeometry.fromPolygonXY([[
            QgsPointXY(0, 0), QgsPointXY(2, 0), QgsPointXY(2, 2),
            QgsPointXY(0, 2), QgsPointXY(0, 0),
        ]])
        anchor = poi_icons._icon_anchor(polygon, "Polygon")
        self.assertIsNotNone(anchor)
        self.assertTrue(polygon.contains(
            QgsGeometry.fromPointXY(anchor)
        ))




class BridgeIconSuppressionTest(unittest.TestCase):
    """Значки мостов одного слоя вытесняют друг друга.

    Значок рисуется MarkerLine в середине линии, поэтому фильтр правила
    скрыл бы весь мост.  Вместо фильтра вложенному маркеру задан
    data-defined размер: поле icon_hide перечисляет диапазоны, где рядом
    есть мост того же слоя с меньшими координатами.
    """

    LAYERS = ("highway main bridge", "track bridge",
              "path bridge", "railway bridge")

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    @staticmethod
    def _find(node, path):
        for rule in node.findall("rule"):
            title = rule.get("description") or rule.get("label")
            if title == path[0]:
                if len(path) == 1:
                    return rule
                return BridgeIconSuppressionTest._find(rule, path[1:])
        return None

    @staticmethod
    def _active_symbols(layer):
        renderer = layer.find("renderer-v2")
        symbols = {
            symbol.get("name"): symbol
            for symbol in renderer.findall("symbols/symbol")
        }
        usage = {}

        def walk(node, inherited_min, inherited_max):
            lower = float(node.get("scalemindenom") or 0)
            upper = float(node.get("scalemaxdenom") or 0)
            if lower <= 1.0:
                lower = 0.0
            effective_min = max(inherited_min, lower)
            if inherited_max and upper:
                effective_max = min(inherited_max, upper)
            else:
                effective_max = inherited_max or upper
            children = node.findall("rule")
            if not children:
                symbol_name = node.get("symbol")
                if symbol_name:
                    usage.setdefault(symbol_name, []).append(
                        (effective_min, effective_max))
            for child in children:
                if child.get("checkstate") == "0":
                    continue
                walk(child, effective_min, effective_max)

        for node in renderer.find("rules").findall("rule"):
            if node.get("checkstate") == "0":
                continue
            walk(node, 0.0, 0.0)
        return symbols, usage

    def test_bridge_markers_have_same_layer_suppression(self):
        checked = 0
        for name in self.LAYERS:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            symbols, usage = self._active_symbols(layer)
            for symbol_name in usage:
                symbol = symbols.get(symbol_name)
                if symbol is None:
                    continue
                markers = []
                has_line = False
                for marker in symbol.iter("layer"):
                    if marker.get("class") == "SimpleLine":
                        has_line = True
                    if marker.get("class") != "SvgMarker":
                        continue
                    options = {
                        option.get("name"): option.get("value")
                        for option in marker.findall("Option/Option")
                    }
                    if "bridge" in (options.get("name") or ""):
                        markers.append((marker, options))
                if has_line:
                    # Значок должен быть в отдельном правиле: иначе его
                    # нельзя отключить, не скрыв линию моста.
                    self.assertNotIn(
                        "bridge.svg",
                        ElementTree.tostring(symbol, encoding="unicode"),
                        f"{name}/{symbol_name}",
                    )
                for marker, _options in markers:
                    marker_line = next(
                        candidate for candidate in symbol.iter("layer")
                        if candidate.get("class") == "MarkerLine"
                        and marker in candidate.iter("layer")
                    )
                    with self.subTest(layer=name, symbol=symbol_name):
                        properties = marker_line.find(
                            'data_defined_properties/Option/'
                            'Option[@name="properties"]'
                        )
                        self.assertIsNotNone(properties)
                        enabled = properties.find('Option[@name="enabled"]')
                        self.assertIsNotNone(enabled)
                        active = enabled.find('Option[@name="active"]')
                        self.assertEqual(active.get("value"), "true")
                        expression = enabled.find(
                            'Option[@name="expression"]'
                        ).get("value")
                        self.assertIn(
                            'string_to_array("icon_hide", \';\')', expression
                        )
                        self.assertIn("false, true", expression)
                        self.assertNotIn("overlay_nearest", expression)
                        self.assertNotIn("@map_scale", expression)
                        index = re.search(r"'(\d+)'\), false", expression)
                        self.assertIsNotNone(index)
                        self.assertEqual(
                            expression,
                            poi_icons.icon_enabled_expression(
                                int(index.group(1))),
                        )
                        checked += 1
        self.assertTrue(checked)

    def test_service_bridge_icon_stops_at_200k(self):
        layer = self.layers["highway main bridge"]
        leaf = self._find(
            layer.find("renderer-v2/rules"),
            ["service", "else", "200000 - 300000"],
        )
        self.assertIsNotNone(leaf)
        symbol = next(
            symbol for symbol in layer.findall("renderer-v2/symbols/symbol")
            if symbol.get("name") == leaf.get("symbol")
        )
        self.assertNotIn("bridge.svg",
                         ElementTree.tostring(symbol, encoding="unicode"))
        self.assertGreaterEqual(len(symbol.findall("layer")), 2)
        self.assertTrue(all(
            item.get("class") == "SimpleLine"
            for item in symbol.findall("layer")
        ))

    def test_service_bridge_icon_stays_below_200k(self):
        layer = self.layers["highway main bridge"]
        service = self._find(
            layer.find("renderer-v2/rules"), ["service", "else"],
        )
        self.assertIsNotNone(service)
        icon_rules = []
        for rule in service.findall("rule"):
            symbol = next(
                (symbol for symbol in layer.findall(
                    "renderer-v2/symbols/symbol")
                 if symbol.get("name") == rule.get("symbol")), None)
            if symbol is None:
                continue
            if "bridge.svg" in ElementTree.tostring(
                    symbol, encoding="unicode"):
                icon_rules.append(rule)
        self.assertTrue(icon_rules, "у service-мостов нет значка ниже 200k")
        for rule in icon_rules:
            maximum = rule.get("scalemaxdenom")
            self.assertTrue(maximum)
            self.assertLessEqual(int(maximum), 200000)
        self.assertFalse([
            rule for rule in icon_rules
            if int(rule.get("scalemindenom") or 0) >= 200000
        ])


class BridgeIconHideTest(unittest.TestCase):
    """Поле icon_hide и выражение видимости значка моста."""

    @staticmethod
    def _point(lon, lat, name="highway main bridge", fid=1):
        return poi_icons._Point(
            rank=0.02, lon=lon, lat=lat, name=name, fid=fid,
            geometry="Line", bands={0: 4}, sizes={0: 2.0},
        )

    def test_expression_uses_icon_hide(self):
        expression = poi_icons.icon_enabled_expression(4)
        self.assertIn('string_to_array("icon_hide", \';\')', expression)
        self.assertIn("'4'", expression)
        self.assertIn("coalesce(", expression)
        self.assertIn("false, true", expression)
        self.assertNotIn("@map_scale", expression)
        self.assertNotIn("overlay_nearest", expression)

    def test_same_layer_tie_breaks_by_coordinates(self):
        west = self._point(6.9000, 46.05, fid=1)
        east = self._point(6.9006, 46.05, fid=2)
        hidden = poi_icons._hidden_bridge_bands(
            [west, east], [(100000.0, 130000.0)],
        )
        self.assertNotIn(("highway main bridge", 1), hidden)
        self.assertIn(("highway main bridge", 2), hidden)

    def test_other_layer_does_not_suppress(self):
        bridge = self._point(6.9000, 46.05, fid=1)
        track = self._point(6.9006, 46.05, name="track bridge", fid=2)
        hidden = poi_icons._hidden_bridge_bands(
            [bridge, track], [(100000.0, 130000.0)],
        )
        self.assertFalse(hidden)

    def test_field_is_written_to_bridge_layer(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from osgeo import ogr, osr

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vector = root / "vector"
            vector.mkdir()
            path = vector / "bridges.sqlite"
            dataset = ogr.GetDriverByName("SQLite").CreateDataSource(
                str(path))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            layer = dataset.CreateLayer("bridges", srs, ogr.wkbLineString)
            for lon in (6.9000, 6.9006):
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetGeometry(ogr.CreateGeometryFromWkt(
                    f"LINESTRING ({lon} 46.05, {lon} 46.0502)"))
                layer.CreateFeature(feature)
            dataset = None

            source = poi_icons.PoiLayer(
                name="highway main bridge",
                source="./vector/bridges.sqlite",
                geometry="Line",
            )
            config = SimpleNamespace(
                project_dir=str(root), vector_dir=str(vector))
            poi_icons._write_icon_hide(
                config, [source],
                [self._point(6.9000, 46.05, fid=1),
                 self._point(6.9006, 46.05, fid=2)],
                [(100000.0, 130000.0)],
            )
            dataset = ogr.Open(str(path))
            layer = dataset.GetLayer(0)
            values = {
                feature.GetFID(): feature.GetField("icon_hide")
                for feature in layer
            }
            dataset = None
            self.assertIsNone(values[1])
            self.assertEqual(values[2], "0")

            # Повторная подготовка не оставляет старых значений.
            poi_icons._write_icon_hide(
                config, [source], [self._point(6.9000, 46.05, fid=1)],
                [(100000.0, 130000.0)],
            )
            dataset = ogr.Open(str(path))
            layer = dataset.GetLayer(0)
            self.assertFalse([
                value for value in (
                    feature.GetField("icon_hide") for feature in layer
                ) if value
            ])
            dataset = None


class PoiSuppressionExpressionTest(unittest.TestCase):
    """Выражение подавления вытесняет значки и внутри слоя.

    Прежняя схема ссылалась на geometry(@parent) внутри overlay_nearest,
    но @parent в его аргументах недоступен (NULL): значки одного слоя не
    вытесняли друг друга.  Теперь родительская точка передаётся через
    with_variable, кандидаты возвращаются массивами, а расстояние
    считается в метрах.
    """

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        layers = poi_icons.read_template_poi()
        cls.bands = poi_icons.canonical_bands(layers)
        cls.layer = next(layer for layer in layers if layer.name == "places main")
        cls.leaf = next(
            leaf for leaf in poi_icons.leaves(cls.layer.root)
            if leaf.minimum == 0 and leaf.maximum == 100000
        )
        cls.expression = poi_icons.extend_filter(
            "", cls.layer, cls.leaf, cls.bands,
        )

    def _feature(self, layer, lon, lat, values):
        from qgis.core import QgsFeature, QgsGeometry, QgsPointXY

        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        for name, value in values.items():
            feature[name] = value
        return feature

    def _visible(self, aux_points, parent_points, scale=10000.0):
        from qgis.core import (
            QgsExpression, QgsExpressionContext, QgsExpressionContextScope,
            QgsExpressionContextUtils, QgsField, QgsProject, QgsVectorLayer,
        )
        from qgis.PyQt.QtCore import QVariant

        project = QgsProject.instance()
        project.clear()
        project.setCustomVariables({"level_of_detail": "4"})
        aux = QgsVectorLayer("Point?crs=EPSG:4326", "poi icons 0", "memory")
        aux.dataProvider().addAttributes([
            QgsField("rank", QVariant.Int),
            QgsField("lod", QVariant.Int),
        ])
        aux.updateFields()
        aux.dataProvider().addFeatures([
            self._feature(aux, lon, lat, {"rank": rank, "lod": 3})
            for lon, lat, rank in aux_points
        ])
        parent = QgsVectorLayer("Point?crs=EPSG:4326", "places main", "memory")
        parent.dataProvider().addAttributes([QgsField("name", QVariant.String)])
        parent.updateFields()
        parent.dataProvider().addFeatures([
            self._feature(parent, lon, lat, {"name": name})
            for lon, lat, name in parent_points
        ])
        project.addMapLayer(aux)
        project.addMapLayer(parent)
        scope = QgsExpressionContextScope("map")
        scope.setVariable("map_scale", scale)
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.globalScope())
        context.appendScope(QgsExpressionContextUtils.projectScope(project))
        context.appendScope(QgsExpressionContextUtils.layerScope(parent))
        context.appendScope(scope)
        expression = QgsExpression(self.expression)
        self.assertFalse(expression.hasParserError(), expression.parserErrorString())
        result = {}
        for feature in parent.getFeatures():
            context.setFeature(feature)
            result[feature["name"]] = bool(expression.evaluate(context))
        self.assertFalse(expression.evalErrorString())
        return result

    def test_same_layer_tie_break_prefers_western_icon(self):
        visible = self._visible(
            [(6.9000, 46.05, 6), (6.9001, 46.05, 6)],
            [(6.9000, 46.05, "west"), (6.9001, 46.05, "east")],
        )
        self.assertTrue(visible["west"])
        self.assertFalse(visible["east"])

    def test_higher_priority_icon_hides_both(self):
        visible = self._visible(
            [(6.90003, 46.05, 3)],
            [(6.9000, 46.05, "west"), (6.9001, 46.05, "east")],
        )
        self.assertFalse(visible["west"])
        self.assertFalse(visible["east"])

    def test_lonely_icon_stays_visible(self):
        visible = self._visible(
            [(6.9000, 46.05, 6)],
            [(6.9200, 46.05, "far")],
        )
        self.assertTrue(visible["far"])


class MountainPassTemplateTest(unittest.TestCase):
    """Подписи перевалов получают признак местных названий из шаблона."""

    def test_pass_calls_receive_name_local(self):
        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        needle = "compose_mountain_pass_name("
        calls = []
        index = 0
        while True:
            start = text.find(needle, index)
            if start < 0:
                break
            pos = start + len(needle)
            depth = 1
            while depth:
                char = text[pos]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                pos += 1
            calls.append(text[start:pos])
            index = pos
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("@name_local", call)


if __name__ == "__main__":
    unittest.main()
