"""Инварианты шаблона проекта.

Стили задаются только в шаблоне (`automap.qgz`); готовые проекты не
чинятся, поэтому проверяем нужные свойства прямо в шаблоне — иначе карта
будет нарисована не так, как задумано. Правки шаблона вносят скрипты в
`tools/`.
"""

import re
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from qgis_xtopo.core import poi_icons
from qgis_xtopo.core.area_field import (
    AREA_LAYERS, LABEL_AREA_FIELD, LABEL_AREA_LAYERS,
)
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
ISOLINES_REGULAR_OVERVIEW = "[TERRAIN] isolines regular overview"
ISOLINES_GLACIER_OVERVIEW = "[TERRAIN] isolines glacier overview"
DD_PROPERTY = "offsetAlongLine"


_TEMPLATE_ROOT = []


def _template_root():
    """Разобранный шаблон: XML в нём на десятки мегабайт, читать его
    заново в каждом классе слишком долго."""
    if not _TEMPLATE_ROOT:
        with zipfile.ZipFile(template_path()) as archive:
            _TEMPLATE_ROOT.append(
                ElementTree.fromstring(archive.read("automap.qgs"))
            )
    return _TEMPLATE_ROOT[0]


def _base_filter(filter_text):
    """Фильтр правила без условия подавления значка.

    Подавление добавлено в конец фильтра: либо `NOT (...)`, либо
    `(основа) AND NOT (...)`.  Перед ним может стоять проверка полос
    масштаба, добавленная для границ диапазонов.
    """
    text = filter_text or ""
    position = text.rfind("NOT (")
    if position < 0:
        return text
    prefix = text[:position].rstrip()
    if prefix.endswith("AND"):
        prefix = prefix[:-3].rstrip()
    term = (
        r"\(@map_scale (?:>=|<) [0-9.]+"
        r"(?: AND @map_scale (?:>=|<) [0-9.]+)?\)"
    )
    guard = rf"(?:\({term}(?: OR {term})+\)|{term})"
    match = re.search(rf"(?:^| AND ){guard}$", prefix)
    if match:
        prefix = prefix[:match.start()].rstrip()
    if prefix.startswith("(") and prefix.endswith(")"):
        return prefix[1:-1]
    return prefix


def _find_option(element, name):
    """Первое свойство с указанным именем (в QGIS свойства лежат в картах)."""
    if element is None:
        return None
    for option in element.iter("Option"):
        if option.get("name") == name:
            return option
    return None


def _integrity_layers(element):
    """symbol layer, цвет которых считается по highway_integrity.

    Это поверхность дороги Highway main: её маска значка не режет, иначе
    дорога под значком разрывается.
    """
    result = set()
    for symbol_layer in element.iter("layer"):
        symbol_layer_id = symbol_layer.get("id")
        if not symbol_layer_id:
            continue
        properties = symbol_layer.find("data_defined_properties")
        if properties is None:
            continue
        if "highway_integrity" in ElementTree.tostring(
                properties, encoding="unicode"):
            result.add(symbol_layer_id)
    return result


def _options(element):
    """Свойства symbol layer в виде словаря."""
    return {
        option.get("name"): option.get("value")
        for option in element.findall("Option/Option")
    }


def _pedestrian_surface_layers(renderer):
    """symbol layer поверхности пешеходных дорог.

    У правила ``pedestrian`` символ двухслойный: первый symbol layer —
    тёмная обводка, остальные — серая поверхность.  Маска режет только
    обводку, поверхность остаётся: иначе в полосе пешеходной дороги под
    значком появляется светлый разрыв.
    """
    symbols = set()
    for rule in renderer.iter("rule"):
        text = rule.get("filter") or ""
        if ("highway = 'pedestrian'" not in text
                and "highway='pedestrian'" not in text):
            continue
        for child in rule.iter("rule"):
            if child.get("symbol"):
                symbols.add(child.get("symbol"))
        if rule.get("symbol"):
            symbols.add(rule.get("symbol"))
    result = set()
    for name in symbols:
        symbol = renderer.find(f"symbols/symbol[@name='{name}']")
        if symbol is None:
            continue
        layers = [
            item for item in symbol.findall("layer") if item.get("id")
        ]
        result |= {item.get("id") for item in layers[1:]}
    return result


def _icon_masks():
    """Файлы масок значков и их размер в шаблоне.

    Возвращает словарь ``имя файла -> размер значка, мм``.  Размер
    нужен, чтобы пересчитать отступ маски: он считается от размера
    значка, с которым тот рисуется.
    """
    sizes = {}
    for maplayer in _template_root().iter("maplayer"):
        for symbol in maplayer.iter("symbol"):
            for layer in symbol.findall("layer"):
                if layer.get("class") != "MaskMarker":
                    continue
                sub = layer.find("symbol")
                if sub is None:
                    continue
                marker = next(
                    (item for item in sub.findall("layer")
                     if item.get("class") == "SvgMarker"),
                    None,
                )
                if marker is None:
                    continue
                options = _options(marker)
                name = Path(options.get("name") or "").name
                if not name:
                    continue
                mask_file = (
                    name if name.endswith("_m.svg")
                    else Path(name).stem + "_m.svg"
                )
                size = float(options.get("size") or 0)
                if size:
                    sizes[mask_file] = max(sizes.get(mask_file, 0.0), size)
    return sizes


def _mask_paths(root):
    """Контуры маски: основа из <defs> и сама маска."""
    base = ""
    data = ""
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "path":
            continue
        if element.get("id") == "base":
            base = element.get("d") or ""
        elif not data:
            data = element.get("d") or ""
    return base, data


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


class AtlasNumberLabelTest(unittest.TestCase):
    """Номер листа печатается только в многостраничном атласе."""

    def test_number_hidden_for_single_sheet(self):
        labels = [
            item.get("labelText", "")
            for item in _template_root().iter("LayoutItem")
            if item.get("type") == "65641"
            and "@atlas_featurenumber" in item.get("labelText", "")
        ]
        self.assertEqual(len(labels), 1)
        self.assertIn("@atlas_totalfeatures", labels[0])
        self.assertIn(">1", labels[0])


class TemplateFilterExpressionTest(unittest.TestCase):
    """Выражения шаблона — валидные выражения QGIS.

    Проверяются не только фильтры правил, но и подписи, выражения полей
    и data-defined свойства: ошибка в любом из них ломает отрисовку или
    вычисление атрибута.  При запекании фильтров подавления в шаблон
    HTML-сущности однажды экранировали дважды: в XML осталось
    ``&amp;lt;``, QGIS читал ``&lt;``, и правило с таким фильтром не
    срабатывало.
    """

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app
        from qgis_xtopo.core.expressions import load_expressions

        cls.app = qgis_app()
        load_expressions()
        cls.root = _template_root()

    def _expressions(self):
        extra_attributes = (
            "pageNameExpression",
            "filterFeatures",
            "annotationExpression",
            "startExpression",
            "endExpression",
            "sortExpression",
            "classificationAttributeExpression",
        )
        for element in self.root.iter():
            if element.tag == "rule":
                value = element.get("filter")
                if value and value.strip().upper() != "ELSE":
                    yield element, "filter", value
            if element.tag == "text-style" and element.get("isExpression") == "1":
                value = element.get("fieldName")
                if value:
                    yield element, "fieldName", value
            if element.tag == "previewExpression":
                value = "".join(element.itertext())
                if value.strip():
                    yield element, "text", value
            if element.tag == "Option" and element.get("name") == "expression":
                value = element.get("value")
                if value:
                    yield element, "expression", value
            if element.tag == "field" and element.get("expression"):
                yield element, "expression", element.get("expression")
            if element.tag == "constraint" and element.get("exp"):
                yield element, "exp", element.get("exp")
            for attribute in extra_attributes:
                value = element.get(attribute)
                if value:
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


class DemLayerTest(unittest.TestCase):
    """Высота перевала считается по DEM в градусах, как и координаты слоя."""

    def test_no_projected_dem_layer(self):
        names = [
            layer.findtext("layername")
            for layer in _template_root().iter("maplayer")
        ]
        self.assertIn("merged_dem", names)
        self.assertNotIn("merged_dem_3857", names)

    def test_mountain_pass_has_single_elevation_expression(self):
        fields = [
            field.get("expression") or ""
            for field in _template_root().iter("field")
        ]
        self.assertFalse(
            any("merged_dem_3857" in expression for expression in fields)
        )
        for layer in _template_root().iter("maplayer"):
            if layer.findtext("layername") != "mountain pass":
                continue
            blocks = layer.findall("expressionfields")
            self.assertEqual(len(blocks), 1)
            elevation = [
                field.get("expression") or ""
                for field in blocks[0].findall("field")
                if field.get("name") == "ele_calc"
            ]
            self.assertEqual(len(elevation), 1)
            self.assertIn("raster_value('merged_dem'", elevation[0])
            break
        else:
            self.fail("mountain pass layer not found")


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


class AdminBoundaryOrderTest(unittest.TestCase):
    """Границы admin_level и их подписи выше воды.

    Иначе вода закрывает линии границ, а подписи QGIS всё равно рисует
    поверх — они остаются без линии и выглядят висящими в воздухе.
    """

    ADMIN = (
        "admin level 2 (country borders)",
        "admin level 2 (country names)",
        "admin level 4 (regions)",
        "admin level 4 (region names)",
    )

    @classmethod
    def setUpClass(cls):
        root = _template_root()
        cls.order = [
            item.get("name")
            for item in root.iter("layer-tree-layer")
        ]
        cls.water = []
        for group in root.iter("layer-tree-group"):
            if group.get("name") != "water":
                continue
            cls.water = [
                item.get("name")
                for item in group.iter("layer-tree-layer")
            ]
            break

    def test_admin_boundaries_are_above_water(self):
        self.assertTrue(self.water, "в шаблоне нет группы water")
        for admin in self.ADMIN:
            for water in self.water:
                self.assertLess(
                    self.order.index(admin), self.order.index(water),
                    f"{admin} должен быть выше {water}",
                )


class OpenBorderAdminLabelTest(unittest.TestCase):
    """Названия на открытых границах ставятся вдоль линии границы.

    Открытая граница превращается в тонкую полосу, в которую обычная
    подпись с `fitInPolygonOnly=1` не помещается. Отдельная ориентированная
    линия `label_line` позволяет поставить текст вдоль границы с нужной
    стороны: линия направлена так, чтобы страна или регион были слева.
    Подпись повторяется вдоль линии, иначе одна подпись теряется за
    пределами вида. Приоритет выше, чем у подписей POI и дорог: иначе
    название страны проигрывает коллизию в заселённых местах. Поля
    `maritime` и `open_border` могут отсутствовать в слое, поэтому
    фильтры читают их через `attribute()`: прямое имя поля даёт ошибку и
    скрывает все подписи.
    """

    LAYERS = (
        "admin level 2 (country names)",
        "admin level 4 (region names)",
    )
    LABEL_OPTIONS = {
        "admin level 2 (country names)": ("10", "160"),
        "admin level 4 (region names)": ("8", "320"),
    }
    AREA_REPEAT = {
        "admin level 2 (country names)": "400",
        "admin level 4 (region names)": "800",
    }

    @classmethod
    def setUpClass(cls):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        cls.rules = {
            name: layers[name].find("labeling/rules").findall("rule")
            for name in cls.LAYERS
        }

    def test_open_border_label_uses_curved_line_placement(self):
        for name, rules in self.rules.items():
            open_rules = [
                rule for rule in rules
                if "attribute('open_border') = 'yes'"
                in (rule.get("filter") or "")
            ]
            self.assertEqual(len(open_rules), 2, name)
            priority, repeat = self.LABEL_OPTIONS[name]
            by_scale = sorted(
                open_rules, key=lambda rule: int(rule.get("scalemaxdenom"))
            )
            low, large = by_scale
            self.assertEqual(large.get("scalemindenom"), "1", name)
            self.assertEqual(large.get("scalemaxdenom"), "100000", name)
            self.assertEqual(low.get("scalemindenom"), "100000", name)
            self.assertEqual(low.get("scalemaxdenom"), "0", name)
            for rule in open_rules:
                self.assertNotIn(
                    "attribute('maritime') = 'yes'",
                    rule.get("filter") or "", name,
                )
                placement = rule.find("settings/placement")
                self.assertIsNotNone(placement, name)
                self.assertEqual(placement.get("fitInPolygonOnly"), "0", name)
                self.assertEqual(placement.get("placement"), "3", name)
                self.assertEqual(placement.get("placementFlags"), "2", name)
                self.assertEqual(
                    placement.get("geometryGeneratorEnabled"), "1", name
                )
                self.assertIn(
                    "label_line",
                    placement.get("geometryGenerator") or "", name,
                )
                self.assertEqual(
                    placement.get("geometryGeneratorType"),
                    "LineGeometry", name,
                )
                self.assertEqual(
                    placement.get("layerType"), "LineGeometry", name
                )
                self.assertEqual(placement.get("priority"), priority, name)
                self.assertEqual(
                    placement.get("repeatDistanceUnits"), "MM", name
                )
            large_rendering = large.find("settings/rendering")
            self.assertEqual(
                large_rendering.get("labelPerPart"), "1", name
            )
            self.assertEqual(
                large.find("settings/placement").get("repeatDistance"),
                "0", name,
            )
            low_rendering = low.find("settings/rendering")
            self.assertEqual(
                low_rendering.get("labelPerPart"), "0", name
            )
            self.assertEqual(
                low.find("settings/placement").get("repeatDistance"),
                repeat, name,
            )
            for rule in rules:
                placement = rule.find("settings/placement")
                self.assertIsNotNone(placement, name)
                if rule.get("description") == "open border":
                    self.assertEqual(
                        placement.get("repeatDistance"), "0", name
                    )
                    continue
                expected = (
                    repeat
                    if rule.get("description") == "open border low zoom"
                    else self.AREA_REPEAT[name]
                )
                self.assertEqual(
                    placement.get("repeatDistance"), expected,
                    f"{name}: {rule.get('description')}",
                )
                self.assertEqual(
                    placement.get("repeatDistanceUnits"), "MM", name
                )
            maritime_rule = next(
                other for other in rules
                if "attribute('maritime') = 'yes'"
                in (other.get("filter") or "")
            )
            for other in rules:
                if other is maritime_rule or any(
                    other is rule for rule in open_rules
                ):
                    continue
                self.assertIn(
                    "coalesce(attribute('maritime'), '') != 'yes'",
                    other.get("filter") or "", name,
                )
                self.assertIn(
                    "coalesce(attribute('open_border'), '') != 'yes'",
                    other.get("filter") or "", name,
                )

    def test_admin_labels_keep_priority_above_poi_labels(self):
        """Названия стран и открытых границ не скрываются подписями POI.

        У регионов обычные площадные правила оставлены с прежним
        приоритетом: они подписывают контуры внутри области, и повышение
        приоритета делает их навязчивыми. Для открытых и морских границ
        приоритет выше, чем у POI и дорог.
        """
        for name, rules in self.rules.items():
            priority, _ = self.LABEL_OPTIONS[name]
            for rule in rules:
                if (
                    name == "admin level 4 (region names)"
                    and rule.get("description") not in ("open border", "maritime")
                ):
                    continue
                placement = rule.find("settings/placement")
                self.assertIsNotNone(placement, name)
                self.assertEqual(
                    placement.get("priority"), priority, name
                )

    def test_maritime_label_rule_keeps_polygon_placement(self):
        for name, rules in self.rules.items():
            maritime_rules = [
                rule for rule in rules
                if "attribute('maritime') = 'yes'"
                in (rule.get("filter") or "")
            ]
            self.assertEqual(len(maritime_rules), 1, name)
            rule = maritime_rules[0]
            self.assertIsNone(rule.get("scalemindenom"), name)
            self.assertIsNone(rule.get("scalemaxdenom"), name)
            placement = rule.find("settings/placement")
            self.assertIsNotNone(placement, name)
            self.assertEqual(placement.get("placement"), "7", name)
            self.assertEqual(placement.get("placementFlags"), "2", name)
            self.assertEqual(placement.get("fitInPolygonOnly"), "0", name)
            self.assertEqual(
                placement.get("geometryGeneratorEnabled"), "0", name
            )

    def test_all_admin_labels_stay_on_the_inner_side(self):
        """Ни одно правило не разрешает подпись с внешней стороны линии."""
        for name, rules in self.rules.items():
            for rule in rules:
                placement = rule.find("settings/placement")
                self.assertIsNotNone(placement, name)
                self.assertEqual(
                    placement.get("placementFlags"), "2",
                    f"{name}: {rule.get('description')}",
                )

    @staticmethod
    def _matches(layer, filter_text, **values):
        """Результат фильтра правила на объекте и ошибка выражения."""
        from qgis.core import (
            QgsExpression, QgsExpressionContext,
            QgsExpressionContextUtils, QgsFeature,
        )

        feature = QgsFeature(layer.fields())
        for name, value in values.items():
            feature.setAttribute(name, value)
        context = QgsExpressionContext()
        context.appendScopes(
            QgsExpressionContextUtils.globalProjectLayerScopes(layer)
        )
        context.setFeature(feature)
        expression = QgsExpression(filter_text)
        return bool(expression.evaluate(context)), expression.evalErrorString()

    def test_label_filters_handle_open_border_and_missing_fields(self):
        """Открытые границы уходят в правило снаружи, обычные — внутрь.

        Отсутствие полей `maritime`/`open_border` не должно давать ошибку
        выражения: иначе QGIS скрывает все подписи слоя.
        """
        from tests import qgis_app
        from qgis.core import QgsField, QgsVectorLayer
        from PyQt5.QtCore import QVariant

        qgis_app()
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "labels", "memory")
        layer.dataProvider().addAttributes([
            QgsField("name", QVariant.String),
            QgsField("maritime", QVariant.String),
            QgsField("open_border", QVariant.String),
        ])
        layer.updateFields()
        bare = QgsVectorLayer("Point?crs=EPSG:4326", "bare", "memory")
        bare.dataProvider().addAttributes([
            QgsField("name", QVariant.String),
        ])
        bare.updateFields()

        for name, rules in self.rules.items():
            open_rules = [
                rule for rule in rules
                if "attribute('open_border') = 'yes'"
                in (rule.get("filter") or "")
            ]
            self.assertEqual(len(open_rules), 2, name)
            open_rule = open_rules[0]
            maritime_rule = next(
                rule for rule in rules
                if "attribute('maritime') = 'yes'"
                in (rule.get("filter") or "")
            )
            normal = next(
                rule for rule in rules
                if rule is not maritime_rule and not any(
                    rule is item for item in open_rules
                )
            )
            normal_filter = normal.get("filter")
            open_filter = open_rule.get("filter")
            maritime_filter = maritime_rule.get("filter")

            match, error = self._matches(layer, normal_filter)
            self.assertTrue(match, name)
            self.assertEqual(error, "", name)
            for label_filter in (open_filter, maritime_filter):
                match, error = self._matches(layer, label_filter)
                self.assertFalse(match, name)
                self.assertEqual(error, "", name)

            match, error = self._matches(
                layer, normal_filter, open_border="yes"
            )
            self.assertFalse(match, name)
            self.assertEqual(error, "", name)
            self.assertTrue(
                self._matches(layer, open_filter, open_border="yes")[0],
                name,
            )
            self.assertFalse(
                self._matches(layer, maritime_filter, open_border="yes")[0],
                name,
            )

            match, error = self._matches(
                layer, normal_filter, maritime="yes"
            )
            self.assertFalse(match, name)
            self.assertEqual(error, "", name)
            self.assertFalse(
                self._matches(layer, open_filter, maritime="yes")[0],
                name,
            )
            self.assertTrue(
                self._matches(layer, maritime_filter, maritime="yes")[0],
                name,
            )

            match, error = self._matches(bare, normal_filter)
            self.assertTrue(match, name)
            self.assertEqual(error, "", name)
            for label_filter in (open_filter, maritime_filter):
                match, error = self._matches(bare, label_filter)
                self.assertFalse(match, name)
                self.assertEqual(error, "", name)


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

    def test_sand_stays_below_water(self):
        # Песок остаётся под водой: пересечение воды и песка вырезается
        # из воды в polygon_overlaps, и отмели видны через дырку.
        self.assertGreater(
            self.order.index("sand"), self.order.index("water"),
            "песок должен оставаться под водой",
        )

    def test_cemetery_stays_below_building(self):
        # Кладбище закрывало здания внутри него: слой опущен под building.
        self.assertGreater(
            self.order.index("cemetery"), self.order.index("building"),
            "кладбище не должно закрывать здания",
        )

    def test_cemetery_icons_stay_above_map(self):
        """Значок кладбища не перекрывают полигоны и линии."""
        icons = self.order.index("cemetery icons")
        for name in ("route_hiking kilometer markers", "atlas template",
                     "mountain area", "camp site polygon", "embankment",
                     "power line", "highway main", "building", "water"):
            self.assertLess(icons, self.order.index(name), name)
        self.assertLess(icons, self.order.index("places main major"))

    def test_wood_and_scrub_are_above_open_land(self):
        for forest in ("wood", "scrub"):
            for open_land in self.OPEN_LAND:
                self.assertLess(
                    self.order.index(forest), self.order.index(open_land),
                    f"{forest} должен быть выше {open_land} (order 7 против 5)",
                )

    def test_mud_stays_above_wetland_and_water(self):
        self.assertLess(self.order.index("mud"), self.order.index("wetland"))
        self.assertLess(self.order.index("mud"), self.order.index("water"))


