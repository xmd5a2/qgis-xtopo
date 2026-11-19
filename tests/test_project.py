import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from qgis_xtopo.core.project import project_file_path, rewrite_project_archive
from qgis_xtopo.core.resources import template_path

VARIABLE_VALUE = re.compile(
    r"<value\b[^>]*?/>|<value\b[^>]*?>(.*?)</value>", re.S
)


def project_variables(text):
    """Переменные проекта: значения, включая пустые `<value />`."""
    start = text.rfind("<Variables>")
    block = text[start:text.find("</Variables>", start)]
    names, values = block.split("</variableNames>")
    return dict(zip(
        [match.group(1) for match in VARIABLE_VALUE.finditer(names)],
        [match.group(1) or "" for match in VARIABLE_VALUE.finditer(values)],
    ))


class ProjectTemplateTest(unittest.TestCase):
    def test_rewrite(self):
        source = template_path()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(source, destination, "demo")
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
                self.assertIn("demo.qgs", names)
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn('projectname="demo"', text)
        self.assertIn("<title>demo</title>", text)
        self.assertIn("<qgis_xtopo", text)

    def test_rewrite_in_place_updates_attribution(self):
        """Атрибуция мозаики дописывается уже в готовый проект."""
        source = template_path()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo.qgz"
            rewrite_project_archive(source, target, "demo")
            rewrite_project_archive(
                target, target, "demo", dem_attribution="Copernicus · Sonny"
            )
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertEqual(names.count("demo.qgs"), 1)
        self.assertIn("Copernicus · Sonny", text)

    def test_template_has_aerialway_station(self):
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("aerialway station icons", text)
        self.assertIn(
            "aerialway_station_buffered_dissolved_centroids.sqlite",
            text,
        )
        self.assertIn("../icons/aerialway_station.svg", text)
        self.assertIn('labelsEnabled="1"', text)
        # станции видны только до 1:100000 (minScale — предел отдаления)
        block = re.search(
            r"<maplayer[^>]*>(?:(?!</maplayer>).)*"
            r"aerialway station icons.*?</maplayer>",
            text, re.S,
        )
        self.assertIsNotNone(block)
        self.assertIn('minScale="100000"', block.group(0))
        self.assertIn('hasScaleBasedVisibilityFlag="1"', block.group(0))
        self.assertNotIn("100000 - 300000", block.group(0))
        # стрелки направления с учётом oneway, между точками пунктира
        self.assertGreaterEqual(text.count("filled_arrowhead"), 16)
        self.assertIn("offset_along_line", text)
        self.assertIn("&quot;oneway&quot;", text)

    def test_default_queries_include_aerialway_station(self):
        from qgis_xtopo.core.config import default_queries

        self.assertIn("aerialway_station", default_queries())
        self.assertLess(
            default_queries().index("aerialway_station"),
            default_queries().index("aerialway_cable_car_gondola"),
        )

    def test_template_overlay_filters_guarded_with_try(self):
        """overlay_* не должны падать, когда слой удалён из проекта.

        Пустые слои (spoil heap, prison, cliff, earth bank gully)
        вырезаются при сборке проекта, поэтому обращения к ним в
        фильтрах обёрнуты в try(..., fallback) — иначе весь фильтр
        даёт NULL и объекты пропадают с карты.
        """
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        unguarded = _unguarded_overlay_calls(text)
        if unguarded:
            self.fail(
                "незащищённый вызов overlay в шаблоне: " + unguarded[0]
            )


def _unguarded_overlay_calls(text):
    """Вызовы overlay_*, не обёрнутые в try(...).

    Разбор идёт одним проходом по тексту: раньше для каждого вызова
    скобки искались назад с начала файла, и на шаблоне в 6 МБ это
    занимало больше двух минут.
    """
    pairs = []
    stack = []
    for match in re.finditer(r"[()]", text):
        position = match.start()
        if match.group(0) == "(":
            # Имя функции ищем в небольшом окне: срез всего текста на
            # каждой скобке снова даёт квадратичное время.
            window = text[max(0, position - 64):position]
            name = re.search(r"([\w.]+)\s*$", window)
            stack.append((position, name.group(1) if name else ""))
        else:
            if stack:
                open_position, name = stack.pop()
                pairs.append((open_position, position, name))
    guarded = [
        (open_position, close_position)
        for open_position, close_position, name in pairs
        if name == "try"
    ]
    result = []
    for match in re.finditer(r"overlay_\w+\('[^']+'\)", text):
        position = match.start()
        if not any(
            open_position < position < close_position
            for open_position, close_position in guarded
        ):
            result.append(match.group(0))
    return result


