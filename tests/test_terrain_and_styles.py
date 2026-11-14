import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

from qgis_xtopo.core.query_recipes import RECIPES, recipe_for
from qgis_xtopo.core.resources import template_path

HAS_QGIS = importlib.util.find_spec("qgis") is not None


class SlopeResolutionTest(unittest.TestCase):
    def test_slope_upsampled_dem(self):
        """Уклон считается по удвоенному DEM, а финальный растр — в 6 раз
        больше исходного (вдвое больше, чем раньше)."""
        from osgeo import gdal, osr
        import numpy as np
        from qgis_xtopo.core.terrain import generate_hillshade_slope

        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = type("Config", (), {})()
            config.raster_dir = root
            config.temp_dir = root / "tmp"
            config.temp_dir.mkdir()
            config.terrain_resample_method = "cubicspline"

            dem_path = root / "merged_dem.tif"
            driver = gdal.GetDriverByName("GTiff")
            ds = driver.Create(str(dem_path), 40, 40, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((0, 0.01, 0, 0, 0, -0.01))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(3857)
            ds.SetProjection(srs.ExportToWkt())
            band = ds.GetRasterBand(1)
            x = np.linspace(0, 100, 40, dtype=np.float32)
            band.WriteArray(np.tile(x, (40, 1)))
            ds = None

            generate_hillshade_slope(config, None)
            slope_up = gdal.Open(str(root / "slope_upscaled.tif"))
            self.assertEqual(slope_up.RasterXSize, 40 * 2 * 3)
            self.assertEqual(slope_up.RasterYSize, 40 * 2 * 3)
            slope_up = None
            self.assertFalse((root / "merged_dem_x2.tif").exists())


class RasterBlockTransformTest(unittest.TestCase):
    """Поблочная обработка даёт тот же растр, что и обработка целиком."""

    def test_transform_matches_full_array(self):
        from osgeo import gdal, osr
        import numpy as np
        from qgis_xtopo.core import terrain

        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.tif"
            destination_path = root / "destination.tif"
            driver = gdal.GetDriverByName("GTiff")
            source = driver.Create(
                str(source_path), 37, 29, 1, gdal.GDT_Float32
            )
            source.SetGeoTransform((10, 0.01, 0, 20, 0, -0.01))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            source.SetProjection(srs.ExportToWkt())
            values = np.arange(37 * 29, dtype="float32").reshape(29, 37)
            source.GetRasterBand(1).WriteArray(values)
            source = None

            original = terrain.RASTER_BLOCK_SIDE
            terrain.RASTER_BLOCK_SIDE = 11
            try:
                terrain._transform_raster(
                    source_path, destination_path,
                    lambda array: array * 2.0 + 1.0,
                )
            finally:
                terrain.RASTER_BLOCK_SIDE = original

            result = gdal.Open(str(destination_path))
            array = result.GetRasterBand(1).ReadAsArray()
            self.assertEqual(result.RasterXSize, 37)
            self.assertEqual(result.RasterYSize, 29)
            self.assertEqual(result.GetGeoTransform(), (10, 0.01, 0, 20, 0, -0.01))
            result = None
        np.testing.assert_array_equal(array, values * 2.0 + 1.0)


class HillshadeStyleTest(unittest.TestCase):
    """Отмывка как у gdaldem hillshade: одно направление света, тени не чёрные."""

    def test_deep_shadows_are_lifted(self):
        from osgeo import gdal, osr
        import numpy as np
        from qgis_xtopo.core.terrain import generate_hillshade_slope

        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = type("Config", (), {})()
            config.raster_dir = root
            config.temp_dir = root / "tmp"
            config.temp_dir.mkdir()
            config.terrain_resample_method = "cubicspline"

            dem_path = root / "merged_dem.tif"
            driver = gdal.GetDriverByName("GTiff")
            ds = driver.Create(str(dem_path), 40, 40, 1, gdal.GDT_Float32)
            ds.SetGeoTransform((0, 0.0003, 0, 0, 0, -0.0003))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            ds.SetProjection(srs.ExportToWkt())
            # Конус с уклоном около 55°: юго-восточный склон полностью в тени,
            # северо-западный — на свету.
            yy, xx = np.mgrid[0:40, 0:40]
            values = 1000.0 - 45.0 * np.hypot(xx - 20.0, yy - 20.0)
            ds.GetRasterBand(1).WriteArray(values.astype("float32"))
            ds = None

            generate_hillshade_slope(config, None)
            composite = gdal.Open(str(root / "hillshade_composite.tif"))
            array = composite.GetRasterBand(1).ReadAsArray()
            composite = None

            # 10 % подсветки дают нижнюю границу около 25,5; прежний вариант
            # с примешиванием уклона и многонаправленной отмывкой был светлее.
            self.assertLess(float(array.min()), 35.0)
            self.assertGreater(float(array.max()), 235.0)


class BarrierRecipeTest(unittest.TestCase):
    def test_barrier_polygons_become_lines(self):
        self.assertIn("barrier", RECIPES)
        operations = [operation for operation, _ in recipe_for("barrier").steps]
        self.assertIn("polygonstolines", operations)

    def test_barrier_way_datasource(self):
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("barrier_lines.sqlite", text)


class NatureReserveLabelingTest(unittest.TestCase):
    def test_labels_use_names_and_are_disabled_by_default(self):
        from xml.etree import ElementTree

        source = template_path()
        with zipfile.ZipFile(source) as archive:
            root = ElementTree.fromstring(archive.read("automap.qgs"))
        layer = next(
            item for item in root.iter("maplayer")
            if item.findtext("layername") == "nature_reserve"
        )
        self.assertEqual(layer.get("labelsEnabled"), "0")
        styles = list(layer.iter("text-style"))
        self.assertEqual(len(styles), 11)
        for style in styles:
            self.assertEqual(
                style.get("fieldName"),
                'coalesce("name", "name:en", "name:de")',
            )


class IsolineLabelingTest(unittest.TestCase):
    def test_contour_label_settings(self):
        source = template_path()
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn('maxCurvedCharAngleIn="60"', text)
        self.assertIn('maxCurvedCharAngleOut="-60"', text)
        self.assertIn('allowDegraded="1"', text)


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class IsolineColorTest(unittest.TestCase):
    """Перекраска изогипс из панели: линии, штрихи и подписи."""

    BASE = (43, 32, 24)

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _layer(self):
        from qgis.core import (
            QgsHashedLineSymbolLayer, QgsLineSymbol, QgsPalLayerSettings,
            QgsRuleBasedLabeling, QgsRuleBasedRenderer,
            QgsSimpleLineSymbolLayer, QgsVectorLayer,
        )
        from qgis.PyQt.QtGui import QColor

        def line_layer(color):
            return QgsSimpleLineSymbolLayer(QColor(*color))

        def symbol(color):
            layer = QgsLineSymbol()
            layer.changeSymbolLayer(0, line_layer(color))
            return layer

        symbol_ = symbol(self.BASE)
        hashes = QgsHashedLineSymbolLayer()
        hashes.setSubSymbol(symbol(self.BASE))
        symbol_.appendSymbolLayer(hashes)
        hidden = QgsSimpleLineSymbolLayer(QColor(35, 35, 35, 0))
        symbol_.appendSymbolLayer(hidden)

        rule = QgsRuleBasedRenderer.Rule(symbol_)
        rule.setLabel("10")
        root = QgsRuleBasedRenderer.Rule(None)
        root.appendChild(rule)

        format_ = QgsPalLayerSettings()
        text_format = format_.format()
        text_format.setColor(QColor(64, 64, 64))
        format_.setFormat(text_format)
        label_rule = QgsRuleBasedLabeling.Rule(format_)
        labeling = QgsRuleBasedLabeling(QgsRuleBasedLabeling.Rule(None))
        labeling.rootRule().appendChild(label_rule)

        layer = QgsVectorLayer("LineString?crs=EPSG:4326", "iso", "memory")
        layer.setRenderer(QgsRuleBasedRenderer(root))
        layer.setLabeling(labeling)
        return layer

    def test_apply_and_restore(self):
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import (
            _isoline_label_styles, _isoline_symbol_colors,
            apply_isoline_color, label_color_for, restore_isoline_style,
        )

        layer = self._layer()
        base = _isoline_symbol_colors(layer)
        label_base = _isoline_label_styles(layer)
        self.assertEqual(len(base), 4)
        self.assertEqual(label_base, [(QColor(64, 64, 64, 255), False)])

        color = QColor(0, 128, 96)
        self.assertTrue(apply_isoline_color(layer, color))
        colors = _isoline_symbol_colors(layer)
        self.assertEqual(colors[:3], [color, color, color])
        # Невидимая служебная линия остаётся невидимой.
        self.assertEqual(colors[3].alpha(), 0)
        labels = _isoline_label_styles(layer)
        self.assertEqual(labels[0][1], False)
        self.assertEqual(labels[0][0], label_color_for(color))
        self.assertEqual(labels[0][0].hue(), color.hue())
        self.assertLess(labels[0][0].value(), color.value())

        # Повторная перекраска тем же цветом ничего не меняет.
        self.assertFalse(apply_isoline_color(layer, color))

        self.assertTrue(restore_isoline_style(layer, base, label_base))
        self.assertEqual(_isoline_symbol_colors(layer), base)
        self.assertEqual(_isoline_label_styles(layer), label_base)
        self.assertFalse(restore_isoline_style(layer, base, label_base))

    def test_missing_labeling_is_tolerated(self):
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import (
            _isoline_label_styles, apply_isoline_color,
        )

        layer = self._layer()
        layer.setLabeling(None)
        self.assertEqual(_isoline_label_styles(layer), [])
        self.assertTrue(apply_isoline_color(layer, QColor(10, 20, 30)))

    def test_label_color_is_much_darker(self):
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import label_color_for

        for rgb in ((220, 120, 10), (60, 120, 200), (200, 200, 200)):
            color = label_color_for(QColor(*rgb))
            if QColor(*rgb).hue() >= 0:
                self.assertEqual(color.hue(), QColor(*rgb).hue())
            self.assertLess(color.value(), QColor(*rgb).value() * 0.6)


class IsolineWidthTest(unittest.TestCase):
    def test_template_multiplies_widths(self):
        """Шаблон умножает ширину линий изогипс на переменную проекта."""
        from xml.etree import ElementTree

        source = template_path()
        with zipfile.ZipFile(source) as archive:
            root = ElementTree.fromstring(archive.read("automap.qgs"))
        layers = {
            item.findtext("layername"): item
            for item in root.iter("maplayer")
            if item.findtext("layername", "").startswith(
                "[TERRAIN] isolines"
            )
        }
        self.assertEqual(
            sorted(layers),
            ["[TERRAIN] isolines glacier", "[TERRAIN] isolines regular"],
        )
        for layer in layers.values():
            expressions = [
                option.get("value") or ""
                for option in layer.iter("Option")
                if option.get("name") == "expression"
            ]
            symbols = layer.find("renderer-v2").find("symbols").findall("symbol")
            self.assertTrue(symbols)
            # Умножитель ширины должен быть у каждого символа изолиний.
            self.assertGreaterEqual(
                sum(
                    "isolines_width_mult" in text
                    for text in expressions
                ),
                len(symbols),
            )


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class IsolineWidthToggleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis.core import QgsProject

        self.project = QgsProject()

    def test_toggle_sets_and_removes_project_variable(self):
        from qgis_xtopo.gui.projectpanel import (
            isolines_width_doubled, set_isolines_width_doubled,
        )

        self.assertFalse(isolines_width_doubled(self.project))
        set_isolines_width_doubled(True, self.project)
        self.assertEqual(self.project.customVariables().get(
            "isolines_width_mult"
        ), "2")
        self.assertTrue(isolines_width_doubled(self.project))
        set_isolines_width_doubled(False, self.project)
        self.assertFalse(isolines_width_doubled(self.project))
        self.assertNotIn(
            "isolines_width_mult", self.project.customVariables()
        )

    def test_other_value_is_not_double_width(self):
        from qgis.core import QgsExpressionContextUtils
        from qgis_xtopo.gui.projectpanel import isolines_width_doubled
        QgsExpressionContextUtils.setProjectVariable(
            self.project, "isolines_width_mult", "1.5"
        )
        self.assertFalse(isolines_width_doubled(self.project))


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class IsolineResetTemplateTest(unittest.TestCase):
    """Сброс цвета возвращает цвета из automap.qgz, а не из проекта."""

    LAYER = "[TERRAIN] isolines regular"

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _layer(self):
        from qgis.core import QgsVectorLayer
        from qgis_xtopo.gui import projectpanel as pp

        renderer, labeling = pp._template_layer_objects(self.LAYER)
        layer = QgsVectorLayer("LineString?crs=EPSG:4326", self.LAYER, "memory")
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        return layer

    def _template_symbols(self, layer_name):
        """Число символов изолиний в шаблоне."""
        import zipfile
        from xml.etree import ElementTree

        source = template_path()
        with zipfile.ZipFile(source) as archive:
            root = ElementTree.fromstring(archive.read("automap.qgs"))
        for layer in root.iter("maplayer"):
            if layer.findtext("layername") == layer_name:
                return layer.find("renderer-v2").find("symbols").findall("symbol")
        return []

    def test_template_styles_are_cached(self):
        from qgis_xtopo.gui import projectpanel as pp

        styles = pp.template_isoline_styles(self.LAYER)
        self.assertIsNotNone(styles)
        # У каждого символа изолиний три цвета: линия, штрихи и их подсимвол.
        symbols = self._template_symbols(self.LAYER)
        self.assertTrue(symbols)
        self.assertEqual(len(styles[0]), 3 * len(symbols))
        self.assertTrue(styles[1])
        self.assertIs(styles, pp.template_isoline_styles(self.LAYER))
        glacier = pp.template_isoline_styles(pp.GLACIER_ISOLINES_LAYER)
        self.assertIsNotNone(glacier)
        self.assertEqual(len(glacier[0]), 3 * len(self._template_symbols(pp.GLACIER_ISOLINES_LAYER)))
        # Подписи изолиний и изолиний ледников оформлены одинаково.
        self.assertEqual(len(glacier[1]), len(styles[1]))

    def test_reset_uses_template_after_reopen(self):
        """Проект сохранён с другим цветом: сброс идёт к цветам шаблона."""
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel as pp

        layer = self._layer()
        self.assertFalse(pp.isoline_styles_differ(layer))

        custom = QColor(190, 30, 120)
        pp.apply_isoline_color(layer, custom)
        self.assertTrue(pp.isoline_styles_differ(layer))

        # Как после переоткрытия: сохранённые цвета уже не равны шаблону.
        captured = (
            pp._isoline_symbol_colors(layer),
            pp._isoline_label_styles(layer),
        )
        colors, labels = pp.isoline_reset_styles(layer, captured)
        template = pp.template_isoline_styles(self.LAYER)
        self.assertEqual(colors, template[0])
        self.assertEqual(labels, template[1])

        self.assertTrue(pp.restore_isoline_style(layer, colors, labels))
        self.assertFalse(pp.isoline_styles_differ(layer))
        self.assertEqual(pp._isoline_symbol_colors(layer), template[0])

    def test_reset_falls_back_when_template_does_not_fit(self):
        from qgis.core import QgsVectorLayer
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel as pp

        layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326", self.LAYER, "memory"
        )
        captured = ([QColor(1, 2, 3)], [])
        self.assertEqual(pp.isoline_reset_styles(layer, captured), captured)