class MudLayerTest(unittest.TestCase):
    """natural=mud повторяет болото, но с фоном #E3CCB4."""

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()
        cls.layer = next(
            layer for layer in cls.root.iter("maplayer")
            if layer.findtext("layername") == "mud"
        )

    def test_source_is_mud_and_geometry_is_multipolygon(self):
        self.assertEqual(
            self.layer.findtext("datasource"),
            "./vector/mud.sqlite|layerid=0|geometrytype=MultiPolygon",
        )
        self.assertEqual(self.layer.get("wkbType"), "MultiPolygon")
        entries = [
            entry for entry in self.root.iter("layer-tree-layer")
            if entry.get("name") == "mud"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get("id"), self.layer.findtext("id"))
        self.assertEqual(
            entries[0].get("source"),
            "./vector/mud.sqlite|layerid=0|geometrytype=MultiPolygon",
        )

    def test_style_repeats_wetland_with_a_beige_fill(self):
        renderer = self.layer.find("renderer-v2")
        self.assertIsNotNone(renderer)
        self.assertEqual(renderer.get("type"), "RuleRenderer")
        rules = renderer.find("rules").findall("rule")
        self.assertEqual(
            [rule.get("label") for rule in rules],
            ["0 - 50000", "50000 - 100000", "100000 - 300000", "300000 - 0"],
        )
        symbols = renderer.find("symbols").findall("symbol")
        self.assertEqual(len(symbols), 4)
        for symbol in symbols:
            fill = next(
                layer for layer in symbol.findall("layer")
                if layer.get("class") == "SimpleFill"
            )
            color = next(
                option for option in fill.findall("./Option/Option")
                if option.get("name") == "color"
            )
            self.assertTrue(
                color.get("value").startswith("227,204,180,"),
                color.get("value"),
            )
            self.assertNotIn(
                "fillColor",
                [option.get("name") for option in fill.iter("Option")],
            )
            svg = next(
                layer for layer in symbol.findall("layer")
                if layer.get("class") == "SVGFill"
            )
            path = next(
                option for option in svg.findall("./Option/Option")
                if option.get("name") == "svgFile"
            )
            self.assertEqual(path.get("value"), "../icons/wetland.svg")

    def test_wetland_keeps_its_fill(self):
        wetland = next(
            layer for layer in self.root.iter("maplayer")
            if layer.findtext("layername") == "wetland"
        )
        fill = next(
            layer for layer in wetland.find("renderer-v2/symbols/symbol")
            .findall("layer") if layer.get("class") == "SimpleFill"
        )
        color = next(
            option for option in fill.findall("./Option/Option")
            if option.get("name") == "color"
        )
        self.assertTrue(color.get("value").startswith("158,229,219,"))


class ShingleLabelTest(unittest.TestCase):
    """Подписи shingle повторяют стиль подписей sand."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _rules(self, name):
        return self.layers[name].find("labeling/rules").findall("rule")

    def test_labels_are_rule_based(self):
        self.assertEqual(self.layers["shingle"].get("labelsEnabled"), "1")
        self.assertEqual(
            self.layers["shingle"].find("labeling").get("type"),
            "rule-based",
        )
        self.assertEqual(len(self._rules("shingle")), 5)

    def test_scale_bands_match_sand(self):
        def bands(name):
            return [
                (rule.get("scalemindenom"), rule.get("scalemaxdenom"))
                for rule in self._rules(name)
            ]
        self.assertEqual(bands("shingle"), bands("sand"))

    def test_text_style_matches_sand(self):
        for shingle_rule, sand_rule in zip(
            self._rules("shingle"), self._rules("sand")
        ):
            shingle = shingle_rule.find("settings/text-style")
            sand = sand_rule.find("settings/text-style")
            for attribute in (
                "fontSize", "textColor", "fontFamily", "fontWeight",
                "namedStyle",
            ):
                self.assertEqual(
                    shingle.get(attribute), sand.get(attribute), attribute
                )
            shingle_buffer = shingle.find("text-buffer")
            sand_buffer = sand.find("text-buffer")
            for attribute in (
                "bufferDraw", "bufferSize", "bufferColor", "bufferOpacity",
            ):
                self.assertEqual(
                    shingle_buffer.get(attribute),
                    sand_buffer.get(attribute),
                    attribute,
                )

    def test_shingle_keeps_its_own_name_expression(self):
        for rule in self._rules("shingle"):
            expression = rule.find("settings/text-style").get("fieldName")
            self.assertIn("@name_tag_1", expression)
            self.assertNotIn("format_sand_desert_name", expression)

    def test_sand_keeps_its_name_expression(self):
        for rule in self._rules("sand"):
            expression = rule.find("settings/text-style").get("fieldName")
            self.assertIn("format_sand_desert_name", expression)

    def test_rule_keys_differ_from_sand(self):
        shingle = {rule.get("key") for rule in self._rules("shingle")}
        sand = {rule.get("key") for rule in self._rules("sand")}
        self.assertFalse(shingle & sand)


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


class IsolinesSchemeTest(unittest.TestCase):
    """Видимость изолиний, подписи и штриховка по масштабам.

    Схема: 1-25k все, 25-50k без 5, 50-100k без 5/10, 100-300k без
    5/10/25, 300-600k без 5/10/25/50, 600k+ только 200 (принудительно)
    и 1000.  Подписи на уровень грубее, 25 и 200 не подписываются.
    Фильтры правил не пересекаются: каждая линия рисуется одним уровнем.
    """

    RENDER_MAX = {5: 25000, 10: 50000, 25: 100000, 50: 300000,
                  100: 600000, 200: 0, 1000: 0}
    LABEL_MAX = {10: 25000, 50: 100000, 100: 300000, 1000: 0}
    LABEL_OFF = (25, 200)
    EXCLUDED = {
        5: (1000, 200, 100, 50, 25, 10),
        10: (1000, 200, 100, 50, 25),
        25: (1000, 200, 100, 50),
        50: (1000, 200, 100),
        100: (1000, 200),
        200: (1000,),
        1000: (),
    }
    HASH_BUCKETS = {5: (), 10: (0,), 25: (0,), 50: (0,),
                    100: (0, 1, 2), 200: (0, 1, 2), 1000: (0, 1, 2)}
    LAYERS = (
        ISOLINES_REGULAR, ISOLINES_GLACIER,
        ISOLINES_REGULAR_OVERVIEW, ISOLINES_GLACIER_OVERVIEW,
    )
    OVERVIEW = {ISOLINES_REGULAR_OVERVIEW, ISOLINES_GLACIER_OVERVIEW}

    def _expected_render(self, name):
        return {str(value) for value in self.RENDER_MAX}

    def _expected_labels(self, name):
        return {str(value) for value in set(self.LABEL_MAX) | set(self.LABEL_OFF)}

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _rules(self, layer_name, tag, attribute):
        layer = self.layers.get(layer_name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {layer_name}")
        parent = layer.find(tag)
        self.assertIsNotNone(parent, f"у слоя {layer_name} нет {tag}")
        return {
            rule.get(attribute): rule
            for rule in parent.findall("./rules/rule")
        }

    def _check_filter(self, rule, value):
        expression = rule.get("filter") or ""
        self.assertIn(f'("ELEV" % {value}) = 0', expression)
        for excluded in self.EXCLUDED[value]:
            self.assertIn(f'("ELEV" % {excluded}) != 0', expression)

    @staticmethod
    def _bucket(child):
        minimum = int(child.get("scalemindenom", "0"))
        maximum = int(child.get("scalemaxdenom", "0"))
        return {(0, 50000): 0, (50000, 100000): 1,
                (100000, 300000): 2, (300000, 0): 3}.get(
                    (minimum, maximum))

    @staticmethod
    def _hash_enabled(renderer, child):
        name = child.get("symbol")
        symbols = renderer.findall("./symbols/symbol")
        symbol = next(
            (item for item in symbols if item.get("name") == name), None)
        if symbol is None:
            return []
        return [
            layer.get("enabled") == "1"
            for layer in symbol.findall("layer")
            if layer.get("class") == HASH_CLASS
        ]

    def test_renderer_levels_and_scales(self):
        for name in self.LAYERS:
            rules = self._rules(name, "renderer-v2", "label")
            self.assertEqual(set(rules), self._expected_render(name))
            for value, maximum in self.RENDER_MAX.items():
                rule = rules.get(str(value))
                if rule is None:
                    self.assertIn(name, self.OVERVIEW)
                    continue
                self.assertNotIn(
                    "checkstate", rule.attrib,
                    f"{name}: правило {value} должно быть активным")
                self.assertEqual(
                    int(rule.get("scalemaxdenom", "0")), maximum)
                self.assertEqual(
                    int(rule.get("scalemindenom", "0")), 0)
                self._check_filter(rule, value)

    def test_hash_lines_follow_scale_and_level(self):
        for name in self.LAYERS:
            renderer = self.layers[name].find("renderer-v2")
            rules = self._rules(name, "renderer-v2", "label")
            for value, buckets in self.HASH_BUCKETS.items():
                rule = rules.get(str(value))
                if rule is None:
                    self.assertIn(name, self.OVERVIEW)
                    continue
                for child in rule.findall("rule"):
                    bucket = self._bucket(child)
                    if bucket is None:
                        continue
                    expected = bucket in buckets
                    enabled = self._hash_enabled(renderer, child)
                    self.assertTrue(
                        enabled, f"{name}: у {value} нет HashLine")
                    for flag in enabled:
                        self.assertEqual(
                            flag, expected,
                            f"{name}: HashLine {value}/{bucket}")

    @staticmethod
    def _hash_layer_elements(renderer, child):
        name = child.get("symbol")
        symbol = next(
            (item for item in renderer.findall("./symbols/symbol")
             if item.get("name") == name), None)
        if symbol is None:
            return []
        return [
            layer for layer in symbol.findall("layer")
            if layer.get("class") == HASH_CLASS
        ]

    @staticmethod
    def _dd_enabled(layer):
        for option in layer.iter("Option"):
            if option.get("name") != "enabled":
                continue
            return {
                item.get("name"): item.get("value")
                for item in option.findall("Option")
            }
        return None

    def test_hash_lines_stop_at_150k(self):
        """Выше 1:150 000 штриховка выключена data-defined enabled."""
        for name in self.LAYERS:
            renderer = self.layers[name].find("renderer-v2")
            rules = self._rules(name, "renderer-v2", "label")
            for value in (100, 200, 1000):
                for child in rules[str(value)].findall("rule"):
                    bucket = self._bucket(child)
                    if bucket not in (0, 1, 2):
                        continue
                    layers = self._hash_layer_elements(renderer, child)
                    self.assertTrue(
                        layers, f"{name}: нет HashLine {value}/{bucket}")
                    for layer in layers:
                        data = self._dd_enabled(layer)
                        self.assertIsNotNone(
                            data, f"{name}: нет enabled {value}/{bucket}")
                        self.assertEqual(data.get("active"), "true")
                        self.assertIn(
                            "@map_scale <= 150000",
                            data.get("expression") or "")

    def test_label_levels_scales_and_visibility(self):
        for name in self.LAYERS:
            rules = self._rules(name, "labeling", "description")
            self.assertEqual(set(rules), self._expected_labels(name))
            for value, maximum in self.LABEL_MAX.items():
                rule = rules.get(str(value))
                if rule is None:
                    self.assertIn(name, self.OVERVIEW)
                    continue
                self.assertIsNone(
                    rule.get("active"),
                    f"{name}: подписи {value} должны быть включены")
                self.assertEqual(
                    int(rule.get("scalemaxdenom", "0")), maximum)
                self.assertEqual(
                    int(rule.get("scalemindenom", "0")), 0)
                self._check_filter(rule, value)
                for child in rule.iter("rule"):
                    self.assertIsNone(child.get("active"))
            for value in self.LABEL_OFF:
                rule = rules[str(value)]
                self.assertEqual(
                    rule.get("active"), "0",
                    f"{name}: подписи {value} должны быть выключены")
                for child in rule.iter("rule"):
                    self.assertEqual(child.get("active"), "0")

    def test_overview_labels_are_limited(self):
        """На 1:100 000 и мельче число подписей изолиний ограничено.

        Движок подписей перебирает и размещает тысячи подписей; 500 на
        кадр заметно ускоряют обзорный рендер.  Крупные масштабы не
        затрагиваются.
        """
        for name in self.LAYERS:
            rules = self._rules(name, "labeling", "description")
            for value in set(self.LABEL_MAX) | set(self.LABEL_OFF):
                rule = rules.get(str(value))
                if rule is None:
                    self.assertIn(name, self.OVERVIEW)
                    continue
                for child in rule.iter("rule"):
                    rendering = child.find("./settings/rendering")
                    if rendering is None:
                        continue
                    minimum = int(child.get("scalemindenom", "0"))
                    if minimum >= 100000:
                        self.assertEqual(
                            rendering.get("limitNumLabels"), "1",
                            f"{name}: подписи {value} на {minimum}+ "
                            "должны быть ограничены")
                        self.assertEqual(
                            rendering.get("maxNumLabels"), "500",
                            f"{name}: подписи {value} на {minimum}+ "
                            "должны ограничиваться 500")
                    else:
                        self.assertEqual(
                            rendering.get("limitNumLabels"), "0",
                            f"{name}: крупный масштаб {value} "
                            "не должен ограничиваться")


class IsolinesScaleSplitTest(unittest.TestCase):
    """Подробные изолинии — до 1:50000, обзорные — с 1:50001.

    Геометрия подробного слоя не упрощается.  Обзорная копия рисуется
    только на мелких масштабах и сохраняет прежнюю скорость кадра.
    Граница 50001, а не 50000: QGIS считает minScale исключающим, и
    ровно 1:50000 должно оставаться за подробным слоем.
    """

    PAIRS = (
        (ISOLINES_REGULAR, ISOLINES_REGULAR_OVERVIEW),
        (ISOLINES_GLACIER, ISOLINES_GLACIER_OVERVIEW),
    )
    DETAIL_LIMIT = 50001

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _bounds(self, name):
        layer = self.layers.get(name)
        self.assertIsNotNone(layer, name)
        return (
            float(layer.get("minScale") or 0),
            float(layer.get("maxScale") or 0),
        )

    def test_scale_ranges(self):
        for fine, overview in self.PAIRS:
            self.assertEqual(
                self._bounds(fine), (self.DETAIL_LIMIT, 0.0), fine
            )
            self.assertEqual(
                self._bounds(overview), (0.0, self.DETAIL_LIMIT), overview
            )
            for name in (fine, overview):
                self.assertEqual(
                    self.layers[name].get("hasScaleBasedVisibilityFlag"),
                    "1", name,
                )

    def test_overview_keeps_the_same_elevation_filters(self):
        """Общие уровни высот по-прежнему описаны одинаково."""
        for fine, overview in self.PAIRS:
            for tag, attribute in (
                ("renderer-v2", "label"),
                ("labeling", "description"),
            ):
                first = self.layers[fine].find(tag)
                second = self.layers[overview].find(tag)
                self.assertIsNotNone(first, (fine, tag))
                self.assertIsNotNone(second, (overview, tag))
                fine_rules = {
                    rule.get(attribute): rule
                    for rule in first.findall("./rules/rule")
                }
                overview_rules = {
                    rule.get(attribute): rule
                    for rule in second.findall("./rules/rule")
                }
                common = set(fine_rules) & set(overview_rules)
                self.assertTrue(common, (fine, overview, tag))
                for key in common:
                    self.assertEqual(
                        fine_rules[key].get("filter"),
                        overview_rules[key].get("filter"),
                        (fine, overview, tag, key),
                    )

    def test_overview_reads_separate_files(self):
        for fine, overview in self.PAIRS:
            source = self.layers[overview].findtext("datasource") or ""
            self.assertIn("_overview.sqlite", source, overview)
            self.assertNotIn(
                "isolines_regular.sqlite", source, overview
            )


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


class LabelHaloTest(unittest.TestCase):
    """Обводки подписей: прозрачность, цвет и размер."""

    ADMIN_LAYERS = (
        "admin level 2 (country names)",
        "admin level 4 (region names)",
    )
    WATER_COLORS = {
        "water names continental":
            "230,242,255,255,rgb:0.9019608,0.9490196,1,1",
        "river": "237,245,255,255,rgb:0.9294118,0.9607843,1,1",
    }
    VOLCANO_COLORS = {
        "volcano": "251,230,230,255,rgb:0.9843137,0.9019608,0.9019608,1",
        "volcano dirt": "242,238,230,255,rgb:0.9490196,0.9333333,0.9019608,1",
    }
    ADMIN_COLOR = "254,247,255,255,rgb:0.9960784,0.9686275,1,1"

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _buffers(self, name):
        layer = self.layers.get(name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {name}")
        buffers = list(layer.iter("text-buffer"))
        self.assertTrue(buffers, f"у слоя {name} нет обводки подписей")
        return buffers

    def test_all_map_halos_have_20_percent_transparency(self):
        for name, layer in self.layers.items():
            for buffer in layer.iter("text-buffer"):
                self.assertEqual(
                    buffer.get("bufferOpacity"), "0.8",
                    f"halo слоя {name} не на 20 % прозрачен",
                )

    def test_volcano_halos_are_lighter(self):
        for name, color in self.VOLCANO_COLORS.items():
            for buffer in self._buffers(name):
                self.assertEqual(
                    buffer.get("bufferColor"), color,
                    f"halo вулкана {name} не осветлён",
                )

    def test_water_halos_are_lighter(self):
        for name, color in self.WATER_COLORS.items():
            colors = {
                buffer.get("bufferColor") for buffer in self._buffers(name)
            }
            self.assertIn(
                color, colors, f"halo воды {name} не осветлён"
            )

    def test_admin_halos_are_smaller_and_lighter(self):
        for name in self.ADMIN_LAYERS:
            for buffer in self._buffers(name):
                self.assertEqual(
                    buffer.get("bufferSize"), "0.66666666666666663",
                    f"halo {name} не уменьшен в 1,5 раза",
                )
                self.assertEqual(
                    buffer.get("bufferColor"), self.ADMIN_COLOR,
                    f"halo {name} не осветлён",
                )


class RoadShieldLabelBufferTest(unittest.TestCase):
    """У road shields нет обводки подписи.

    Белая кайма текста рисуется поверх чёрной рамки щита и делает её
    неравномерной.  Фон щита (белый прямоугольник с рамкой) остаётся.
    """

    def test_shields_have_no_text_buffer(self):
        found = 0
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername")
            for rule in layer.iter("rule"):
                if rule.get("description") != "shields":
                    continue
                for buffer in rule.iter("text-buffer"):
                    found += 1
                    self.assertEqual(
                        buffer.get("bufferDraw"), "0",
                        f"у road shield слоя {name} осталась обводка текста",
                    )
        self.assertTrue(found, "в шаблоне нет правил road shields")


class BlockingRoadLabelsTest(unittest.TestCase):
    """Подписи слоёв-блокировщиков дорог на карте не видны.

    Слои группы «[blocking text]» — копии настоящих слоёв с нулевым
    символом: они не рисуются, но подписывают те же объекты.  Их текст
    занимает место и не даёт чужим подписям ложиться на дорогу, но сам
    виден на карте, а стиль у дорог отличается от настоящего (серый
    halo вместо цвета полосы).  Из-за этого одно и то же название
    дороги выглядело по-разному.
    """

    LAYERS = (
        "[blocking] highway main",
        "[blocking] highway main overview",
        "[blocking] highway main bridge",
    )

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _labelings(self, name):
        layer = self.layers.get(name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {name}")
        labelings = list(layer.iter("labeling"))
        self.assertTrue(labelings, f"у слоя {name} нет подписей")
        return labelings

    def test_blocking_road_labels_are_invisible(self):
        for name in self.LAYERS:
            for labeling in self._labelings(name):
                style = labeling.find(".//text-style")
                self.assertEqual(
                    style.get("textColor"), "0,0,0,0,rgb:0,0,0,0",
                    f"текст слоя {name} виден на карте",
                )
                buffer = labeling.find(".//text-buffer")
                self.assertEqual(
                    buffer.get("bufferColor"),
                    "243,243,243,0,rgb:0.9529412,0.9529412,0.9529412,0",
                    f"halo слоя {name} виден на карте",
                )

    def test_blocking_road_labels_lose_to_real_ones(self):
        """Приоритет копии ниже приоритета настоящих правил дорог."""
        for name in self.LAYERS:
            real = name[len("[blocking] "):]
            real_priorities = set()
            for real_labeling in self._labelings(real):
                real_priorities |= {
                    int(item.get("priority", 0))
                    for item in real_labeling.iter("placement")
                }
            self.assertTrue(real_priorities, f"у слоя {real} нет размещения")
            for labeling in self._labelings(name):
                priorities = {
                    int(item.get("priority", 0))
                    for item in labeling.iter("placement")
                }
                self.assertTrue(priorities, f"у слоя {name} нет размещения")
                self.assertLess(
                    max(priorities), max(real_priorities),
                    f"копия {name} может отнять подпись у {real}",
                )


class PeakLabelSizeTest(unittest.TestCase):
    """Размер подписи пика растёт ступенями по высоте."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app
        from qgis_xtopo.core.expressions import load_expressions

        cls.app = qgis_app()
        load_expressions()
        cls.layer = next(
            layer for layer in _template_root().iter("maplayer")
            if layer.findtext("layername") == "peak"
        )

    def _rules(self):
        return self.layer.find("labeling").find("rules").findall("rule")

    def _size_expression(self, rule):
        size = rule.find(
            "settings/dd_properties/Option/Option[@name='properties']"
            "/Option[@name='Size']"
        )
        self.assertIsNotNone(size)
        expression = size.find("Option[@name='expression']")
        self.assertIsNotNone(expression)
        return expression.get("value") or ""

    def test_elevation_steps(self):
        from qgis.core import QgsExpression, QgsExpressionContext

        expected = {
            0: 0.0, 999: 0.0, 1000: 0.5, 1999: 0.5,
            2000: 1.0, 2999: 1.0, 3000: 1.5, 3999: 1.5,
            4000: 2.0, 4478: 2.0,
        }
        context = QgsExpressionContext()
        for elevation, value in expected.items():
            expression = QgsExpression(
                f"get_peak_text_size_add({elevation})"
            )
            self.assertFalse(expression.hasParserError())
            self.assertAlmostEqual(expression.evaluate(context), value)

    def test_expressions_use_elevation_steps(self):
        rules = self._rules()
        self.assertEqual(len(rules), 4)
        for rule in rules:
            expression = self._size_expression(rule)
            self.assertIn("get_peak_text_size_add(", expression)
            self.assertNotIn("raster_statistic", expression)
            self.assertNotIn("get_text_size_add", expression)
            self.assertEqual(expression.count("@text_size_mult"), 1)

    def test_medium_scale_keeps_boost(self):
        rule = next(
            rule for rule in self._rules()
            if rule.get("scalemindenom") == "50000"
            and rule.get("scalemaxdenom") == "100000"
        )
        self.assertIn("* 2.5", self._size_expression(rule))

    def test_text_add_field_is_removed(self):
        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertNotIn("text_add", text)