class OverlayFilterPatchTest(unittest.TestCase):
    """Обращения к overlay_* обёрнуты в try(...)."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_filters_are_guarded(self):
        from qgis_xtopo.core.project import patch_overlay_filters

        text = (
            "filter=\"if(overlay_within('spoil heap'),false,true)\" "
            "filter=\"if(overlay_within('prison'),false,true)\" "
            "value=\"if(overlay_contains('earth bank gully') OR "
            "overlay_contains('cliff'),0,1)\""
        )
        patched = patch_overlay_filters(text)
        self.assertNotIn("if(overlay_within", patched)
        self.assertNotIn("if(overlay_contains", patched)
        self.assertEqual(patched.count("try(overlay"), 3)

    def test_patch_is_idempotent(self):
        from qgis_xtopo.core.project import patch_overlay_filters

        text = "filter=\"if(overlay_within('prison'),false,true)\""
        once = patch_overlay_filters(text)
        self.assertEqual(patch_overlay_filters(once), once)

    def test_repair_project_guards_filters(self):
        from qgis_xtopo.core.project import repair_project

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qgz"
            (Path(directory) / "vector").mkdir()
            (Path(directory) / "vector" / "roads.dat").write_text(
                "data", encoding="utf-8"
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "demo.qgs",
                    "<qgis><maplayer><id>x</id>"
                    "<datasource>./vector/roads.dat</datasource>"
                    "<renderer-v2><rule filter=\""
                    "if(overlay_within('prison'),false,true)\"/></renderer-v2>"
                    "</maplayer></qgis>",
                )
            self.assertTrue(repair_project(path))
            with zipfile.ZipFile(path) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn("try(overlay_within('prison'), false)", text)
        self.assertNotIn("if(overlay_within", text)

    def test_repair_project_limits_label_candidates(self):
        from qgis_xtopo.core.project import repair_project

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qgz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "demo.qgs",
                    "<qgis><PAL>"
                    '<CandidatesLinePerCM type="double">5'
                    "</CandidatesLinePerCM></PAL></qgis>",
                )
            self.assertTrue(repair_project(path))
            with zipfile.ZipFile(path) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
            self.assertIn(
                '<CandidatesLinePerCM type="double">0.5'
                "</CandidatesLinePerCM>",
                text,
            )
            self.assertIn("<qgis>", text)
            self.assertIn("</PAL>", text)
            self.assertFalse(repair_project(path))


class RasterResamplingTest(unittest.TestCase):
    """Растры рельефа передискретизируются бикубом, и по возможности
    силами самого GDAL."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    RASTER = (
        '<maplayer type="raster">'
        "<provider>gdal</provider>"
        "<pipe>"
        '<provider><resampling maxOversampling="2" enabled="false" '
        'zoomedOutResamplingMethod="nearestNeighbour" '
        'zoomedInResamplingMethod="nearestNeighbour" /></provider>'
        '<rasterresampler maxOversampling="2" />'
        "<resamplingStage>resamplingFilter</resamplingStage>"
        "</pipe>"
        "</maplayer>"
    )
    TILES = (
        '<maplayer type="raster">'
        "<provider>wms</provider>"
        "<pipe>"
        '<provider><resampling maxOversampling="2" enabled="false" '
        'zoomedInResamplingMethod="nearestNeighbour" /></provider>'
        "<resamplingStage>resamplingFilter</resamplingStage>"
        "</pipe>"
        "</maplayer>"
    )

    def test_terrain_raster_uses_cubic_and_early_resampling(self):
        from qgis_xtopo.core.project import patch_raster_resampling

        patched = patch_raster_resampling(self.RASTER)
        self.assertIn('zoomedInResamplingMethod="cubic"', patched)
        self.assertIn('enabled="true"', patched)
        self.assertIn('zoomedInResampler="cubic"', patched)
        self.assertIn("<resamplingStage>provider</resamplingStage>", patched)

    def test_tile_layer_is_untouched(self):
        from qgis_xtopo.core.project import patch_raster_resampling

        self.assertEqual(patch_raster_resampling(self.TILES), self.TILES)

    def test_patch_is_idempotent(self):
        from qgis_xtopo.core.project import patch_raster_resampling

        once = patch_raster_resampling(self.RASTER)
        self.assertEqual(patch_raster_resampling(once), once)

    def test_template_terrain_rasters_are_patched(self):
        from qgis_xtopo.core.project import patch_raster_resampling

        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        patched = patch_raster_resampling(text)
        self.assertNotEqual(patched, text)
        self.assertIn("<resamplingStage>provider</resamplingStage>", patched)
        # Подложка из плиток остаётся как была.
        tiles = patched.split('<layername>OpenStreetMap</layername>')[0]
        self.assertNotIn("zoomedInResampler", tiles)


