"""Инварианты шаблона проекта.

Стили задаются только в шаблоне (`automap.qgz`): ни инструментов, которые
правят шаблон, ни механизма починки готовых проектов в плагине нет.
Тесты проверяют, что нужные свойства в шаблоне есть — иначе карта будет
нарисована не так, как задумано.
"""

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
                self.assertIn('"rank" <', leaf.filter)
                self.assertIn("@level_of_detail", leaf.filter)
                checked += 1
        self.assertTrue(checked, "в шаблоне нет правил значков POI")

    def test_icon_rules_pick_scale_outside_overlay(self):
        """Текущий диапазон выбирается снаружи overlay_nearest.

        Аргументы overlay_nearest считаются в контексте служебного
        слоя, где @map_scale недоступен.  Поэтому диапазон выбирается
        условием правила, а в выборку соседей попадает слой нужного
        диапазона.
        """
        for layer in self.layers:
            for leaf in poi_icons.leaves(layer.root):
                if not poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                ):
                    continue
                self.assertIn("@map_scale", leaf.filter)
                inside = leaf.filter.split("overlay_nearest", 1)[1]
                self.assertIn("'poi icons ", inside)

    def test_label_rules_suppress_overlaps(self):
        checked = 0
        for layer in self.layers:
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


if __name__ == "__main__":
    unittest.main()