class SettlementScaleTest(unittest.TestCase):
    """Видимость хуторов зависит от масштаба и уровня детализации."""

    NAMES = ("hamlet", "isolated dwelling")
    FINE = ("", "")  # 0-100k и 50k-100k: любой LOD
    MIDDLE = "@level_of_detail >= 3"
    COARSE = "@level_of_detail >= 6"

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

    @staticmethod
    def _bounds(rule):
        def number(name):
            value = float(rule.get(name) or 0)
            # scalemindenom=1 в шаблоне означает отсутствие нижней границы.
            return 0.0 if value <= 1.0 else value
        return number("scalemindenom"), number("scalemaxdenom")

    def _visible_at(self, leaves, scale):
        result = []
        for rule in leaves:
            lower, upper = self._bounds(rule)
            if lower <= scale and (upper == 0 or scale < upper):
                result.append(rule)
        return result

    @staticmethod
    def _base_filter(filter_text):
        """Фильтр правила без условия подавления значка."""
        return _base_filter(filter_text)

    def test_visibility_matrix_for_both_containers(self):
        checks = (
            (25000, self.FINE[0]),
            (75000, self.FINE[1]),
            (150000, self.MIDDLE),
            (300000, self.COARSE),
        )
        for container in ("renderer-v2/rules", "labeling/rules"):
            for name in self.NAMES:
                with self.subTest(container=container, name=name):
                    leaves = self._leaves(
                        self._branch(container, ["else", "else", name])
                    )
                    for scale, expected in checks:
                        visible = self._visible_at(leaves, scale)
                        self.assertEqual(len(visible), 1, (scale, visible))
                        self.assertEqual(
                            self._base_filter(visible[0].get("filter")),
                            expected, scale,
                        )
                    # Дальше 1:500000 правил нет.
                    self.assertFalse(self._visible_at(leaves, 600000))

    def test_exact_scale_boundaries(self):
        """Ровно 1:100000 — средний диапазон, ровно 1:500000 — уже скрыто.

        Верхняя граница диапазона исключающая: правило `0 - 100000`
        перестаёт действовать ровно на 1:100000, а `100000 - 200000`
        начинается там же. Это же подтверждает движок QGIS ниже.
        """
        checks = (
            (99999, self.FINE[1]),
            (100000, self.MIDDLE),
            (199999, self.MIDDLE),
            (200000, self.COARSE),
            (499999, self.COARSE),
        )
        for container in ("renderer-v2/rules", "labeling/rules"):
            for name in self.NAMES:
                with self.subTest(container=container, name=name):
                    leaves = self._leaves(
                        self._branch(container, ["else", "else", name])
                    )
                    for scale, expected in checks:
                        visible = self._visible_at(leaves, scale)
                        self.assertEqual(len(visible), 1, (scale, visible))
                        self.assertEqual(
                            self._base_filter(visible[0].get("filter")),
                            expected, scale,
                        )
                    self.assertFalse(self._visible_at(leaves, 500000))

    def test_coarse_scale_stops_at_500k(self):
        for container in ("renderer-v2/rules", "labeling/rules"):
            for name in self.NAMES:
                with self.subTest(container=container, name=name):
                    leaves = self._leaves(
                        self._branch(container, ["else", "else", name])
                    )
                    for rule in leaves:
                        lower, upper = self._bounds(rule)
                        self.assertLessEqual(upper, 500000, (lower, upper))
                        self.assertLess(lower, 500000, (lower, upper))

    def test_place_density_is_gone(self):
        """Плотность расселения убрана из всех слоёв, а не только places main."""
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername") or ""
            for rule in layer.iter("rule"):
                self.assertNotIn(
                    "place_density", rule.get("filter") or "",
                    (name, rule.get("description") or rule.get("label")),
                )

    def test_population_branch_excludes_them_at_200k(self):
        expected = (
            "population IS NOT NULL AND to_real(population)!='' "
            "AND @level_of_detail < 6 "
            "AND NOT (place IN ('hamlet','isolated_dwelling') "
            "AND @map_scale >= 200000)"
        )
        for container in ("renderer-v2/rules", "labeling/rules"):
            group = self._branch(container, ["else", "by population"])
            self.assertIn(expected, group.get("filter") or "", container)