class SlopeVariantChoiceTest(unittest.TestCase):
    """Вариант раскраски уклонов выбирается по крутизне рельефа."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _raster(self, values, nodata=None):
        from osgeo import gdal
        import numpy as np

        path = Path(self.directory.name) / "slope_upscaled.tif"
        rows, cols = values.shape
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), cols, rows, 1, gdal.GDT_Float32,
        )
        dataset.SetGeoTransform((0, 0.001, 0, 0, 0, -0.001))
        band = dataset.GetRasterBand(1)
        band.WriteArray(np.asarray(values, dtype="float32"))
        if nodata is not None:
            band.SetNoDataValue(nodata)
        dataset = None
        return path

    def test_terrain_types(self):
        import numpy as np
        from qgis_xtopo.core import slope_style

        mountains = self._raster(np.full((50, 50), 45.0))
        self.assertEqual(slope_style.choose_variant(mountains),
                         slope_style.MOUNTAINS)

        foothills = self._raster(np.full((50, 50), 20.0))
        self.assertEqual(slope_style.choose_variant(foothills),
                         slope_style.FOOTHILLS)

        plains = self._raster(np.full((50, 50), 3.0))
        self.assertEqual(slope_style.choose_variant(plains),
                         slope_style.PLAINS)

    def test_foothill_bounds(self):
        import numpy as np
        from qgis_xtopo.core import slope_style

        # Нижняя граница предгорий и верхняя, после которой начинаются горы.
        lower = self._raster(np.full((50, 50), slope_style.FOOTHILL_P90))
        self.assertEqual(slope_style.choose_variant(lower),
                         slope_style.FOOTHILLS)
        upper = self._raster(
            np.full((50, 50), slope_style.MOUNTAIN_P90 - 0.1)
        )
        self.assertEqual(slope_style.choose_variant(upper),
                         slope_style.FOOTHILLS)
        below = self._raster(
            np.full((50, 50), slope_style.FOOTHILL_P90 - 0.1)
        )
        self.assertEqual(slope_style.choose_variant(below),
                         slope_style.PLAINS)
        above = self._raster(np.full((50, 50), slope_style.MOUNTAIN_P90))
        self.assertEqual(slope_style.choose_variant(above),
                         slope_style.MOUNTAINS)

    def test_nodata_is_ignored(self):
        import numpy as np
        from qgis_xtopo.core import slope_style

        values = np.full((50, 50), -9999.0)
        values[:25, :] = 45.0
        path = self._raster(values, nodata=-9999.0)
        self.assertEqual(slope_style.choose_variant(path),
                         slope_style.MOUNTAINS)

    def test_missing_raster_keeps_manual_choice(self):
        from qgis_xtopo.core import slope_style

        self.assertIsNone(slope_style.choose_variant(None))
        self.assertIsNone(
            slope_style.choose_variant(Path(self.directory.name) / "none.tif")
        )

    def test_project_raster_path(self):
        from qgis_xtopo.core import slope_style

        root = Path(self.directory.name)
        (root / "raster").mkdir()
        raster = root / "raster" / "slope_upscaled.tif"
        raster.touch()
        self.assertEqual(
            slope_style.project_raster(str(root / "map.qgz")), raster,
        )
        self.assertIsNone(slope_style.project_raster(""))


if __name__ == "__main__":
    unittest.main()