class ProjectDetailLevelTest(unittest.TestCase):
    """Уровень детализации из панели попадает в переменные проекта."""

    def _variables(self, text):
        return project_variables(text)

    def _text(self, level):
        from qgis_xtopo.core.project import _set_detail_variable

        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        return text, self._variables(_set_detail_variable(text, level))

    def test_value_is_written_to_its_own_position(self):
        text, variables = self._text(5)
        self.assertEqual(variables["level_of_detail"], "5")

    def test_other_variables_are_untouched(self):
        with zipfile.ZipFile(template_path()) as archive:
            source = archive.read("automap.qgs").decode("utf-8")
        original = self._variables(source)
        _text, variables = self._text(3)
        for name, value in original.items():
            if name != "level_of_detail":
                self.assertEqual(variables[name], value, name)

    def test_missing_variable_keeps_text(self):
        from qgis_xtopo.core.project import _set_detail_variable

        text = "<Variables><variableNames type=\"QStringList\">" \
               "<value>other</value></variableNames>" \
               "<variableValues type=\"QStringList\">" \
               "<value>1</value></variableValues></Variables>"
        self.assertEqual(_set_detail_variable(text, 5), text)

    def test_rewrite_uses_selected_level(self):
        from qgis_xtopo.core import map_language

        previous = map_language.level_of_detail()
        try:
            map_language.set_level_of_detail(6, save=False)
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "demo.qgz"
                rewrite_project_archive(template_path(), destination, "demo")
                with zipfile.ZipFile(destination) as archive:
                    text = archive.read("demo.qgs").decode("utf-8")
        finally:
            map_language.set_level_of_detail(previous, save=False)
        self.assertEqual(self._variables(text)["level_of_detail"], "6")