class SettlementScaleEngineTest(unittest.TestCase):
    """Точные границы масштабов глазами самого движка правил QGIS."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        from qgis.core import QgsProject

        cls.project = QgsProject()
        if not cls.project.read(str(template_path())):
            raise unittest.SkipTest("шаблон проекта не читается")
        cls.layer = next(
            layer for layer in cls.project.mapLayers().values()
            if layer.name() == "places main"
        )

    @classmethod
    def tearDownClass(cls):
        cls.project.clear()

    @staticmethod
    def _find(rule, description):
        for child in rule.children():
            if child.description() == description:
                return child
            found = SettlementScaleEngineTest._find(child, description)
            if found is not None:
                return found
        return None

    @staticmethod
    def _leaves(rule):
        leaves = []
        for child in rule.children():
            if child.children():
                leaves.extend(SettlementScaleEngineTest._leaves(child))
            else:
                leaves.append(child)
        return leaves

    def _hamlet_leaves(self):
        root = self.layer.renderer().rootRule()
        empty = next(
            child for child in root.children() if not child.description()
        )
        else_rule = self._find(empty, "else")
        self.assertIsNotNone(else_rule)
        hamlet = self._find(else_rule, "hamlet")
        self.assertIsNotNone(hamlet)
        return self._leaves(hamlet)

    def _visible(self, leaves, scale):
        return [
            rule for rule in leaves
            if rule.isScaleOK(scale)
        ]

    def test_exact_boundaries_match_the_xml_rules(self):
        leaves = self._hamlet_leaves()
        checks = (
            (99999, "0 - 100000", ""),
            (100000, "100000 - 200000", "@level_of_detail >= 3"),
            (200000, "200000 - 500000", "@level_of_detail >= 6"),
        )
        for scale, description, expression in checks:
            visible = self._visible(leaves, scale)
            self.assertEqual(
                [rule.description() for rule in visible],
                [description], scale,
            )
            self.assertEqual(
                _base_filter(visible[0].filterExpression()), expression,
                scale,
            )
        self.assertEqual(self._visible(leaves, 500000), [])


class QuarterPlaceTest(unittest.TestCase):
    """`place=quarter` рисуется правилом suburb/neighbourhood.

    Отдельный символ кварталу не нужен: как и neighbourhood, он
    показывает только подпись, а не значок.  Правило общее в обоих
    слоях населённых пунктов, чтобы стиль совпадал.
    """

    def test_quarter_is_in_renderer_and_label_filters(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
            if layer.findtext("layername") in (
                "places main", "places main major",
            )
        }
        self.assertEqual(set(layers), {"places main", "places main major"})
        for name, layer in layers.items():
            for container in ("renderer-v2", "labeling"):
                filters = [
                    rule.get("filter") or ""
                    for rule in layer.find(container).iter("rule")
                    if "place = 'suburb'" in (rule.get("filter") or "")
                ]
                self.assertEqual(len(filters), 1, (name, container))
                for place in ("suburb", "quarter", "neighbourhood"):
                    self.assertIn(
                        f"place = '{place}'", filters[0], (name, container),
                    )


class LocalityScaleTest(unittest.TestCase):
    """`place=locality` скрывается на обзорных масштабах по уровню детализации.

    Значки точечного слоя и подписи обоих слоёв используют одно условие:
    при LOD 4 и меньше — с 1:150000, при LOD 5 — с 1:170000.  На LOD 6
    местность остаётся видимой.
    """

    CONTAINERS = (
        ("place locality point", "renderer-v2"),
        ("place locality point", "labeling"),
        ("place locality polygon", "labeling"),
    )
    CHECKS = (
        (100000, 4, True),
        (149999, 4, True),
        (150000, 4, False),
        (169999, 5, True),
        (170000, 5, False),
        (170000, 6, True),
        (300000, 4, False),
        (300000, 6, True),
    )

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        cls.root = _template_root()

    @staticmethod
    def _bounds(rule):
        lower = float(rule.get("scalemindenom") or 0)
        upper = float(rule.get("scalemaxdenom") or 0)
        return (0.0 if lower <= 1.0 else lower), upper

    def _rule_at(self, layer_name, tag, scale):
        layer = next(
            layer for layer in self.root.iter("maplayer")
            if layer.findtext("layername") == layer_name
        )
        container = layer.find(tag)
        self.assertIsNotNone(container)
        for rule in container.iter("rule"):
            lower, upper = self._bounds(rule)
            if lower <= scale and (not upper or scale < upper):
                return rule
        return None

    def _visible(self, rule, scale, lod):
        from qgis.core import (
            QgsExpression, QgsExpressionContext, QgsExpressionContextScope,
            QgsFeature, QgsField, QgsFields,
        )
        from qgis.PyQt.QtCore import QVariant

        expression = QgsExpression(rule.get("filter") or "")
        self.assertFalse(
            expression.hasParserError(), expression.parserErrorString()
        )
        context = QgsExpressionContext()
        scope = QgsExpressionContextScope("locality")
        scope.setVariable("level_of_detail", lod)
        scope.setVariable("map_scale", scale)
        context.appendScope(scope)
        fields = QgsFields()
        fields.append(QgsField("hide", QVariant.String))
        feature = QgsFeature(fields)
        # Пустое подавление: проверяется только новое условие масштаба.
        feature.setAttribute("hide", "1e30")
        context.setFeature(feature)
        return bool(expression.evaluate(context))

    def test_visibility_matrix(self):
        for layer_name, tag in self.CONTAINERS:
            for scale, lod, expected in self.CHECKS:
                with self.subTest(layer=layer_name, tag=tag,
                                  scale=scale, lod=lod):
                    rule = self._rule_at(layer_name, tag, scale)
                    self.assertIsNotNone(rule, (scale, lod))
                    self.assertEqual(
                        self._visible(rule, scale, lod), expected,
                        (scale, lod),
                    )


class RuleIntegrityTest(unittest.TestCase):
    """Правила, которые QGIS не сможет прочитать целиком."""

    def test_symbols_are_not_shared(self):
        """QGIS забирает символ из карты по индексу: второй такое правило
        остаётся без символа и не рисуется."""
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername")
            for renderer in layer.iter("renderer-v2"):
                if renderer.get("type") != "RuleRenderer":
                    continue
                symbols = {
                    symbol.get("name")
                    for symbol in renderer.findall("symbols/symbol")
                }
                rules = renderer.find("rules")
                if rules is None:
                    continue
                refs = Counter(
                    rule.get("symbol")
                    for rule in rules.iter("rule")
                    if rule.get("symbol")
                )
                shared = {ref: count for ref, count in refs.items() if count > 1}
                self.assertFalse(shared, (name, shared))
                self.assertEqual(sorted(set(refs) - symbols), [], name)

    def test_scale_attributes_are_spelled_correctly(self):
        """scaleminendenom — опечатка, значение QGIS не читает."""
        for rule in _template_root().iter("rule"):
            self.assertNotIn(
                "scaleminendenom", rule.attrib,
                rule.get("description") or rule.get("label"),
            )


class RuleScaleRangeTest(unittest.TestCase):
    """Диапазоны соседних правил не должны пересекаться.

    Верхняя граница диапазона в QGIS исключающая, поэтому стык
    `100000`/`100000` пересечением не считается.
    """

    @staticmethod
    def _interval(rule, parent):
        lower = float(rule.get("scalemindenom") or 0)
        upper = float(rule.get("scalemaxdenom") or 0) or float("inf")
        return max(parent[0], lower), min(parent[1], upper)

    def _check(self, container, parent, layer_name):
        children = container.findall("rule")
        drawing = [
            (rule, self._interval(rule, parent))
            for rule in children
            if rule.find("settings") is not None
            or rule.find("symbol") is not None
        ]
        for index, (first, first_range) in enumerate(drawing):
            for second, second_range in drawing[index + 1:]:
                if (first.get("filter") or "") != (second.get("filter") or ""):
                    continue
                lower = max(first_range[0], second_range[0])
                upper = min(first_range[1], second_range[1])
                self.assertGreaterEqual(
                    lower, upper,
                    f"{layer_name}: {first.get('description')} "
                    f"{first_range} и {second.get('description')} "
                    f"{second_range}",
                )
        for rule in children:
            self._check(
                rule, self._interval(rule, parent), layer_name
            )

    def test_sibling_scale_ranges_do_not_overlap(self):
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername")
            labeling = layer.find("labeling")
            if labeling is not None and labeling.get("type") == "rule-based":
                rules = labeling.find("rules")
                if rules is not None:
                    self._check(rules, (0.0, float("inf")), name)
            renderer = layer.find("renderer-v2")
            if renderer is not None and renderer.get("type") == "RuleRenderer":
                rules = renderer.find("rules")
                if rules is not None:
                    self._check(rules, (0.0, float("inf")), name)

    def test_plateau_labels_stop_at_400k(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        expected = [
            ("30000", "100000"),
            ("100000", "200000"),
            ("200000", "300000"),
            ("300000", "400000"),
        ]
        for name in ("plateau", "plateau node"):
            rules = layers[name].findall("./labeling/rules/rule")
            self.assertEqual(
                [
                    (rule.get("scalemindenom"), rule.get("scalemaxdenom"))
                    for rule in rules
                ],
                expected,
                name,
            )


class AllotmentLabelScaleTest(unittest.TestCase):
    """Типовые подписи садовых участков не показываются на обзоре."""

    def test_labels_stop_at_150k(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        layer = layers["allotments names 300+"]
        rule = layer.find("labeling/rules/rule")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.get("scalemindenom"), "1")
        self.assertEqual(rule.get("scalemaxdenom"), "150000")
        renderer_rule = layer.find("renderer-v2/rules/rule")
        self.assertIsNotNone(renderer_rule)
        self.assertEqual(renderer_rule.get("scalemindenom"), "1")
        self.assertEqual(renderer_rule.get("scalemaxdenom"), "150000")


class IslandLabelAreaTest(unittest.TestCase):
    """На обзорных масштабах подписываются только крупные острова.

    Иначе в дельте Лены десятки названий мелких островов закрывают карту.
    """

    def test_overview_rule_requires_large_island(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        rules = layers["island polygon"].find("labeling/rules").findall("rule")
        overview = [
            rule for rule in rules
            if rule.get("scalemindenom") == "300000"
            and rule.get("scalemaxdenom") is None
        ]
        self.assertEqual(len(overview), 1)
        self.assertEqual(
            overview[0].get("filter"), '"area_geodesic">150000000'
        )


class WaterLabelMinimumFeatureSizeTest(unittest.TestCase):
    """Маленькие водоёмы не подписываются.

    `minFeatureSize` требует, чтобы полигон занимал на карте не меньше
    заданного размера в миллиметрах; иначе подпись длинного названия
    выходит далеко за пределы почти невидимого озера.
    """

    def test_active_water_label_rules_require_two_mm(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        labeling = layers["water names continental"].find("labeling")
        self.assertIsNotNone(labeling)
        for rule in labeling.iter("rule"):
            if rule.find("settings") is None or rule.get("active") == "0":
                continue
            rendering = rule.find("settings/rendering")
            self.assertIsNotNone(
                rendering, rule.get("filter") or rule.get("description")
            )
            self.assertGreaterEqual(
                float(rendering.get("minFeatureSize") or 0),
                2.0,
                rule.get("filter") or rule.get("description"),
            )


class WaterLabelAreaFieldTest(unittest.TestCase):
    """Площадь для подписей воды берётся из подготовленного поля.

    `$area` в контексте подписей считается по эллипсоиду для каждого
    объекта в кадре.  Геодезическая площадь считается один раз при
    подготовке и лежит в `area_geodesic`.
    """

    def test_labels_use_precomputed_area(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        for name, source in LABEL_AREA_LAYERS.items():
            layer = layers[name]
            datasource = layer.findtext("datasource") or ""
            self.assertIn(f"{source}.sqlite", datasource, name)
            labeling = layer.find("labeling")
            self.assertIsNotNone(labeling, name)
            values = [
                value
                for element in labeling.iter()
                for value in element.attrib.values()
            ]
            self.assertTrue(
                any(LABEL_AREA_FIELD in value for value in values), name)
            self.assertFalse(
                any("$area" in value for value in values), name)

    def test_overview_requires_two_square_km(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        rules = layers["water names continental"].find(
            "labeling/rules").findall("rule")
        overview = [
            rule for rule in rules
            if rule.get("scalemindenom") == "300000"
            and rule.get("scalemaxdenom") is None
        ]
        self.assertEqual(len(overview), 1)
        active = [
            rule for rule in overview[0].findall("rule")
            if rule.get("active") != "0"
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(
            active[0].get("filter"), f'"{LABEL_AREA_FIELD}" > 2000000')


class WaterLodTest(unittest.TestCase):
    """Мелкие озёра скрываются по геодезической площади.

    Старые правила до 1:300 000 остаются как есть.  Для 1:400 000 и
    мельче порог зависит от полосы масштаба и уровня детализации; LOD 6
    показывает больше, но не всё.
    """

    THRESHOLDS = {
        (400000, 1000000): {
            2: 320000, 3: 224000, 4: 160000, 5: 96000, 6: 64000,
        },
        (1000000, 3000000): {
            2: 2000000, 3: 1400000, 4: 1000000, 5: 600000, 6: 400000,
        },
        (3000000, None): {
            2: 18000000, 3: 12600000, 4: 9000000,
            5: 5400000, 6: 3600000,
        },
    }

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _rules(self, name):
        return self.layers[name].find("renderer-v2/rules").findall("rule")

    def _rule(self, name, minimum, maximum):
        for rule in self._rules(name):
            if rule.get("scalemindenom") != str(minimum):
                continue
            if maximum is None:
                if rule.get("scalemaxdenom") is None:
                    return rule
            elif rule.get("scalemaxdenom") == str(maximum):
                return rule
        self.fail(f"{name}: нет правила {minimum}–{maximum}")

    @staticmethod
    def _visible(rule, area, lod):
        from qgis.core import (
            QgsExpression, QgsExpressionContext, QgsExpressionContextScope,
            QgsFeature, QgsField, QgsFields,
        )
        from qgis.PyQt.QtCore import QVariant

        fields = QgsFields()
        fields.append(QgsField("area_geodesic", QVariant.Double))
        fields.append(QgsField("area_m2", QVariant.Double))
        feature = QgsFeature(fields)
        feature.setAttribute("area_geodesic", area)
        feature.setAttribute("area_m2", area)
        context = QgsExpressionContext()
        scope = QgsExpressionContextScope("water")
        scope.setVariable("level_of_detail", lod)
        scope.setVariable("map_scale", 3000000)
        context.appendScope(scope)
        context.setFeature(feature)
        expression = QgsExpression(rule.get("filter") or "")
        assert not expression.hasParserError(), expression.parserErrorString()
        return bool(expression.evaluate(context))

    def test_old_overview_rule_is_kept(self):
        rule = self._rule("water", 300000, 400000)
        self.assertIn("area_m2", rule.get("filter") or "")
        self.assertTrue(self._visible(rule, 700, 4))
        self.assertFalse(self._visible(rule, 600, 4))

    def test_new_bands_start_at_400000(self):
        for minimum, maximum in self.THRESHOLDS:
            rule = self._rule("water", minimum, maximum)
            self.assertIn("area_geodesic", rule.get("filter") or "")
            self.assertNotIn("area_m2", rule.get("filter") or "")
        self.assertFalse(
            any(
                rule.get("scalemindenom") in ("100000", "200000")
                and "area_geodesic" in (rule.get("filter") or "")
                for rule in self._rules("water")
            )
        )

    def test_thresholds_follow_lod(self):
        for band, thresholds in self.THRESHOLDS.items():
            rule = self._rule("water", *band)
            for lod, threshold in thresholds.items():
                with self.subTest(band=band, lod=lod):
                    self.assertFalse(
                        self._visible(rule, threshold - 1, lod))
                    self.assertTrue(
                        self._visible(rule, threshold, lod))

    def test_lod_six_has_a_floor(self):
        for band, thresholds in self.THRESHOLDS.items():
            rule = self._rule("water", *band)
            with self.subTest(band=band):
                self.assertGreater(thresholds[6], 0)
                self.assertFalse(self._visible(rule, 0, 6))

    def test_ocean_passes_every_band(self):
        ocean = 349060668463.0
        for band in self.THRESHOLDS:
            rule = self._rule("water", *band)
            with self.subTest(band=band):
                self.assertTrue(self._visible(rule, ocean, 2))

    def test_intermittent_gets_the_same_scale_bands(self):
        self.assertIn("area_geodesic", self._rule(
            "water intermittent", 400000, 1000000).get("filter") or "")
        self.assertIn("area_geodesic", self._rule(
            "water intermittent", 1000000, 3000000).get("filter") or "")
        old = self._rule("water intermittent", 300000, 400000)
        self.assertFalse(old.get("filter"))

    def test_labels_follow_the_renderer(self):
        rules = [
            rule
            for rule in self.layers["water names continental"].find(
                "labeling/rules").findall("rule")
            if rule.get("scalemindenom") == "300000"
        ]
        self.assertEqual(len(rules), 1)
        text = rules[0].get("filter") or ""
        self.assertIn("@map_scale < 3000000", text)
        self.assertIn("area_geodesic", text)
        self.assertIn("3600000", text)

        intermittent = [
            rule
            for rule in self.layers["water intermittent"].find(
                "labeling/rules").findall("rule")
            if rule.get("scalemindenom") == "300000"
        ]
        self.assertEqual(len(intermittent), 1)
        text = intermittent[0].get("filter") or ""
        self.assertIn("@map_scale < 400000", text)
        self.assertIn("area_geodesic", text)


class WaterLabelCalloutTest(unittest.TestCase):
    """У подписей воды выноски выключены.

    Callout рисует линию и маркер у подписи, вынесенной за пределы
    полигона. На обзорных масштабах это давало красный квадрат и линию
    поверх карты, хотя подпись и без выноски ставится рядом с водоёмом.
    """

    def test_callouts_are_disabled(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        labeling = layers["water names continental"].find("labeling")
        self.assertIsNotNone(labeling)
        callouts = list(labeling.iter("callout"))
        self.assertTrue(callouts)
        for callout in callouts:
            enabled = [
                option.get("value")
                for option in callout.iter("Option")
                if option.get("name") == "enabled"
            ]
            self.assertEqual(enabled, ["0"])


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

    def test_overview_skips_short_tracks(self):
        """На 1:200 000+ короткие тропы не рисуются и не подписываются."""
        rules = self.layer.find("renderer-v2").find("rules")
        self.assertEqual(
            rules.get("filter"),
            "@map_scale <= 200000 OR $length > 70")


class HighwayIntegrityFieldTest(unittest.TestCase):
    """Дорожный интегритет читается из данных, а не считается на кадре."""

    def test_integrity_is_not_a_virtual_field(self):
        for layer in _template_root().iter("maplayer"):
            for field in layer.findall("expressionfields/field"):
                self.assertNotEqual(
                    field.get("name"), "highway_integrity",
                    f"{layer.findtext('layername')}: поле должно быть "
                    "материализовано при подготовке")


class TextMaskReferencesTest(unittest.TestCase):
    """Текстовая маска должна ссылаться на реальные symbol layer id."""

    def test_masks_have_no_unresolved_symbol_layer_ids(self):
        for layer in _template_root().iter("maplayer"):
            for mask in layer.iter("text-mask"):
                value = mask.get("maskedSymbolLayers") or ""
                name = layer.findtext("layername")
                # Формат QGIS 3.30+: layerId;symbolLayerId;...
                # Пустой symbolLayerId даёт ";;" и маска ничего не режет.
                self.assertNotIn(";;", value, name)
                if not value:
                    self.assertNotEqual(mask.get("maskEnabled"), "1", name)


class IconMaskGeometryTest(unittest.TestCase):
    """Отступ маски значка от основы: равномерный и небольшой.

    Маска — это белая копия значка (основа лежит в ``<defs>`` того же
    файла), увеличенная равномерным отступом: поле вокруг значка
    одинаково и снаружи, и в вырезах.  Простая увеличенная копия
    силуэта давала неровное поле — у внешнего контура шире, у
    внутренних вырезов уже.

    Отступ считается от размера значка в шаблоне, поэтому при
    уменьшении значка поле тоже должно уменьшиться.  Пересобирает
    маски ``tools/build_icon_masks.py``.
    """

    # Отступ маски от контура значка, мм.
    MARGIN_MM = 0.25 / 1.5
    # Растр, в котором измеряется отступ.
    RASTER = 256
    # Допуск измерения: площадь и периметр считаются по растру, и
    # погрешность достигает пятой части отступа.
    TOLERANCE = 0.3

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        cls.sizes = _icon_masks()
        cls.icons = PACKAGE_DIR / "resources" / "icons"

    @staticmethod
    def _raster(text, width, height, viewbox):
        """Растр белого контура."""
        import numpy
        from qgis.PyQt.QtCore import QRectF, Qt
        from qgis.PyQt.QtGui import QImage, QPainter
        from qgis.PyQt.QtSvg import QSvgRenderer

        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" '
            f'height="{height:g}" viewBox="{viewbox}">'
            f'<path fill="#ffffff" fill-rule="evenodd" d="{text}"/></svg>'
        )
        renderer = QSvgRenderer(bytearray(svg.encode("utf-8")))
        self_check = renderer.isValid()
        if not self_check:
            raise AssertionError("неверный SVG")
        pixels = IconMaskGeometryTest.RASTER
        image = QImage(pixels, pixels, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        renderer.render(painter, QRectF(0, 0, pixels, pixels))
        painter.end()
        stride = image.bytesPerLine()
        bits = image.constBits()
        bits.setsize(image.byteCount())
        raw = numpy.frombuffer(bytes(bits), dtype="uint8").reshape(
            pixels, stride,
        )
        return (raw[:, : pixels * 4].reshape(pixels, pixels, 4)[:, :, 3] > 16)

    @staticmethod
    def _edge(mask):
        """Пиксели границы фигуры."""
        import numpy
        inner = mask.copy()
        inner[1:, :] &= mask[:-1, :]
        inner[:-1, :] &= mask[1:, :]
        inner[:, 1:] &= mask[:, :-1]
        inner[:, :-1] &= mask[:, 1:]
        return int((mask & ~inner).sum())

    def test_base_is_kept(self):
        """У каждой маски есть основа: из неё считается отступ."""
        files = sorted(self.icons.glob("*_m.svg"))
        self.assertTrue(files)
        for path in files:
            root = ElementTree.parse(path).getroot()
            base, data = _mask_paths(root)
            self.assertTrue(base, path.name)
            self.assertTrue(data, path.name)

    def test_offset_is_uniform(self):
        """Отступ одинаковый и равен заданному."""
        import numpy

        checked = 0
        for name, size_mm in sorted(self.sizes.items()):
            path = self.icons / name
            if not path.exists():
                continue
            root = ElementTree.parse(path).getroot()
            width = float(root.get("width") or 0)
            height = float(root.get("height") or 0)
            viewbox = root.get("viewBox") or f"0 0 {width:g} {height:g}"
            base, data = _mask_paths(root)
            base_mask = self._raster(base, width, height, viewbox)
            mask = self._raster(data, width, height, viewbox)
            edge = self._edge(base_mask)
            self.assertTrue(edge, name)
            # Площадь полосы отступа: длина контура плюс углы.
            pixels = (int(mask.sum()) - int(base_mask.sum())) / edge
            # Отступ в мм: один пиксель равен size_mm / RASTER.
            millimetres = pixels * size_mm / self.RASTER
            self.assertAlmostEqual(
                millimetres, self.MARGIN_MM,
                delta=self.MARGIN_MM * self.TOLERANCE,
                msg=f"{name}: отступ {millimetres:.3f} мм",
            )
            checked += 1
        self.assertGreater(checked, 30, "маски значков не проверены")

    def test_mask_sub_symbol_matches_icon(self):
        """Суб-символ маски того же размера, что значок."""
        for maplayer in _template_root().iter("maplayer"):
            for symbol in maplayer.iter("symbol"):
                for layer in symbol.findall("layer"):
                    if layer.get("class") != "MaskMarker":
                        continue
                    sub = layer.find("symbol")
                    if sub is None:
                        continue
                    marker = next(
                        (item for item in sub.findall("layer")
                         if item.get("class") in ("SvgMarker", "SimpleMarker")),
                        None,
                    )
                    if marker is None:
                        continue
                    mask_options = _options(marker)
                    icons = [
                        item for item in symbol.findall("layer")
                        if item.get("class") in ("SvgMarker", "SimpleMarker")
                        and not (_options(item).get("name") or "").endswith(
                            "_m.svg")
                    ]
                    if not icons:
                        continue
                    icon_options = _options(icons[0])
                    mask_size = float(mask_options.get("size") or 0)
                    icon_size = float(icon_options.get("size") or 0)
                    name = maplayer.findtext("layername")
                    if marker.get("class") == "SvgMarker":
                        self.assertAlmostEqual(
                            mask_size, icon_size, places=3,
                            msg=f"{name}: размер маски {mask_size}",
                        )
                    else:
                        self.assertAlmostEqual(
                            mask_size, icon_size + 0.5, places=3,
                            msg=f"{name}: размер маски {mask_size}",
                        )


class IconMaskReferencesTest(unittest.TestCase):
    """Все источники маски значков должны резать один и тот же полный набор."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("id"): layer
            for layer in _template_root().iter("maplayer")
        }
        cls.masks = [
            (layer, symbol_layer)
            for layer in cls.layers.values()
            for symbol_layer in layer.iter("layer")
            if symbol_layer.get("class") == "MaskMarker"
        ]

    def test_all_sources_share_one_mask_value(self):
        values = set()
        for layer, symbol_layer in self.masks:
            name = layer.findtext("layername")
            option = _find_option(symbol_layer, "mask_symbollayers")
            self.assertIsNotNone(option, name)
            value = option.get("value") or ""
            self.assertNotIn(";;", value, name)
            self.assertNotIn(",", value, name)
            values.add(value)
        self.assertTrue(self.masks)
        self.assertEqual(
            len(values), 1,
            "разные MaskMarker маскируют разные наборы symbol layer",
        )

    def test_each_target_masks_all_its_borders(self):
        self.assertTrue(self.masks)
        option = _find_option(self.masks[0][1], "mask_symbollayers")
        parts = (option.get("value") or "").split(";")
        self.assertEqual(len(parts) % 2, 0)
        targets = {}
        for index in range(0, len(parts), 2):
            targets.setdefault(parts[index], set()).add(parts[index + 1])
        self.assertTrue(targets)
        line_classes = {
            "SimpleLine", "MarkerLine", "ArrowLine",
            "InterpolatedLine", "HashLine",
        }
        for layer_id, masked in targets.items():
            layer = self.layers[layer_id]
            name = layer.findtext("layername")
            self.assertIn(layer.get("geometry"), ("Line", "Polygon"), name)
            renderer = layer.find("renderer-v2")
            self.assertIsNotNone(renderer, name)
            actual = {
                symbol_layer.get("id")
                for symbol_layer in renderer.iter("layer")
            }
            self.assertNotIn(None, actual, name)
            self.assertLessEqual(masked, actual, name)
            borders = {
                symbol_layer.get("id")
                for symbol_layer in renderer.iter("layer")
                if symbol_layer.get("class") in line_classes
            }
            # Поверхность дороги, красящаяся по highway_integrity, маской
            # не режется: иначе дорога под значком разрывается.
            borders -= _integrity_layers(renderer)
            # Поверхность пешеходных дорог тоже остаётся целой.
            borders -= _pedestrian_surface_layers(renderer)
            self.assertLessEqual(
                borders, masked,
                f"{name}: маска должна резать все границы слоя")

    def test_pedestrian_surface_is_not_masked(self):
        """Серая поверхность пешеходной дороги под значком остаётся.

        Правило ``pedestrian`` рисует дорогу тёмной обводкой и серой
        поверхностью.  Если резать поверхность, в серой полосе под
        значком появляется светлый разрыв, как у дороги Highway main без
        исключения по ``highway_integrity``.
        """
        option = _find_option(self.masks[0][1], "mask_symbollayers")
        parts = (option.get("value") or "").split(";")
        masked = {
            (parts[index], parts[index + 1])
            for index in range(0, len(parts) - 1, 2)
        }
        found = 0
        for layer_id, layer in self.layers.items():
            renderer = layer.find("renderer-v2")
            if renderer is None:
                continue
            for symbol_layer_id in _pedestrian_surface_layers(renderer):
                self.assertNotIn(
                    (layer_id, symbol_layer_id), masked,
                    layer.findtext("layername"),
                )
                found += 1
        self.assertGreater(found, 0, "поверхность пешеходных дорог не найдена")

    def test_road_surface_is_not_masked(self):
        """Поверхность дороги под значком не вырезается.

        Маска значка вырезает кусок из целевых слоёв.  У дороги Highway
        main цвет поверхности считается по полю ``highway_integrity``, и
        если её резать, дорога под значком разрывается: под широкой
        чёрной обводкой появляется белый клин.
        """
        option = _find_option(self.masks[0][1], "mask_symbollayers")
        parts = (option.get("value") or "").split(";")
        masked = {
            (parts[index], parts[index + 1])
            for index in range(0, len(parts) - 1, 2)
        }
        found = 0
        for layer_id, layer in self.layers.items():
            for symbol_layer_id in _integrity_layers(layer):
                self.assertNotIn(
                    (layer_id, symbol_layer_id), masked,
                    layer.findtext("layername"),
                )
                found += 1
        self.assertTrue(found, "в шаблоне нет слоёв по highway_integrity")

    def test_overview_road_layer_is_a_mask_target(self):
        """На 1:200000 дорогу рисует overview-слой: он тоже в масках.

        ``highway main`` скрывается ровно на 1:200000, и обводку дороги
        под значком place of worship режет только его обзорная копия.
        Поверхность дороги при этом не режется ни там, ни здесь.
        """
        overview = next(
            layer for layer in self.layers.values()
            if layer.findtext("layername") == "highway main overview"
        )
        option = _find_option(self.masks[0][1], "mask_symbollayers")
        parts = (option.get("value") or "").split(";")
        masked = {
            parts[index + 1]
            for index in range(0, len(parts) - 1, 2)
            if parts[index] == overview.findtext("id")
        }
        borders = {
            symbol_layer.get("id")
            for symbol_layer in overview.find("renderer-v2").iter("layer")
            if symbol_layer.get("class") in (
                "SimpleLine", "MarkerLine", "ArrowLine",
                "InterpolatedLine", "HashLine",
            )
        }
        self.assertTrue(borders)
        borders -= _integrity_layers(overview.find("renderer-v2"))
        self.assertLessEqual(
            borders, masked,
            "highway main overview: обводка дороги не вырезается под значком",
        )


class ServiceRoadLodTest(unittest.TestCase):
    """Два уровня service: без service=* до 300k, с service=* до 100k."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {}
        for layer in _template_root().iter("maplayer"):
            name = layer.findtext("layername") or ""
            if layer.get("geometry") != "Line":
                continue
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

    def test_plain_service_scale_bands_follow_visibility(self):
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
            elif name == "highway main":
                # Ближний слой скрыт с 1:200000, поэтому полосы 200k+
                # в нём недостижимы и удалены.
                self.assertNotIn(200000, leaves, name)
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


class RoadLayerScaleSplitTest(unittest.TestCase):
    """Вблизи дороги рисуются одним слоем, на обзоре остаются главные.

    Правила рендерера скрывают улицы, но QGIS всё равно перебирает все
    объекты слоя. Поэтому на обзорных масштабах виден слой с подмножеством
    дорог без улиц, а улицы не читаются. `unclassified` остаётся в обзоре:
    это не внутриквартальная улица, и он нужен на 1:200000+. Вблизи,
    наоборот, нужен один слой со всеми дорогами: symbol levels кладут
    casing всех дорог под заливки, и на стыке магистрали с улицей нет
    видимой границы.
    """

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def _datasource(self, name):
        layer = self.layers.get(name)
        self.assertIsNotNone(layer, f"в шаблоне нет слоя {name}")
        return layer.findtext("datasource") or ""

    def test_close_layer_reads_all_roads(self):
        layer = self.layers.get("highway main")
        self.assertIsNotNone(layer)
        datasource = self._datasource("highway main")
        self.assertIn("highway_main.sqlite", datasource)
        self.assertNotIn("subset=", datasource)
        self.assertEqual(layer.get("hasScaleBasedVisibilityFlag"), "1")
        self.assertEqual(layer.get("minScale"), "200000")
        self.assertEqual(layer.get("maxScale"), "0")
        renderer = layer.find("renderer-v2")
        self.assertIsNotNone(renderer)
        self.assertEqual(renderer.get("symbollevels"), "1")

    def test_overview_layer_keeps_unclassified_roads(self):
        layer = self.layers.get("highway main overview")
        self.assertIsNotNone(layer)
        datasource = self._datasource("highway main overview")
        self.assertIn("highway_main.sqlite", datasource)
        self.assertIn("subset=", datasource)
        self.assertIn("NOT IN", datasource)
        self.assertIn("'service'", datasource)
        # unclassified — не улица: он должен читаться и на обзоре.
        self.assertNotIn("'unclassified'", datasource)
        self.assertEqual(layer.get("hasScaleBasedVisibilityFlag"), "1")
        self.assertEqual(layer.get("minScale"), "0")
        self.assertEqual(layer.get("maxScale"), "200000")

    def test_separate_minor_layer_is_gone(self):
        self.assertNotIn("highway main minor", self.layers)

    def test_overview_renderer_has_only_reachable_scale_bands(self):
        """В обзорном слое не должно быть веток вне его видимости."""
        layer = self.layers["highway main overview"]
        rules = layer.find("renderer-v2/rules")
        leaves = [
            rule for rule in rules.iter("rule")
            if not rule.findall("rule")
        ]
        for rule in leaves:
            minimum = int(rule.get("scalemindenom") or 0)
            maximum = int(rule.get("scalemaxdenom") or 0)
            self.assertGreaterEqual(
                minimum, 200000, rule.get("label") or rule.get("filter")
            )
            self.assertTrue(
                maximum == 0 or maximum > 200000,
                rule.get("label") or rule.get("filter"),
            )
        filters = " ".join(
            rule.get("filter") or "" for rule in rules.iter("rule")
        )
        for hidden in (
            "residential", "living_street", "pedestrian", "service",
        ):
            self.assertNotIn(hidden, filters)

    def test_overview_coarse_band_is_thinner(self):
        """На 1:700000+ линии в 1.2 раза тоньше полосы 300000–700000."""
        layer = self.layers["highway main overview"]
        renderer = layer.find("renderer-v2")
        symbols = {
            symbol.get("name"): symbol
            for symbol in renderer.find("symbols")
        }
        coarse = [
            rule for rule in renderer.iter("rule")
            if rule.get("scalemindenom") == "300000"
            and rule.get("scalemaxdenom") == "700000"
        ]
        fine = [
            rule for rule in renderer.iter("rule")
            if rule.get("scalemindenom") == "700000"
            and not rule.get("scalemaxdenom")
        ]
        self.assertTrue(coarse)
        self.assertEqual(len(coarse), len(fine))
        for rule in coarse:
            old = rule.get("symbol")
            new = "700_" + old
            self.assertIn(new, symbols, old)
            old_widths = [
                float(option.get("value"))
                for option in symbols[old].iter("Option")
                if option.get("name") == "line_width"
            ]
            new_widths = [
                float(option.get("value"))
                for option in symbols[new].iter("Option")
                if option.get("name") == "line_width"
            ]
            self.assertEqual(len(old_widths), len(new_widths), old)
            for old_width, new_width in zip(old_widths, new_widths):
                self.assertLess(new_width, old_width, old)
                self.assertAlmostEqual(
                    old_width / new_width, 1.2, delta=0.02,
                    msg=f"{old} -> {new}",
                )

    def test_blocking_layers_follow_the_split(self):
        close = self.layers.get("[blocking] highway main")
        overview = self.layers.get("[blocking] highway main overview")
        self.assertIsNotNone(close)
        self.assertIsNotNone(overview)
        self.assertNotIn("subset=", self._datasource("[blocking] highway main"))
        self.assertIn(
            "NOT IN",
            self._datasource("[blocking] highway main overview"),
        )
        self.assertNotIn(
            "'unclassified'",
            self._datasource("[blocking] highway main overview"),
        )
        self.assertEqual(close.get("hasScaleBasedVisibilityFlag"), "1")
        self.assertEqual(close.get("minScale"), "200000")
        self.assertEqual(close.get("maxScale"), "0")
        self.assertEqual(overview.get("hasScaleBasedVisibilityFlag"), "1")
        self.assertEqual(overview.get("minScale"), "0")
        self.assertEqual(overview.get("maxScale"), "200000")
        self.assertNotIn("[blocking] highway main minor", self.layers)

    def test_building_is_hidden_on_overview_scales(self):
        building = self.layers.get("building")
        self.assertIsNotNone(building)
        self.assertEqual(building.get("hasScaleBasedVisibilityFlag"), "1")
        self.assertEqual(building.get("minScale"), "120877")


class DeadScaleRuleTest(unittest.TestCase):
    """Правила не должны целиком выходить за видимость слоя."""

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    @staticmethod
    def _layer_bounds(layer):
        if layer.get("hasScaleBasedVisibilityFlag") != "1":
            return 0.0, float("inf")
        lower = float(layer.get("maxScale") or 0)
        upper = float(layer.get("minScale") or 0) or float("inf")
        return lower, upper

    @staticmethod
    def _rule_bounds(rule):
        lower = float(rule.get("scalemindenom") or 0)
        upper = float(rule.get("scalemaxdenom") or 0) or float("inf")
        return lower, upper

    def _dead_rules(self, rules, layer_lower, layer_upper):
        dead = []

        def walk(container, lower, upper, path):
            for rule in container.findall("rule"):
                rule_lower, rule_upper = self._rule_bounds(rule)
                low = max(lower, rule_lower)
                high = min(upper, rule_upper)
                title = rule.get("label") or rule.get("description") or ""
                here = path + (title,)
                if low >= high:
                    dead.append((here, low, high))
                if rule.findall("rule"):
                    walk(rule, low, high, here)

        walk(rules, layer_lower, layer_upper, ())
        return dead

    def test_no_rule_lies_outside_layer_visibility(self):
        # 5/10 м в обзорных слоях намеренно «спят» при базовой плотности:
        # обзорный слой начинается с 1:50 001, а их пороги меньше.  При
        # плотных режимах панель поднимает пороги, и правила оживают.
        dormant_layers = {
            ISOLINES_REGULAR_OVERVIEW, ISOLINES_GLACIER_OVERVIEW,
        }
        dormant_levels = {"5", "10"}
        for name, layer in self.layers.items():
            lower, upper = self._layer_bounds(layer)
            for tag in ("renderer-v2", "labeling"):
                for style in layer.iter(tag):
                    rules = style.find("rules")
                    if rules is None:
                        continue
                    dead = self._dead_rules(rules, lower, upper)
                    if name in dormant_layers:
                        dead = [
                            item for item in dead
                            if not (
                                item[0]
                                and item[0][0] in dormant_levels
                            )
                        ]
                    self.assertFalse(
                        dead, f"{name} / {tag}: {dead[:3]}"
                    )

    def test_overview_style_has_no_provider_hidden_classes(self):
        layer = self.layers["highway main overview"]
        for tag in ("renderer-v2", "labeling"):
            for style in layer.iter(tag):
                filters = " ".join(
                    rule.get("filter") or ""
                    for rule in style.iter("rule")
                )
                for hidden in (
                    "residential", "living_street", "pedestrian", "service",
                ):
                    self.assertNotIn(hidden, filters, tag)


class PackedGeometryTest(unittest.TestCase):
    """Слои, читающие упакованные файлы, объявляют составную геометрию."""

    PACKED_LINES = (
        "highway main",
        "highway main overview",
        "[blocking] highway main",
        "[blocking] highway main overview",
        "path",
        "track",
        "[blocking] track",
        "railway all",
        "[blocking] railway_all",
        "stream",
        "tree row",
        "power minor line",
        "embankment",
        "cutline",
    )
    PACKED_POLYGONS = (
        "building",
        "wood",
        "grass",
        "farmland",
        "scrub",
        "landuse residential",
        "[blocking] landuse residential",
    )
    PACKED_POINTS = ("tree",)
    PLAIN_LINES = (
        "highway main layer >=1",
        "highway main layer <=-1",
        "highway main tunnel",
        "path bridge",
        "track bridge",
        "railway_all_exploded",
        "railway disused",
        "rail tunnel",
        "railway bridge",
        "cliff",
        "steps",
        "power line",
        "barrier way",
    )

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def test_packed_lines_are_multiline(self):
        for name in self.PACKED_LINES:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            self.assertEqual(layer.get("wkbType"), "MultiLineString", name)
            self.assertIn(
                "geometrytype=MultiLineString",
                layer.findtext("datasource") or "", name,
            )

    def test_packed_polygons_are_multipolygon(self):
        for name in self.PACKED_POLYGONS:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            self.assertEqual(layer.get("wkbType"), "MultiPolygon", name)
            self.assertIn(
                "geometrytype=MultiPolygon",
                layer.findtext("datasource") or "", name,
            )

    def test_packed_points_are_multipoint(self):
        for name in self.PACKED_POINTS:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            self.assertEqual(layer.get("wkbType"), "MultiPoint", name)
            self.assertIn(
                "geometrytype=MultiPoint",
                layer.findtext("datasource") or "", name,
            )

    def test_split_layers_stay_linestring(self):
        for name in self.PLAIN_LINES:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            self.assertEqual(layer.get("wkbType"), "LineString", name)
            self.assertIn(
                "geometrytype=LineString",
                layer.findtext("datasource") or "", name,
            )


class PointClusterTest(unittest.TestCase):
    """Кластеризация точек убрана: она дороже обычной отрисовки."""

    def test_no_point_cluster_renderers(self):
        for layer in _template_root().iter("maplayer"):
            renderer = layer.find("renderer-v2")
            if renderer is None:
                continue
            self.assertNotEqual(
                renderer.get("type"), "pointCluster",
                layer.findtext("layername"),
            )


class PlaceIconSwitchTest(unittest.TestCase):
    """Мёртвый переключатель значков населённых пунктов убран."""

    def test_switch_is_absent(self):
        root = _template_root()
        for option in root.iter("Option"):
            if option.get("name") == "expression":
                self.assertNotIn(
                    "use_place_icon", option.get("value") or ""
                )
        for names in root.iter("variableNames"):
            labels = [item.text for item in names.findall("value")]
            self.assertNotIn("use_place_icon", labels)


class OnewayArrowTest(unittest.TestCase):
    """Стрелки oneway только у крупных масштабов и по классу дороги."""

    LAYERS = (
        "highway main",
        "highway main layer >=1",
        "highway main layer <=-1",
        "highway main tunnel",
    )
    THRESHOLDS = {
        "motorway": 50000,
        "trunk": 45000,
        "primary": 40000,
        "primary link": 40000,
        "primary_link": 40000,
        "secondary": 32000,
        "tertiary": 25000,
        "tertiary link": 25000,
        "tertiary_link": 25000,
        "residential": 16000,
        "service": 12000,
    }

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    @staticmethod
    def _leaves(rule, parent_max=None):
        maximum = rule.get("scalemaxdenom")
        effective = int(maximum) if maximum else parent_max
        children = rule.findall("rule")
        if not children:
            yield rule, effective
            return
        for child in children:
            yield from OnewayArrowTest._leaves(child, effective)

    @classmethod
    def _branch(cls, layer, label):
        renderer = layer.find("renderer-v2")
        for rule in renderer.find("rules").findall("rule"):
            if rule.get("label") == label:
                return rule
        return None

    @staticmethod
    def _symbol(layer, name):
        for symbol in layer.findall("renderer-v2/symbols/symbol"):
            if symbol.get("name") == name:
                return symbol
        return None

    @staticmethod
    def _has_marker(symbol):
        if symbol is None:
            return False
        for item in symbol.iter("layer"):
            if item.get("class") == "MarkerLine" and "oneway_arrow.svg" in \
                    ElementTree.tostring(item, encoding="unicode"):
                return True
        return False

    @classmethod
    def _arrow_symbol(cls, layer, label):
        branch = cls._branch(layer, label)
        threshold = cls.THRESHOLDS[label]
        if branch is None:
            return None
        for leaf, effective in cls._leaves(branch):
            if effective == threshold:
                return cls._symbol(layer, leaf.get("symbol"))
        return None

    def test_rules_split_at_class_threshold(self):
        for name in self.LAYERS:
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            for label, threshold in self.THRESHOLDS.items():
                branch = self._branch(layer, label)
                if branch is None:
                    continue
                leaves = [
                    (leaf, effective)
                    for leaf, effective in self._leaves(branch)
                    if effective is not None and effective <= 50000
                ]
                arrow = next(
                    (leaf for leaf, effective in leaves
                     if effective == threshold), None,
                )
                self.assertIsNotNone(
                    arrow, f"{name}/{label}: нет правила 0–{threshold}")
                self.assertTrue(
                    self._has_marker(
                        self._symbol(layer, arrow.get("symbol"))),
                    f"{name}/{label}: нет стрелки",
                )
                if threshold < 50000:
                    plain = next(
                        (leaf for leaf, _effective in leaves
                         if leaf.get("scalemindenom") == str(threshold)
                         and leaf.get("scalemaxdenom") == "50000"),
                        None,
                    )
                    self.assertIsNotNone(
                        plain,
                        f"{name}/{label}: нет правила {threshold}–50000",
                    )
                    self.assertFalse(
                        self._has_marker(
                            self._symbol(layer, plain.get("symbol"))),
                        f"{name}/{label}: стрелка осталась после порога",
                    )

    def test_overview_and_bridge_have_no_arrows(self):
        for name in ("highway main overview", "highway main bridge"):
            layer = self.layers.get(name)
            self.assertIsNotNone(layer, name)
            self.assertFalse([
                item for item in layer.iter("layer")
                if item.get("class") == "MarkerLine"
                and "oneway_arrow.svg" in ElementTree.tostring(
                    item, encoding="unicode")
            ], name)

    def test_marker_style_and_reverse_angle(self):
        symbol = self._arrow_symbol(self.layers["highway main"], "primary")
        self.assertIsNotNone(symbol)
        marker = next(
            item for item in symbol.iter("layer")
            if item.get("class") == "MarkerLine"
        )
        self.assertEqual(marker.get("pass"), "12")
        self.assertEqual(_option_value(marker, "placements"), "Interval")
        self.assertEqual(_option_value(marker, "rotate"), "1")
        self.assertEqual(_option_value(marker, "interval"), "20")
        self.assertEqual(_option_value(marker, "offset_along_line"), "10")
        inner = next(
            item for item in marker.iter("layer")
            if item.get("class") == "SvgMarker"
        )
        self.assertEqual(
            _option_value(inner, "name"), "../icons/oneway_arrow.svg")
        self.assertIn("0,0,0", _option_value(inner, "color"))
        self.assertEqual(_option_value(inner, "size"), "1.8")
        enabled = _option_value(
            _find_option(
                inner.find("data_defined_properties"), "enabled"
            ), "expression",
        )
        self.assertIn('"oneway"', enabled)
        self.assertIn("IN ('yes', '-1')", enabled)
        self.assertNotIn("@map_scale", enabled)
        angle = _option_value(
            _find_option(
                inner.find("data_defined_properties"), "angle"
            ), "expression",
        )
        self.assertIn('"oneway"', angle)
        self.assertIn("'-1'", angle)
        self.assertIn("0, 180", angle)
        phase = _option_value(
            _find_option(
                marker.find("data_defined_properties"),
                "offsetAlongLine",
            ), "expression",
        )
        self.assertIn("$id", phase)
        self.assertIn("% 17", phase)

    def test_arrow_size_follows_class_width(self):
        expected = {
            "motorway": "1.8",
            "tertiary": "1.5",
            "residential": "1.44",
            "service": "1.5",
        }
        layer = self.layers["highway main"]
        for label, size in expected.items():
            symbol = self._arrow_symbol(layer, label)
            self.assertIsNotNone(symbol, label)
            inner = next(
                item for item in symbol.iter("layer")
                if item.get("class") == "SvgMarker"
            )
            self.assertEqual(_option_value(inner, "size"), size, label)

    def test_arrow_icon_is_elongated(self):
        path = PACKAGE_DIR / "resources" / "icons" / "oneway_arrow.svg"
        text = path.read_text(encoding="utf-8")
        self.assertIn('viewBox="0 0 4 2"', text)


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
        for label in ("100000 - 150000", "150000 - 200000",
                      "200000 - 300000", "300000 - 500000"):
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


class StreamDryingStyleTest(unittest.TestCase):
    """Подсыхание ручьёв задано целочисленными фильтрами по dry_rank."""

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
                return StreamDryingStyleTest._find(rule, path[1:])
        return None

    def _rule(self, layer_name, path):
        layer = self.layers[layer_name]
        rule = self._find(
            layer.find("renderer-v2").find("rules"), path
        )
        self.assertIsNotNone(rule, f"{layer_name}/{'/'.join(path)}")
        return rule

    def _filter(self, layer_name, path):
        return self._rule(layer_name, path).get("filter") or ""

    def test_stream_bands_have_expected_ranks(self):
        cases = (
            ("stream", "default", (
                ("100000 - 150000", None, 3),
                ("150000 - 200000", 1, 3),
                ("200000 - 300000", 2, 4),
            )),
            ("stream", "drain or ditch", (
                ("100000 - 150000", None, 4),
                ("150000 - 200000", 1, 4),
                ("200000 - 300000", 2, 5),
            )),
            ("stream intermittent", "default", (
                ("100000 - 150000", None, 4),
                ("150000 - 200000", 1, 4),
                ("200000 - 300000", 2, 5),
            )),
        )
        for layer_name, branch, bands in cases:
            for label, rank, lod in bands:
                text = self._filter(layer_name, [branch, label])
                if rank is None:
                    self.assertNotIn("dry_rank", text)
                else:
                    self.assertIn(f'"dry_rank" >= {rank}', text)
                self.assertIn(f"@level_of_detail >= {lod}", text)

    def test_overview_bands_keep_rank_two(self):
        for layer_name, branch, label in (
            ("stream", "default", "300000 - 500000"),
            ("stream intermittent", "default", "300000 - 500000"),
            ("stream", "drain or ditch", "200000 - 300000"),
        ):
            self.assertIn(
                '"dry_rank" >= 2',
                self._filter(layer_name, [branch, label]),
            )

    def test_scale_ranges_cover_100k_to_500k_without_gaps(self):
        expected = {
            "100000 - 150000": ("100000", "150000"),
            "150000 - 200000": ("150000", "200000"),
            "200000 - 300000": ("200000", "300000"),
            "300000 - 500000": ("300000", "500000"),
        }
        for layer_name, branch in (
            ("stream", "default"),
            ("stream", "drain or ditch"),
            ("stream intermittent", "default"),
        ):
            for label, (minimum, maximum) in expected.items():
                if label == "300000 - 500000" and branch == "drain or ditch":
                    self.assertIsNone(self._find(
                        self.layers[layer_name].find("renderer-v2").find(
                            "rules"
                        ),
                        [branch, label],
                    ))
                    continue
                rule = self._rule(layer_name, [branch, label])
                self.assertEqual(rule.get("scalemindenom"), minimum)
                self.assertEqual(rule.get("scalemaxdenom"), maximum)

    def test_below_100k_has_no_drying(self):
        for layer_name, branch, label in (
            ("stream", "default", "50000 - 100000"),
            ("stream", "drain or ditch", "50000 - 100000"),
            ("stream intermittent", "default", "50000 - 100000"),
        ):
            self.assertNotIn(
                "dry_rank",
                self._filter(layer_name, [branch, label]),
            )

    def test_arrows_dry_with_the_stream(self):
        layer = self.layers["stream arrows"]
        rules = layer.find("renderer-v2").find("rules").findall("rule")
        by_scale = {
            (rule.get("scalemindenom"), rule.get("scalemaxdenom")): rule
            for rule in rules
        }
        self.assertNotIn(
            "dry_rank",
            by_scale[("100000", "150000")].get("filter") or "",
        )
        self.assertIn(
            '"dry_rank" >= 1',
            by_scale[("150000", "200000")].get("filter") or "",
        )
        self.assertIn(
            '"dry_rank" >= 2',
            by_scale[("200000", "300000")].get("filter") or "",
        )
        self.assertIn(
            '"dry_rank" >= 2',
            by_scale[("300000", None)].get("filter") or "",
        )

    def test_drystream_is_not_changed(self):
        layer = self.layers["drystream"]
        for rule in layer.find("renderer-v2").iter("rule"):
            self.assertNotIn("dry_rank", rule.get("filter") or "")


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
        ranks = [
            layer.rank for layer in self.layers
            if layer.name not in poi_icons.LOWEST_ICON_LAYERS
        ]
        # Ранг — место слоя в дереве проекта, поэтому между соседними
        # POI могут стоять другие слои и номера идут с пропусками.
        # Исключение — значок кладбища: он стоит в дереве выше карты,
        # а ранг получает самый низкий.
        self.assertEqual(ranks, sorted(ranks))
        ranks = [layer.rank for layer in self.layers]
        self.assertEqual(len(set(ranks)), len(ranks))

    def test_places_main_major_is_above_mountains(self):
        """Города и посёлки не вытесняются пиками и вулканами.

        Приоритет значка задаёт место слоя в дереве проекта.  Слой
        city/town стоит выше volcano, volcano dirt и peak.
        """
        ranks = {layer.name: layer.rank for layer in self.layers}
        for name in ("volcano", "volcano dirt", "peak"):
            self.assertLess(
                ranks["places main major"], ranks[name], name,
            )

    def test_places_main_stays_below_peaks(self):
        """Деревни и хутора по-прежнему уступают пикам."""
        ranks = {layer.name: layer.rank for layer in self.layers}
        for name in ("volcano", "volcano dirt", "peak"):
            self.assertGreater(ranks["places main"], ranks[name], name)

    def test_huts_are_below_locality(self):
        """Урочище не перекрывается домиками для отдыха."""
        ranks = {layer.name: layer.rank for layer in self.layers}
        for name in ("alpine hut", "wilderness hut"):
            self.assertGreater(ranks[name], ranks["place locality point"], name)

    def test_city_is_not_suppressed_by_peak(self):
        """Город рядом с пиком остаётся, пик уступает городу."""
        from qgis_xtopo.core.poi_icons import (
            _Point, _suppression_distances,
        )

        ranks = {layer.name: layer.rank for layer in self.layers}
        city = _Point(
            rank=ranks["places main major"] + 0.1, lon=6.9, lat=46.05,
            name="places main major", fid=1, bands={0: 2}, sizes={0: 2.0},
        )
        peak = _Point(
            rank=ranks["peak"], lon=6.9005, lat=46.05,
            name="peak", fid=2, bands={0: 2}, sizes={0: 2.0},
        )
        distances = _suppression_distances(
            [city, peak], {0: [city, peak]}, [(0.0, 20000.0)],
        )
        self.assertNotIn(("places main major", 1), distances)
        self.assertIn(("peak", 2), distances)

    def test_village_is_still_suppressed_by_peak(self):
        """Деревня рядом с пиком по-прежнему уступает ему."""
        from qgis_xtopo.core.poi_icons import (
            _Point, _suppression_distances,
        )

        ranks = {layer.name: layer.rank for layer in self.layers}
        village = _Point(
            rank=ranks["places main"] + 0.1, lon=6.9, lat=46.05,
            name="places main", fid=1, bands={0: 2}, sizes={0: 2.0},
        )
        peak = _Point(
            rank=ranks["peak"], lon=6.9005, lat=46.05,
            name="peak", fid=2, bands={0: 2}, sizes={0: 2.0},
        )
        distances = _suppression_distances(
            [village, peak], {0: [village, peak]}, [(0.0, 20000.0)],
        )
        self.assertIn(("places main", 1), distances)
        self.assertNotIn(("peak", 2), distances)

    def test_unnamed_peak_does_not_suppress_village(self):
        """Безымянный пик не скрывает даже деревню."""
        from qgis_xtopo.core.poi_icons import (
            _Point, _lower_unnamed_peaks, _suppression_distances,
        )

        ranks = {layer.name: layer.rank for layer in self.layers}
        village = _Point(
            rank=ranks["places main"] + 0.1, lon=6.9, lat=46.05,
            name="places main", fid=1, bands={0: 2}, sizes={0: 2.0},
        )
        peak = _Point(
            rank=ranks["peak"], lon=6.9005, lat=46.05,
            name="peak", fid=2, named=False,
            bands={0: 2}, sizes={0: 2.0},
        )
        _lower_unnamed_peaks([village, peak])
        self.assertGreater(peak.rank, village.rank)
        distances = _suppression_distances(
            [village, peak], {0: [village, peak]}, [(0.0, 20000.0)],
        )
        self.assertNotIn(("places main", 1), distances)
        self.assertIn(("peak", 2), distances)

    def test_peak_name_tags_detected(self):
        """Именным считается пик с name, alt_name или name:*."""
        from qgis.core import QgsFeature, QgsField, QgsFields
        from qgis.PyQt.QtCore import QVariant

        fields = QgsFields()
        fields.append(QgsField("name", QVariant.String))
        fields.append(QgsField("alt_name", QVariant.String))
        fields.append(QgsField("name:ru", QVariant.String))
        feature = QgsFeature(fields)
        self.assertFalse(poi_icons._feature_is_named(feature))

        feature.setAttribute("name:ru", "Эльбрус")
        self.assertTrue(poi_icons._feature_is_named(feature))
        feature.setAttribute("name:ru", None)
        feature.setAttribute("alt_name", "Elbrus")
        self.assertTrue(poi_icons._feature_is_named(feature))
        feature.setAttribute("alt_name", "")
        feature.setAttribute("name", "Matterhorn")
        self.assertTrue(poi_icons._feature_is_named(feature))

    def test_all_icon_layers_use_one_crs(self):
        """Все слои со значками — в EPSG:4326.

        Расстояние подавления задано в градусах; слой в метрах
        (EPSG:3857) сравнивал бы его с метрами и не находил соседей.
        """
        for layer in self.layers:
            self.assertEqual(layer.crs, "EPSG:4326", layer.name)

    def test_service_layer_names_are_unique(self):
        """Имена служебных слоёв уникальны.

        Два слоя с одинаковым именем (4326 и 3857) делали выбор
        соседей неоднозначной: первым находился слой не той системы
        координат, и вытеснение не работало.
        """
        names = [
            layer.findtext("layername") or ""
            for layer in self.root.iter("maplayer")
        ]
        service = [name for name in names if name.startswith("poi icons")]
        self.assertEqual(len(service), len(set(service)), service)

    def test_service_layers_have_spatial_index(self):
        """Служебный слой лежит в GeoPackage: у SQLite нет индекса."""
        for layer in self.root.iter("maplayer"):
            name = layer.findtext("layername") or ""
            if not name.startswith("poi icons"):
                continue
            source = layer.findtext("datasource") or ""
            self.assertIn(".gpkg", source)

    def test_icon_rules_suppress_overlaps(self):
        """Подавление значков берётся из данных слоя.

        Порог до ближайшего подавителя лежит в поле ``hide`` слоя:
        правило сравнивает его с радиусом подавления.  Выражений с
        overlay_nearest в правилах нет — они вызывались на каждый
        объект слоя и делали отрисовку медленной.
        """
        checked = 0
        for layer in self.layers:
            for leaf in poi_icons.leaves(layer.root):
                if not poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                ):
                    # Правило не рисуется ни в одном диапазоне.
                    continue
                self.assertIn(
                    f'"{poi_icons.hide_field_name()}"', leaf.filter,
                    f"у слоя {layer.name} нет подавления значков",
                )
                self.assertNotIn("overlay_nearest(", leaf.filter)
                self.assertIn("@level_of_detail", leaf.filter)
                checked += 1
        self.assertTrue(checked, "в шаблоне нет правил значков POI")

    def test_icon_rules_use_band_thresholds(self):
        """Каждому диапазону масштаба — свой порог.

        Набор видимых соседей зависит от диапазона масштаба, поэтому
        и порог в данных свой у каждого диапазона: слот в поле слоя
        выбирается по номеру диапазона.  Порядок слотов общий для всех
        слоёв: два слоя могут читать один и тот же файл.
        """
        for layer in self.layers:
            for leaf in poi_icons.leaves(layer.root):
                used = poi_icons.band_indexes(
                    leaf.minimum, leaf.maximum, self.bands,
                )
                for index in used:
                    offset = poi_icons.hide_slot_index(index)
                    self.assertIn(
                        f"{offset} + @level_of_detail", leaf.filter,
                        f"у слоя {layer.name} неверный слот порога",
                    )

    def test_every_icon_layer_is_suppressed(self):
        """Подавление есть у всех слоёв POI, включая places main.

        Раньше у places main его не было: десятки выражений с
        overlay_nearest делали слой неотрисовываемым.  Теперь пороги
        лежат в данных слоя, и выражение в правиле — простая проверка
        поля, поэтому slowdown больше нет.
        """
        names = {layer.name for layer in self.layers}
        self.assertIn("places main", names)
        for layer in self.layers:
            if layer.name != "places main":
                continue
            leaves = poi_icons.leaves(layer.root)
            self.assertTrue(leaves)
            for leaf in leaves:
                self.assertIn(
                    f'"{poi_icons.hide_field_name()}"', leaf.filter,
                    "у places main нет подавления значков",
                )
            for rule in layer.labels:
                self.assertIn(
                    f'"{poi_icons.hide_field_name()}"', rule.filter,
                    "у places main нет подавления подписей",
                )

    def test_label_rules_suppress_overlaps(self):
        """Подпись скрывается вместе со значком — по тем же данным."""
        checked = 0
        for layer in self.layers:
            for rule in layer.labels:
                expected = poi_icons.layer_suppression_data_expression(
                    layer, self.bands, rule.minimum, rule.maximum,
                )
                if expected == "false":
                    # В этом диапазоне значок не рисуется: подавлять
                    # подписи нечем.
                    continue
                self.assertIn(
                    f'"{poi_icons.hide_field_name()}"', rule.filter,
                    f"у слоя {layer.name} нет подавления подписей",
                )
                self.assertNotIn("overlay_nearest(", rule.filter)
                checked += 1
        self.assertTrue(checked, "в шаблоне нет подписей POI")


class PoiScaleBoundaryTest(unittest.TestCase):
    """На границе полосы правило не показывает все значки подряд."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()
        layers = poi_icons.read_template_poi()
        cls.layer = next(
            layer for layer in layers
            if layer.name == "tower communication"
        )
        cls.leaves = poi_icons.leaves(cls.layer.root)

    @staticmethod
    def _visible(leaf, scale, hide=""):
        from qgis.core import (
            QgsExpression, QgsExpressionContext, QgsExpressionContextScope,
            QgsFeature, QgsField, QgsFields,
        )
        from qgis.PyQt.QtCore import QVariant

        fields = QgsFields()
        fields.append(QgsField("hide", QVariant.String))
        feature = QgsFeature(fields)
        feature.setAttribute(
            "hide", ";".join([hide] * 90) if hide else "",
        )
        context = QgsExpressionContext()
        scope = QgsExpressionContextScope("test")
        scope.setVariable("map_scale", scale)
        scope.setVariable("level_of_detail", 4)
        context.appendScope(scope)
        context.setFeature(feature)
        return bool(QgsExpression(leaf.filter).evaluate(context))

    def _leaf(self, minimum, maximum):
        return next(
            leaf for leaf in self.leaves
            if leaf.minimum == minimum and leaf.maximum == maximum
        )

    def test_boundary_keeps_band_suppression(self):
        """На 1:100000 активное правило находит свою полосу."""
        previous = self._leaf(50000.0, 100000.0)
        current = self._leaf(100000.0, 300000.0)
        # Значок не пропадает: допуск принимает границу.
        self.assertTrue(self._visible(previous, 100000))
        # Но и не рисуется без вытеснения: полоса читает свой слот.
        self.assertFalse(self._visible(previous, 100000, hide="0"))
        self.assertTrue(self._visible(previous, 99999))
        self.assertFalse(self._visible(current, 99999))
        self.assertTrue(self._visible(current, 100000))

    def test_boundary_keeps_finest_suppression(self):
        """Ровно 1:50000 — допуск у верхней границы полосы."""
        finest = self._leaf(0.0, 50000.0)
        next_band = self._leaf(50000.0, 100000.0)
        self.assertTrue(self._visible(finest, 50000))
        self.assertFalse(self._visible(finest, 50000, hide="0"))
        self.assertTrue(self._visible(next_band, 50000))


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
        bridge = self._point(6.9, 46.05, rank=3)
        shelter = self._point(6.9002, 46.05, rank=8)
        kept = self._kept([bridge, shelter])
        self.assertIn(bridge, kept)
        self.assertNotIn(shelter, kept)

    def test_obstacle_without_points_is_dropped(self):
        bridge = self._point(6.9, 46.05, rank=3)
        far = self._point(6.94, 46.05, rank=8)
        self.assertEqual(self._kept([bridge, far]), [])