class ProjectTextSizeTest(unittest.TestCase):
    """Переменная text_size_mult масштабирует все подписи слоёв."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _template_text(self):
        with zipfile.ZipFile(template_path()) as archive:
            return archive.read("automap.qgs").decode("utf-8")

    def test_percent_maps_to_multiplier(self):
        from qgis_xtopo.core import text_size

        expected = {
            50: "0.5", 75: "0.75", 100: "1", 125: "1.25",
            150: "1.5", 200: "2",
        }
        for percent, value in expected.items():
            self.assertEqual(text_size.multiplier(percent), value)

    def test_percent_is_normalized_to_available_choices(self):
        from qgis_xtopo.core import text_size

        self.assertEqual(text_size.normalize(74), 75)
        self.assertEqual(text_size.normalize(120), 125)
        self.assertEqual(text_size.normalize(-10), 50)
        self.assertEqual(text_size.normalize(500), 200)
        self.assertEqual(text_size.normalize("bad"), 100)

    def test_template_scales_every_drawn_label(self):
        root = ET.fromstring(self._template_text())
        missing = []
        for layer in root.iter("maplayer"):
            labeling = layer.find("labeling")
            if labeling is None:
                continue
            name = layer.findtext("layername") or layer.findtext("id")
            for settings in labeling.iter("settings"):
                rendering = settings.find("rendering")
                if (rendering is not None
                        and rendering.get("drawLabels") == "0"):
                    continue
                dd = settings.find("dd_properties")
                properties = (
                    dd.find("Option/Option[@name='properties']")
                    if dd is not None else None
                )
                size = (
                    properties.find("Option[@name='Size']")
                    if properties is not None else None
                )
                active = (
                    size.find("Option[@name='active']")
                    if size is not None else None
                )
                expression = (
                    size.find("Option[@name='expression']")
                    if size is not None else None
                )
                value = ""
                if expression is not None:
                    value = expression.get("value") or ""
                if (active is None or active.get("value") != "true"
                        or value.count("@text_size_mult") != 1):
                    missing.append(name)
        self.assertEqual(missing, [])

    def test_template_has_no_undeclared_multiplier(self):
        self.assertNotIn(
            "@text_size_mult_low_mapscale", self._template_text()
        )

    def test_template_size_expressions_parse(self):
        from qgis.core import QgsExpression
        from qgis_xtopo.core.expressions import load_expressions

        load_expressions()
        root = ET.fromstring(self._template_text())
        errors = []
        for option in root.iter("Option"):
            if option.get("name") != "Size":
                continue
            expression = option.find("Option[@name='expression']")
            if expression is None:
                continue
            value = expression.get("value") or ""
            if "@text_size_mult" not in value:
                continue
            parsed = QgsExpression(value)
            if parsed.hasParserError():
                errors.append((value, parsed.parserErrorString()))
        self.assertEqual(errors, [])

    def test_template_expression_doubles_label_size(self):
        from qgis.core import (
            QgsExpression,
            QgsExpressionContext,
            QgsExpressionContextUtils,
            QgsPalLayerSettings,
            QgsProject,
        )
        from qgis_xtopo.core import text_size
        from qgis_xtopo.core.expressions import load_expressions

        load_expressions()
        project = QgsProject()
        project.read(
            str(template_path()), QgsProject.FlagDontResolveLayers
        )
        try:
            layer = project.mapLayersByName("cliff")[0]
            settings = layer.labeling().settings()
            prop = settings.dataDefinedProperties().property(
                QgsPalLayerSettings.Size
            )
            base = settings.format().font().pointSizeF()
            for value, expected in (("1", base), ("2", base * 2)):
                QgsExpressionContextUtils.setProjectVariable(
                    project, text_size.VARIABLE, value
                )
                context = QgsExpressionContext()
                context.appendScope(
                    QgsExpressionContextUtils.projectScope(project)
                )
                context.appendScope(
                    QgsExpressionContextUtils.layerScope(layer)
                )
                context.setOriginalValueVariable(base)
                expression = QgsExpression(prop.expressionString())
                self.assertAlmostEqual(
                    expression.evaluate(context), expected
                )
        finally:
            project.clear()

    def test_rewrite_writes_selected_size(self):
        from qgis_xtopo.core import text_size

        previous = text_size.percent()
        try:
            text_size.set_percent(150, save=False)
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "demo.qgz"
                rewrite_project_archive(
                    template_path(), destination, "demo"
                )
                with zipfile.ZipFile(destination) as archive:
                    text = archive.read("demo.qgs").decode("utf-8")
        finally:
            text_size.set_percent(previous, save=False)
        self.assertEqual(project_variables(text)["text_size_mult"], "1.5")

    def test_rewrite_keeps_variable_positions(self):
        """Пустое значение dem_attribution не сдвигает остальные."""
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(
                template_path(), destination, "demo",
                dem_attribution="ATTRIBUTION",
            )
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        variables = project_variables(text)
        self.assertEqual(variables["dem_attribution"], "ATTRIBUTION")
        self.assertEqual(variables["disable_surface_integrity"], "false")
        self.assertEqual(variables["mountain_pass_text_buffer_width"], "0.5")

    def test_project_variable_is_written(self):
        from qgis.core import QgsExpressionContextUtils, QgsProject
        from qgis_xtopo.core import text_size

        project = QgsProject()
        try:
            text_size.set_project_text_size(125, project)
            self.assertEqual(
                project.customVariables()[text_size.VARIABLE], "1.25"
            )
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, text_size.VARIABLE
            )


class ProjectBasemapTest(unittest.TestCase):
    """Подложка проекта: нижний слой и площадные слои без полигонов."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis.core import (
            QgsFeature,
            QgsFillSymbol,
            QgsGeometry,
            QgsLineSymbol,
            QgsPalLayerSettings,
            QgsPointXY,
            QgsProject,
            QgsSingleSymbolRenderer,
            QgsVectorLayer,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis_xtopo.core import basemap

        self.basemap = basemap
        self.project = QgsProject()
        self.polygon = QgsVectorLayer(
            "Polygon?crs=EPSG:3857&field=name:string", "wood", "memory"
        )
        feature = QgsFeature(self.polygon.fields())
        feature["name"] = "Лес"
        feature.setGeometry(QgsGeometry.fromPolygonXY([[
            QgsPointXY(0, 0), QgsPointXY(10, 0), QgsPointXY(10, 10),
            QgsPointXY(0, 10),
        ]]))
        self.polygon.dataProvider().addFeature(feature)
        self.polygon.updateExtents()
        self.polygon.setRenderer(QgsSingleSymbolRenderer(
            QgsFillSymbol.createSimple({"color": "0,150,0"})
        ))
        labeling = QgsPalLayerSettings()
        labeling.fieldName = "name"
        self.polygon.setLabeling(QgsVectorLayerSimpleLabeling(labeling))
        self.polygon.setLabelsEnabled(True)
        self.project.addMapLayer(self.polygon)

        self.line = QgsVectorLayer(
            "LineString?crs=EPSG:3857", "path", "memory"
        )
        self.line.setRenderer(QgsSingleSymbolRenderer(
            QgsLineSymbol.createSimple({"color": "120,80,20"})
        ))
        self.project.addMapLayer(self.line)

        self.osm = QgsVectorLayer(
            "Polygon?crs=EPSG:3857", "OpenStreetMap", "memory"
        )
        self.project.addMapLayer(self.osm)
        self.bbox = QgsVectorLayer(
            "Polygon?crs=EPSG:3857", "BBOX", "memory"
        )
        self.bbox.setRenderer(QgsSingleSymbolRenderer(
            QgsFillSymbol.createSimple({"color": "255,0,0"})
        ))
        self.project.addMapLayer(self.bbox)

    def tearDown(self):
        self.project.clear()

    def _visible(self, layer):
        node = self.project.layerTreeRoot().findLayer(layer.id())
        return bool(node is not None and node.itemVisibilityChecked())

    def test_sources_are_satellite_xyz(self):
        self.assertEqual(
            [item.key for item in self.basemap.SOURCES],
            ["google", "bing", "arcgis"],
        )
        for item in self.basemap.SOURCES:
            self.assertIn("type=xyz", item.uri)
            self.assertIn("zmax=", item.uri)
        self.assertIn("{q}", self.basemap.source("bing").uri)

    def test_apply_adds_layer_at_bottom_and_hides_polygons(self):
        before_line = self.line.renderer().type()
        before_bbox = self.bbox.renderer().type()
        layer = self.basemap.apply("google", self.project)
        self.assertIsNotNone(layer)
        self.assertEqual(
            self.project.layerTreeRoot().children()[-1].layer().id(),
            layer.id(),
        )
        self.assertEqual(
            self.basemap.active_source(self.project).key, "google"
        )
        self.assertEqual(
            self.polygon.styleManager().currentStyle(),
            self.basemap.LABELS_STYLE,
        )
        self.assertEqual(self.polygon.renderer().type(), "nullSymbol")
        self.assertTrue(self.polygon.labelsEnabled())
        self.assertEqual(self.line.renderer().type(), before_line)
        self.assertEqual(self.bbox.renderer().type(), before_bbox)
        self.assertFalse(self._visible(self.osm))
        self.assertEqual(
            layer.customProperty(self.basemap.SOURCE_PROPERTY), "google"
        )
        self.assertIn("mt1.google.com", layer.source())

    def test_switch_source_keeps_single_basemap(self):
        self.basemap.apply("google", self.project)
        layer = self.basemap.apply("bing", self.project)
        self.assertEqual(
            len(self.basemap.active_layers(self.project)), 1
        )
        self.assertEqual(
            self.basemap.active_source(self.project).key, "bing"
        )
        self.assertIn("virtualearth", layer.source())
        self.assertEqual(
            self.project.layerTreeRoot().children()[-1].layer().id(),
            layer.id(),
        )

    def test_remove_restores_styles_and_osm(self):
        self.basemap.apply("google", self.project)
        self.assertTrue(self.basemap.remove(self.project))
        self.assertEqual(self.basemap.active_layers(self.project), [])
        self.assertEqual(self.polygon.renderer().type(), "singleSymbol")
        self.assertTrue(self.polygon.labelsEnabled())
        self.assertTrue(self._visible(self.osm))
        self.assertEqual(self.bbox.renderer().type(), "singleSymbol")

    def test_failed_switch_restores_state(self):
        from unittest import mock

        self.basemap.apply("google", self.project)
        with mock.patch.object(
            self.basemap, "_create_layer", return_value=None
        ):
            self.assertIsNone(self.basemap.apply("bing", self.project))
        self.assertEqual(self.basemap.active_layers(self.project), [])
        self.assertEqual(self.polygon.renderer().type(), "singleSymbol")
        self.assertTrue(self._visible(self.osm))

    def test_styles_survive_project_save(self):
        from qgis.core import QgsProject

        self.basemap.apply("google", self.project)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qgz"
            self.assertTrue(self.project.write(str(path)))
            saved = QgsProject()
            self.assertTrue(saved.read(str(path)))
            try:
                layer = saved.mapLayersByName("wood")[0]
                self.assertEqual(
                    layer.styleManager().currentStyle(),
                    self.basemap.LABELS_STYLE,
                )
                self.assertEqual(layer.renderer().type(), "nullSymbol")
                self.assertEqual(
                    self.basemap.active_source(saved).key, "google"
                )
                self.assertTrue(self.basemap.remove(saved))
                self.assertEqual(layer.renderer().type(), "singleSymbol")
            finally:
                saved.clear()