class PoiSuppressionDataTest(unittest.TestCase):
    """Пороги подавления считаются при подготовке данных.

    Правило рендерера читает порог из поля слоя, поэтому решает,
    кто кому мешает, расчёт в `core/poi_icons.py`: ближайший
    подавитель ищется среди точек, стоящих раньше по рангу и
    координатам.
    """

    # Диапазон с верхней границей: радиус подавления ограничен
    # размером значка и этой границей.
    BANDS = [(0.0, 20000.0)]

    def _point(self, lon, lat, rank, fid, band=0, lod=2, name="peak",
               geometry="Point"):
        from qgis_xtopo.core.poi_icons import _Point

        return _Point(
            rank=rank,
            lon=lon,
            lat=lat,
            name=name,
            fid=fid,
            geometry=geometry,
            bands={band: lod},
            sizes={band: 2.0},
        )

    def _distances(self, points):
        records = {0: list(points)}
        return poi_icons._suppression_distances(
            points, records, self.BANDS,
        )

    def test_same_layer_tie_breaks_by_coordinates(self):
        """Значки одного слоя: побеждает тот, кто стоит раньше."""
        west = self._point(6.9, 46.05, 5, 1)
        east = self._point(6.9005, 46.05, 5, 2)
        distances = self._distances([west, east])
        self.assertNotIn(("peak", 1), distances)
        self.assertIn(("peak", 2), distances)
        threshold = distances[("peak", 2)][0][2]
        # Пик в полуторастах метрах от соседнего.
        self.assertTrue(30 < threshold < 60, threshold)

    def test_lower_priority_is_suppressed(self):
        """Точка низшего приоритета скрывается рядом с важной."""
        top = self._point(6.9, 46.05, 3, 1, name="volcano")
        low = self._point(6.9005, 46.05, 9, 2, name="peak")
        distances = self._distances([top, low])
        self.assertNotIn(("volcano", 1), distances)
        self.assertIn(("peak", 2), distances)

    def test_lower_obstacle_does_not_suppress_point(self):
        """Мост ниже слоя POI не скрывает его значок."""
        bridge = self._point(
            6.9, 46.05, 159, 1,
            name="highway main bridge", geometry="Line",
        )
        settlement = self._point(
            6.9005, 46.05, 5, 2, name="places main",
        )
        distances = self._distances([bridge, settlement])
        self.assertNotIn(("places main", 2), distances)

    def test_higher_obstacle_still_suppresses_point(self):
        """Препятствие выше слоя POI по-прежнему его вытесняет."""
        bridge = self._point(
            6.9, 46.05, 3, 1,
            name="highway main bridge", geometry="Line",
        )
        poi = self._point(6.9005, 46.05, 8, 2, name="peak")
        distances = self._distances([bridge, poi])
        self.assertIn(("peak", 2), distances)

    def test_another_settlement_still_suppresses_city(self):
        """Более важный населённый пункт город по-прежнему вытесняет."""
        city = self._point(6.9, 46.05, 5.0, 1, name="places main")
        town = self._point(6.9005, 46.05, 5.1, 2, name="places main")
        distances = self._distances([city, town])
        self.assertIn(("places main", 2), distances)

    def test_level_of_detail_hides_late_candidate(self):
        """Подавитель, видимый только на высоком уровне, не мешает.

        На уровне детализации 2 такой подавитель не нарисован, значит
        и не вытесняет: порог для уровня 2 остаётся пустым.
        """
        top = self._point(6.9, 46.05, 3, 1, name="volcano", lod=5)
        low = self._point(6.9005, 46.05, 9, 2, name="peak")
        distances = self._distances([top, low])
        levels = distances[("peak", 2)][0]
        self.assertNotIn(2, levels)
        self.assertNotIn(4, levels)
        self.assertIn(5, levels)
        self.assertIn(6, levels)

    def test_far_point_is_not_suppressed(self):
        """Сосед за пределами радиуса подавления не мешает."""
        top = self._point(6.9, 46.05, 3, 1, name="volcano")
        low = self._point(6.95, 46.05, 9, 2, name="peak")
        distances = self._distances([top, low])
        self.assertNotIn(("peak", 2), distances)

    def test_coincident_point_does_not_suppress(self):
        """Точка на том же месте не вытесняет.

        Выражение шаблона отбрасывает кандидатов, совпадающих с
        родителем по координатам: иначе значки разных слоёв в одной
        точке гасили бы друг друга всегда.
        """
        top = self._point(6.9, 46.05, 3, 1, name="volcano")
        low = self._point(6.9, 46.05, 9, 2, name="peak")
        distances = self._distances([top, low])
        self.assertNotIn(("peak", 2), distances)

    def test_base_filter_drops_suppression(self):
        """Условие подавления снимается с фильтра правила."""
        suppression = "a"
        self.assertEqual(
            poi_icons.base_filter("NOT (a)", suppression), ""
        )
        self.assertEqual(
            poi_icons.base_filter("(x) AND NOT (a)", suppression), "x"
        )
        # Подавления в фильтре нет — слой без вытеснения.
        self.assertIsNone(poi_icons.base_filter("(x)", suppression))

    def test_data_filter_keeps_base_condition(self):
        """Новое условие не теряет исходную видимость объекта."""
        self.assertEqual(
            poi_icons.data_filter("x", "b"), "(x) AND NOT (b)",
        )
        self.assertEqual(
            poi_icons.data_filter("", "b"), "NOT (b)",
        )