class ProjectAttributionTest(unittest.TestCase):
    """Атрибуция источника рельефа попадает в подпись макета атласа."""

    def _variables(self, text):
        return project_variables(text)

    def test_template_has_attribution_expression(self):
        with zipfile.ZipFile(template_path()) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("@dem_attribution", text)
        self.assertIn("dem_attribution", self._variables(text))

    def test_rewrite_writes_attribution(self):
        attribution = (
            "Produced using Copernicus data and information funded by "
            "the European Union and ESA"
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(
                template_path(), destination, "demo",
                dem_attribution=attribution,
            )
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertEqual(self._variables(text)["dem_attribution"], attribution)

    def test_rewrite_without_attribution_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(template_path(), destination, "demo")
            with zipfile.ZipFile(destination) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertEqual(self._variables(text)["dem_attribution"], "")

    def test_attribution_survives_project_reopen(self):
        """Новый проект сохраняет атрибуцию после сохранения и открытия."""
        from qgis.core import QgsProject
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.project import prepare_project
        from qgis_xtopo.core.terrain import _write_dem_attribution

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="7,46,7.1,46.1",
                array_queries=[],
                generate_terrain=True,
                terrain_source="cop30",
                download_terrain_tiles=True,
            )
            path = prepare_project(config)
            _write_dem_attribution(config, ["cop30"])
            project = QgsProject()
            try:
                self.assertTrue(project.read(str(path)))
                expected = project.customVariables().get("dem_attribution")
                self.assertIn("Copernicus", str(expected))
                self.assertTrue(project.write(str(path)))
            finally:
                project.clear()
            reopened = QgsProject()
            try:
                self.assertTrue(reopened.read(str(path)))
                self.assertEqual(
                    reopened.customVariables().get("dem_attribution"),
                    expected,
                )
            finally:
                reopened.clear()

    def test_prepare_project_uses_source_attribution(self):
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.project import prepare_project

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="7,46,7.1,46.1",
                array_queries=[],
                generate_terrain=True,
                terrain_source="cop30",
                download_terrain_tiles=True,
            )
            path = prepare_project(config)
            with zipfile.ZipFile(path) as archive:
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn(
            "Produced using Copernicus data",
            self._variables(text)["dem_attribution"],
        )


class ProjectFilePathTest(unittest.TestCase):
    def test_path(self):
        path = project_file_path("/tmp/projects", "demo")
        self.assertEqual(
            path, Path("/tmp/projects/demo/demo.qgz"),
        )

    def test_invalid_name(self):
        self.assertIsNone(project_file_path("/tmp/projects", "bad name!"))


if __name__ == "__main__":
    unittest.main()