class SettlementPriorityTest(unittest.TestCase):
    """Приоритет населённого пункта внутри слоя places main.

    Все точки слоя имели один ранг — места в дереве проекта, — и
    внутри слоя решали только координаты.  Теперь ранг учитывает тип
    места и население: город вытесняет деревню, а внутри одного типа
    — тот, у кого больше население.
    """

    @staticmethod
    def _feature(place, population=None):
        from qgis.core import QgsFeature, QgsField, QgsFields
        from qgis.PyQt.QtCore import QVariant

        fields = QgsFields()
        fields.append(QgsField("place", QVariant.String))
        fields.append(QgsField("population", QVariant.String))
        feature = QgsFeature(fields)
        feature.setAttribute("place", place)
        feature.setAttribute("population", population)
        return feature

    def _priority(self, place, population=None):
        return poi_icons._settlement_priority(
            self._feature(place, population),
        )

    def test_type_beats_position_and_population(self):
        """Город важнее деревни, даже если у деревни больше людей."""
        city = self._priority("city", 5000)
        village = self._priority("village", 50000)
        self.assertLess(city, village)

    def test_hamlet_beats_isolated_dwelling(self):
        self.assertLess(
            self._priority("hamlet"), self._priority("isolated_dwelling"),
        )
        self.assertLess(
            self._priority("village"), self._priority("hamlet"),
        )
        self.assertLess(
            self._priority("town"), self._priority("village"),
        )

    def test_quarter_sits_between_suburb_and_neighbourhood(self):
        self.assertLess(
            self._priority("suburb"), self._priority("quarter"),
        )
        self.assertLess(
            self._priority("quarter"), self._priority("neighbourhood"),
        )
        self.assertLess(
            self._priority("neighbourhood"),
            self._priority("isolated_dwelling"),
        )

    def test_population_breaks_ties_of_one_type(self):
        """Внутри одного типа важнее тот, у кого больше население."""
        big = self._priority("village", 5000)
        small = self._priority("village", 100)
        self.assertLess(big, small)

    def test_place_without_type_ranks_by_population(self):
        """Пункт без типа места получает ранг по населению."""
        big = self._priority("", 200000)
        small = self._priority("", 50)
        self.assertLess(big, small)
        self.assertLess(big, self._priority("village", 1000))

    def test_ranks_stay_inside_the_layer_slot(self):
        """Ранг точки не выходит за пределы места слоя в дереве.

        Иначе точки слоя вытесняли бы значки слоёв, которые стоят
        ниже, — или наоборот.
        """
        from qgis_xtopo.core.poi_icons import _Point

        points = [
            _Point(rank=5.0, lon=6.9, lat=46.0, name="places main", fid=1),
            _Point(rank=5.0, lon=6.91, lat=46.0, name="places main", fid=2),
        ]
        # fid=1 — хутор, fid=2 — город: город должен получить ранг
        # меньше, хотя стоит восточнее.
        poi_icons._apply_priorities(
            points, [(3, -100.0), (0, -5000.0)], 5.0,
        )
        self.assertLess(points[1].rank, points[0].rank)
        for point in points:
            self.assertTrue(5.0 < point.rank < 6.0, point.rank)

    def test_layer_is_in_the_list(self):
        self.assertIn("places main", poi_icons.SELF_RANKED_LAYERS)
        self.assertIn("places main major", poi_icons.SELF_RANKED_LAYERS)


class BridgeIconLayersTest(unittest.TestCase):
    """Значки мостов — обычные точечные слои, как остальные POI."""

    NAMES = (
        "highway main bridge icons",
        "track bridge icons",
        "path bridge icons",
        "railway bridge icons",
    )

    def test_layers_are_points_in_poi_list(self):
        layers = {
            layer.name: layer for layer in poi_icons.read_template_poi()
        }
        for name in self.NAMES:
            self.assertIn(name, layers)
            layer = layers[name]
            self.assertEqual(layer.geometry, "Point", name)
            self.assertGreater(layer.icon_size, 0.0, name)
            self.assertGreater(layer.rank, 0.0, name)
            self.assertIn("bridge_icons.sqlite", layer.source, name)
            self.assertEqual(layer.crs, "EPSG:4326", name)

    def test_line_layers_keep_lines_without_icons(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        for name in (
            "highway main bridge", "track bridge",
            "path bridge", "railway bridge",
        ):
            layer = layers[name]
            self.assertEqual(layer.get("geometry"), "Line", name)
            renderer = layer.find("renderer-v2")
            for symbol in renderer.findall("symbols/symbol"):
                classes = {
                    item.get("class") for item in symbol.iter("layer")
                }
                self.assertNotIn("SvgMarker", classes, (name, symbol.get("name")))
            self.assertNotIn(
                'description="icon"',
                ElementTree.tostring(layer, encoding="unicode"),
                name,
            )

    def test_cemetery_icon_has_lowest_rank(self):
        """Значок кладбища уступает всем, но стоит выше карты."""
        layers = poi_icons.read_template_poi()
        by_name = {layer.name: layer for layer in layers}
        icon = by_name["cemetery icons"]
        others = [
            layer.rank for layer in layers
            if layer.name != "cemetery icons"
        ]
        self.assertEqual(icon.rank, max(others) + 1)
        self.assertEqual(icon.geometry, "Point")


class BridgeIconSuppressionTest(unittest.TestCase):
    """Значки мостов вытесняются тем же полем hide, что и остальные POI."""

    NAMES = (
        "highway main bridge icons",
        "track bridge icons",
        "path bridge icons",
        "railway bridge icons",
    )

    @classmethod
    def setUpClass(cls):
        cls.xml = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        cls.poi = {
            layer.name: layer for layer in poi_icons.read_template_poi()
        }

    @staticmethod
    def _find(rule, predicate):
        if predicate(rule):
            return rule
        for child in rule.findall("rule"):
            found = BridgeIconSuppressionTest._find(child, predicate)
            if found is not None:
                return found
        return None

    @staticmethod
    def _leaves(rule):
        result = []
        for child in rule.findall("rule"):
            if child.findall("rule"):
                result.extend(BridgeIconSuppressionTest._leaves(child))
            else:
                result.append(child)
        return result

    def test_all_icon_rules_use_hide(self):
        for name in self.NAMES:
            info = self.poi[name]
            leaves = poi_icons.leaves(info.root)
            self.assertTrue(leaves, name)
            for leaf in leaves:
                self.assertIn('"hide"', leaf.filter, name)
                self.assertIn("@map_scale", leaf.filter, name)
                self.assertNotIn("icon_hide", leaf.filter, name)

    def test_service_bridge_icons_stop_at_200k(self):
        layer = self.xml["highway main bridge icons"]
        service = self._find(
            layer.find("renderer-v2/rules"),
            lambda rule: rule.get("filter") == "highway = 'service'",
        )
        self.assertIsNotNone(service)
        plain = self._find(service, lambda rule: rule.get("label") == "else")
        self.assertIsNotNone(plain)
        leaves = self._leaves(plain)
        self.assertTrue(leaves)
        for leaf in leaves:
            maximum = int(leaf.get("scalemaxdenom") or 0)
            self.assertTrue(maximum, leaf.get("label"))
            self.assertLessEqual(maximum, 200000, leaf.get("label"))
        self.assertTrue(any(
            int(leaf.get("scalemindenom") or 0) < 200000
            for leaf in leaves
        ))

    def test_icons_stop_at_800k(self):
        for name in self.NAMES:
            layer = self.xml[name]
            symbols = {
                symbol.get("name"): symbol
                for symbol in layer.findall("renderer-v2/symbols/symbol")
            }
            found = False
            for leaf in layer.find("renderer-v2/rules").iter("rule"):
                if leaf.findall("rule") or not leaf.get("symbol"):
                    continue
                symbol = symbols.get(leaf.get("symbol"))
                if symbol is None:
                    continue
                if not any(
                    item.get("class") == "SvgMarker"
                    for item in symbol.iter("layer")
                ):
                    continue
                if leaf.get("scalemaxdenom"):
                    maximum = int(leaf.get("scalemaxdenom"))
                    self.assertLessEqual(maximum, 800000, (name, leaf.get("label")))
                else:
                    self.assertIn(
                        "@map_scale < 800000", leaf.get("filter") or "",
                        (name, leaf.get("label")),
                    )
                    found = True
            if name in ("highway main bridge icons", "railway bridge icons"):
                self.assertTrue(found, name)


class CemeteryIconSuppressionTest(unittest.TestCase):
    """Значок кладбища — обычная точка, вытесняется полем hide."""

    @classmethod
    def setUpClass(cls):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        cls.layer = layers["cemetery icons"]
        cls.fill_layer = layers["cemetery"]
        cls.info = next(
            layer for layer in poi_icons.read_template_poi()
            if layer.name == "cemetery icons"
        )

    def test_layer_is_built_from_generated_points(self):
        """Шаблон читает точечный файл, а не полигоны кладбищ."""
        self.assertEqual(self.layer.get("geometry"), "Point")
        self.assertEqual(self.layer.get("wkbType"), "Point")
        source = self.layer.findtext("datasource")
        self.assertIn("cemetery_icons.sqlite", source)
        self.assertIn("geometrytype=Point", source)
        symbols = {
            symbol.get("name"): symbol
            for symbol in self.layer.findall("renderer-v2/symbols/symbol")
        }
        for symbol in symbols.values():
            classes = [layer.get("class") for layer in symbol.findall("layer")]
            self.assertIn("SvgMarker", classes)
            self.assertNotIn("CentroidFill", classes)

    def test_icon_is_used_only_up_to_100k(self):
        """После 1:100000 значка нет: остаётся заливка и штриховка."""
        groups = self.layer.findall("renderer-v2/rules/rule")
        self.assertEqual(len(groups), 2)
        groups = [
            group for group in groups
            if group.get("checkstate") != "0"
        ]
        self.assertEqual(
            [group.get("scalemaxdenom") for group in groups],
            ["50000", "100000"],
        )
        for group in groups:
            (icon_rule,) = group.findall("rule")
            self.assertIn("area_m2", icon_rule.get("filter"))
            self.assertIn("@map_scale", icon_rule.get("filter"))
            self.assertIn('"hide"', icon_rule.get("filter"))
            self.assertIn("@level_of_detail", icon_rule.get("filter"))

    def test_icon_rotates_by_prepared_angle(self):
        """Поворот значка берётся из поля icon_angle, а не из площадки."""
        symbols = {
            symbol.get("name"): symbol
            for symbol in self.layer.findall("renderer-v2/symbols/symbol")
        }
        checked = 0
        for name, symbol in symbols.items():
            marker = next(
                layer for layer in symbol.findall("layer")
                if layer.get("class") == "SvgMarker"
            )
            properties = marker.find(
                'data_defined_properties/Option/Option[@name="properties"]'
            )
            self.assertIsNotNone(properties, name)
            angle = properties.find('Option[@name="angle"]')
            self.assertIsNotNone(angle, name)
            expression = angle.find('Option[@name="expression"]').get("value")
            self.assertEqual(expression, '"icon_angle"')
            self.assertNotIn("main_angle", expression)
            checked += 1
        self.assertTrue(checked, "у кладбищ нет значка с поворотом")

    def test_fill_layer_has_no_centroid_icon(self):
        """Заливка кладбища больше не рисует значок в центре."""
        for symbol in self.fill_layer.findall(
                "renderer-v2/symbols/symbol"):
            classes = [layer.get("class") for layer in symbol.findall("layer")]
            self.assertNotIn("CentroidFill", classes)


class PoiSuppressionExpressionTest(unittest.TestCase):
    """Прежнее выражение подавления: вытесняет значки и внутри слоя.

    Оно осталось в коде для снятия условия, если попадётся
    непереведённый шаблон.  В правилах шаблона выражения больше нет:
    подавление считается при подготовке данных, см. `PoiSuppressionDataTest`.

    Прежняя схема ссылалась на geometry(@parent) внутри overlay_nearest,
    но @parent в его аргументах недоступен (NULL): значки одного слоя не
    вытесняли друг друга.  Родительская точка передаётся через
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
        rank = int(self.layer.rank)
        visible = self._visible(
            [(6.9000, 46.05, rank), (6.9001, 46.05, rank)],
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
            [(6.9000, 46.05, int(self.layer.rank))],
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


class IconAngleTest(unittest.TestCase):
    """Углы поворота и теги значков берутся из готовых полей.

    Раньше угол перевала и станции считался выражением в шаблоне: при
    каждой отрисовке QGIS строил индекс всего слоя гребней или рельсов.
    Теперь угол пишется в поле `icon_angle` при подготовке, а в шаблоне
    остаётся только обращение к полю.  Значок станции выбирается так же:
    в поле `icon` лежит путь к значку.
    """

    LAYERS = ("mountain pass", "railway station icons", "railway halt icons")

    @classmethod
    def setUpClass(cls):
        cls.root = _template_root()

    def _layer(self, name):
        for layer in self.root.iter("maplayer"):
            if layer.findtext("layername") == name:
                return layer
        return None

    def _expressions(self, layer):
        expressions = []
        for symbol in layer.iter("symbol"):
            for option in symbol.iter("Option"):
                if option.get("name") != "expression":
                    continue
                value = option.get("value") or ""
                if "icon_angle" in value or "icon_building" in value:
                    expressions.append(value)
                elif "overlay_nearest" in value:
                    expressions.append(value)
        return expressions

    def _marker_properties(self, layer):
        """Свойства значков слоя: имя свойства -> выражение."""
        properties = []
        for symbol in layer.iter("symbol"):
            for marker in symbol.findall("layer"):
                if marker.get("class") != "SvgMarker":
                    continue
                block = marker.find(
                    'data_defined_properties/Option/Option[@name="properties"]'
                )
                if block is None:
                    continue
                values = {}
                for option in block.findall("Option"):
                    expression = option.find('Option[@name="expression"]')
                    if expression is None:
                        continue
                    values[option.get("name")] = expression.get("value") or ""
                properties.append(values)
        return properties

    def test_angles_use_ready_field(self):
        for name in self.LAYERS:
            layer = self._layer(name)
            self.assertIsNotNone(layer, name)
            expressions = self._expressions(layer)
            self.assertTrue(expressions, name)
            for expression in expressions:
                self.assertNotIn("overlay_nearest", expression)
                self.assertIn("icon_angle", expression)

    def test_station_icon_uses_ready_field(self):
        """Значок вокзала выбирается готовым полем, а не выражением."""
        layer = self._layer("railway station icons")
        properties = self._marker_properties(layer)
        self.assertTrue(properties)
        for values in properties:
            self.assertEqual(values.get("name"), '"icon"')
            self.assertEqual(values.get("angle"), '"icon_angle"')
            for expression in values.values():
                self.assertNotIn("overlay_nearest", expression)
                self.assertNotIn("icon_building", expression)

    def test_station_layer_reads_prepared_points(self):
        """Слой значков читает готовый точечный файл, а не центроиды."""
        layer = self._layer("railway station icons")
        source = layer.findtext("datasource")
        self.assertIn("railway_station_icons.sqlite", source)
        self.assertIn("geometrytype=Point", source)
        self.assertEqual(layer.get("geometry"), "Point")
        self.assertEqual(layer.get("wkbType"), "Point")

    def test_pass_keeps_rotation_switch(self):
        layer = self._layer("mountain pass")
        expressions = self._expressions(layer)
        for expression in expressions:
            if "icon_angle" in expression:
                self.assertIn("@rotate_mountain_pass", expression)

    def test_pass_field_uses_ready_value(self):
        layer = self._layer("mountain pass")
        fields = [
            field for field in layer.iter("field")
            if field.get("name") == "rotation"
        ]
        self.assertTrue(fields)
        for field in fields:
            expression = field.get("expression") or ""
            if not expression:
                continue
            self.assertIn("icon_angle", expression)
            self.assertNotIn("geomdistance", expression)


class BuildingLodTest(unittest.TestCase):
    """Мелкие здания скрываются по уровню детализации."""

    THRESHOLDS = {5: 50, 4: 100, 3: 175}
    FALLBACK = 300

    @classmethod
    def setUpClass(cls):
        layer = next(
            element for element in _template_root().iter("maplayer")
            if element.findtext("layername") == "building"
        )
        cls.rules = layer.find("renderer-v2").find("rules")
        cls.parent = cls.rules.find("rule")
        cls.children = {
            rule.get("label"): rule.get("filter") or ""
            for rule in cls.parent.findall("rule")
        }

    def test_single_parent_rule_depends_on_lod(self):
        self.assertEqual(
            [rule.get("label") for rule in self.rules.findall("rule")],
            ["lod"],
        )
        text = self.parent.get("filter") or ""
        self.assertIn("@map_scale <= 32000", text)
        self.assertIn('coalesce("area_m2", 0) >=', text)
        self.assertIn("CASE", text)

    def test_lod_six_shows_every_building(self):
        self.assertIn(
            "WHEN coalesce(@level_of_detail, 6) >= 6 THEN 0",
            self.parent.get("filter") or "",
        )

    def test_area_thresholds(self):
        text = self.parent.get("filter") or ""
        for level, threshold in self.THRESHOLDS.items():
            self.assertIn(
                f"WHEN @level_of_detail >= {level} THEN {threshold}",
                text,
            )
        self.assertIn(f"ELSE {self.FALLBACK} END", text)

    def test_children_keep_their_filters(self):
        self.assertEqual(set(self.children), {"abandoned", "ruins", "else"})
        self.assertEqual(self.children["else"], "ELSE")
        self.assertNotIn("@level_of_detail", self.children["abandoned"])
        self.assertNotIn("@level_of_detail", self.children["ruins"])


class AdminBoundaryDrawingTest(unittest.TestCase):
    """Граница рисуется один раз, слой имён — только подписи."""

    NAMES = (
        "admin level 2 (country names)",
        "admin level 4 (region names)",
    )

    @classmethod
    def setUpClass(cls):
        cls.layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }

    def test_names_layers_do_not_draw_geometry(self):
        for name in self.NAMES:
            renderer = self.layers[name].find("renderer-v2")
            self.assertIsNotNone(renderer, name)
            self.assertEqual(renderer.get("type"), "nullSymbol", name)

    def test_border_lines_have_one_layer_each(self):
        expected = {
            "admin_level_2_dissolved.sqlite":
                "admin level 2 (country borders)",
            "admin_level_4_proc.sqlite": "admin level 4 (regions)",
        }
        for source, layer_name in expected.items():
            users = [
                name for name, layer in self.layers.items()
                if source in (layer.findtext("datasource") or "")
            ]
            self.assertEqual(users, [layer_name], source)

    def test_border_line_layers_do_not_label(self):
        for name in (
            "admin level 2 (country borders)",
            "admin level 4 (regions)",
        ):
            self.assertEqual(self.layers[name].get("labelsEnabled"), "0", name)


class RegionBoundaryLabelTest(unittest.TestCase):
    """Названия регионов ставятся вдоль кривых контура, а не поперёк него."""

    @classmethod
    def setUpClass(cls):
        layer = next(
            element for element in _template_root().iter("maplayer")
            if element.findtext("layername")
            == "admin level 4 (region names)"
        )
        cls.rules = layer.find("labeling/rules").findall("rule")

    def test_polygon_rules_use_curved_boundary_line(self):
        polygon_rules = [
            rule for rule in self.rules
            if rule.get("scalemindenom") is not None
            and "attribute('open_border') = 'yes'"
            not in (rule.get("filter") or "")
            and "attribute('maritime') = 'yes'"
            not in (rule.get("filter") or "")
        ]
        self.assertEqual(len(polygon_rules), 4)
        for rule in polygon_rules:
            placement = rule.find("settings/placement")
            self.assertIsNotNone(placement)
            self.assertEqual(placement.get("placement"), "3")
            self.assertEqual(placement.get("placementFlags"), "2")
            self.assertEqual(placement.get("fitInPolygonOnly"), "0")
            self.assertEqual(placement.get("geometryGeneratorEnabled"), "1")
            self.assertEqual(
                placement.get("geometryGenerator"), "boundary($geometry)"
            )
            self.assertEqual(
                placement.get("geometryGeneratorType"), "LineGeometry"
            )
            self.assertEqual(placement.get("layerType"), "LineGeometry")
            self.assertEqual(placement.get("priority"), "8")
            self.assertEqual(placement.get("maxCurvedCharAngleIn"), "60")
            self.assertEqual(placement.get("maxCurvedCharAngleOut"), "-60")


class FarmyardScaleFilterTest(unittest.TestCase):
    """Мелкие farmyard скрываются по площади, обводка — с 1:250000."""

    @classmethod
    def setUpClass(cls):
        layer = next(
            element for element in _template_root().iter("maplayer")
            if element.findtext("layername") == "farmyard"
        )
        renderer = layer.find("renderer-v2")
        cls.rules = renderer.find("rules").findall("rule")
        cls.symbols = renderer.find("symbols").findall("symbol")

    def _symbol_layers(self, rule):
        symbol = self.symbols[int(rule.get("symbol"))]
        return symbol.findall("layer")

    def test_scale_and_area_bands(self):
        expected = (
            ("1", "149999", "", 2),
            ("150000", "249999", "area_m2", 2),
            ("250000", None, "area_m2", 1),
        )
        self.assertEqual(len(self.rules), len(expected))
        for rule, (minimum, maximum, area, layers) in zip(
            self.rules, expected
        ):
            self.assertEqual(rule.get("scalemindenom"), minimum)
            self.assertEqual(rule.get("scalemaxdenom"), maximum)
            filter_text = rule.get("filter") or ""
            if area:
                self.assertIn("area_m2", filter_text)
            else:
                self.assertNotIn("area_m2", filter_text)
            self.assertEqual(len(self._symbol_layers(rule)), layers)

    def test_small_farmyards_start_hiding_at_150000(self):
        self.assertIn(">= 50000", self.rules[1].get("filter") or "")
        self.assertIn(">= 50000", self.rules[2].get("filter") or "")

    def test_outline_disappears_at_250000(self):
        full = self._symbol_layers(self.rules[1])
        fill_only = self._symbol_layers(self.rules[2])
        self.assertEqual(len(full), 2)
        self.assertEqual(len(fill_only), 1)
        outline = next(
            option for option in fill_only[0].findall("./Option/Option")
            if option.get("name") == "outline_color"
        )
        self.assertTrue(outline.get("value").startswith("0,0,0,0"))
        visible = next(
            option for option in full[1].findall("./Option/Option")
            if option.get("name") == "outline_color"
        )
        self.assertFalse(visible.get("value").startswith("0,0,0,0"))


class ResidentialScaleFillTest(unittest.TestCase):
    """Заливка residential светлеет на обзоре и исчезает с 1:1000000."""

    BANDS = (("1", "200000"), ("200000", "500000"), ("500000", "1000000"))
    COLORS = {
        "rural": ("176,176,176,255", "196,196,196,255", "216,216,216,255"),
        "urban": ("242,211,182,149", "245,222,200,149", "249,233,219,149"),
        "else": ("176,176,176,255", "196,196,196,255", "216,216,216,255"),
    }

    @classmethod
    def setUpClass(cls):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        cls.layer = layers["landuse residential"]
        cls.blocking = layers["[blocking] landuse residential"]
        renderer = cls.layer.find("renderer-v2")
        cls.parents = {
            rule.get("label"): rule
            for rule in renderer.find("rules").findall("rule")
        }
        cls.symbols = {
            symbol.get("name"): symbol
            for symbol in renderer.find("symbols").findall("symbol")
        }

    @staticmethod
    def _rgba(symbol):
        for layer in symbol.findall("layer"):
            if layer.get("class") != "SimpleFill":
                continue
            for option in layer.findall("./Option/Option"):
                if option.get("name") == "color":
                    return tuple(
                        int(part) for part in option.get("value").split(",")[:4]
                    )
        return None

    def test_parent_filters(self):
        self.assertEqual(
            {name: rule.get("filter") for name, rule in self.parents.items()},
            {
                "rural": "residential='rural'",
                "urban": "residential='urban'",
                "else": "ELSE",
            },
        )

    def test_three_scale_bands_per_type(self):
        for name in self.COLORS:
            leaves = self.parents[name].findall("rule")
            self.assertEqual(len(leaves), 3, name)
            for leaf, (minimum, maximum) in zip(leaves, self.BANDS):
                self.assertEqual(leaf.get("scalemindenom"), minimum, name)
                self.assertEqual(leaf.get("scalemaxdenom"), maximum, name)
                self.assertIn(leaf.get("symbol"), self.symbols, name)

    def test_fill_gets_lighter_with_scale(self):
        for name, colors in self.COLORS.items():
            leaves = self.parents[name].findall("rule")
            actual = tuple(
                self._rgba(self.symbols[leaf.get("symbol")]) for leaf in leaves
            )
            expected = tuple(
                tuple(int(part) for part in color.split(","))
                for color in colors
            )
            self.assertEqual(actual, expected, name)

    def test_no_fill_from_one_million(self):
        for name in self.COLORS:
            for leaf in self.parents[name].findall("rule"):
                self.assertLessEqual(
                    int(leaf.get("scalemaxdenom")), 1000000, name
                )
        self.assertEqual(self.layer.get("minScale"), "1000000")
        self.assertEqual(self.layer.get("maxScale"), "0")
        self.assertEqual(
            self.layer.get("hasScaleBasedVisibilityFlag"), "1"
        )
        self.assertEqual(self.blocking.get("minScale"), "1000000")
        self.assertEqual(self.blocking.get("maxScale"), "0")
        self.assertEqual(
            self.blocking.get("hasScaleBasedVisibilityFlag"), "1"
        )


class AreaFieldFilterTest(unittest.TestCase):
    """Фильтры по площади должны уходить в SQL, а не считаться в кадре."""

    def test_polygon_renderer_filters_use_area_field(self):
        layers = {
            layer.findtext("layername"): layer
            for layer in _template_root().iter("maplayer")
        }
        for name in AREA_LAYERS:
            layer = layers.get(name)
            self.assertIsNotNone(layer, name)
            renderer = layer.find("renderer-v2")
            self.assertIsNotNone(renderer, name)
            filters = [
                rule.get("filter") or "" for rule in renderer.iter("rule")
            ]
            self.assertTrue(
                any("area_m2" in value for value in filters),
                f"{name}: фильтр рендерера должен использовать area_m2")
            self.assertFalse(
                any("$area" in value for value in filters),
                f"{name}: $area в фильтре рендерера не уходит в SQL")


if __name__ == "__main__":
    unittest.main()
