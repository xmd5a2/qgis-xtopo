import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

from qgis_xtopo.core.query_recipes import RECIPES, recipe_for
from qgis_xtopo.core.resources import template_path

HAS_QGIS = importlib.util.find_spec("qgis") is not None


class SlopeResolutionTest(unittest.TestCase):
    def test_relief_rasters_keep_dem_size(self):
        """Уклон и оттенок считаются в разрешении исходного DEM.

        Раньше DEM укрупнялся вдвое, чтобы сгладить отмывку, но QGIS
        передискретизирует растры бикубом сам: запас пикселей только
        раздувал файлы и подготовку.
        """
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
            srs.ImportFromEPSG(4326)
            ds.SetProjection(srs.ExportToWkt())
            band = ds.GetRasterBand(1)
            x = np.linspace(0, 100, 40, dtype="float32")
            band.WriteArray(np.tile(x, (40, 1)))
            ds = None

            generate_hillshade_slope(config, None)
            for name in (
                "slope.tif",
                "hillshade_composite.tif",
            ):
                raster = gdal.Open(str(root / name))
                self.assertEqual(raster.RasterXSize, 40, name)
                self.assertEqual(raster.RasterYSize, 40, name)
                if name == "slope.tif":
                    band = raster.GetRasterBand(1)
                    self.assertEqual(band.DataType, gdal.GDT_Byte)
                    self.assertEqual(band.GetNoDataValue(), 255)
                raster = None
            self.assertFalse((root / "merged_dem_upscaled.tif").exists())
            self.assertFalse((root / "merged_dem_x2.tif").exists())
            # Промежуточных файлов больше нет: отсечение уклона пишется
            # сразу в итоговый растр.
            self.assertFalse((root / "slope_cut.tif").exists())
            self.assertFalse((root / "merged_dem_downscaled.tif").exists())

    def test_relief_rasters_are_written_single_threaded(self):
        """Растры рельефа пишутся без многопоточного сжатия.

        В GDAL 3.8 `NUM_THREADS` приводит к тому, что полосы данных
        крупного файла ложатся не по своим смещениям: файл раздувается
        в разы и читается с ошибкой.
        """
        from qgis_xtopo.core.terrain import _relief_creation_options

        options = _relief_creation_options()
        self.assertFalse(
            [option for option in options if option.startswith("NUM_THREADS")]
        )
        self.assertIn("BIGTIFF=YES", options)


class WaterCutIsolinesTest(unittest.TestCase):
    """Вода вычитается из контуров векторно: концы лежат на берегу."""

    def test_isolines_touch_water_without_bending(self):
        from osgeo import gdal, ogr, osr
        import numpy as np

        from qgis_xtopo.core import terrain
        from qgis_xtopo.core.config import Config

        gdal.UseExceptions()
        ogr.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="water",
                generate_terrain=True,
                generate_terrain_isolines=True,
                isolines_step=10,
                smooth_isolines=False,
                terrain_resample_method="bilinear",
            )
            config.ensure_directories()

            # Ровный склон: высота зависит только от x, поэтому все
            # изолинии — строго вертикальные линии.
            dem_path = config.raster_dir / "merged_dem.tif"
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(dem_path), 60, 60, 1, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((0, 30, 0, 1800, 0, -30))
            spatial_ref = osr.SpatialReference()
            spatial_ref.ImportFromEPSG(3857)
            dataset.SetProjection(spatial_ref.ExportToWkt())
            ys, xs = np.mgrid[0:60, 0:60]
            dataset.GetRasterBand(1).WriteArray(
                ((xs * 30 + 15) * 0.5).astype("float32")
            )
            dataset.GetRasterBand(1).SetNoDataValue(-32768)
            dataset = None

            water_path = config.vector_dir / "water_dissolved.sqlite"
            water_ds = ogr.GetDriverByName("SQLite").CreateDataSource(
                str(water_path)
            )
            water_layer = water_ds.CreateLayer(
                "water_dissolved", spatial_ref, ogr.wkbPolygon
            )
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in (
                (710, 600), (1090, 600), (1090, 1200), (710, 1200),
                (710, 600),
            ):
                ring.AddPoint(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature = ogr.Feature(water_layer.GetLayerDefn())
            feature.SetGeometry(polygon)
            water_layer.CreateFeature(feature)
            water_ds = None

            terrain.generate_isolines(config, None)

            result = ogr.Open(
                str(config.vector_dir / "isolines_regular.sqlite")
            )
            layer = result.GetLayer(0)
            endpoints_on_water = 0
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is None:
                    continue
                expected_x = feature.GetField("ELEV") / 0.5
                for x, y in geometry.GetPoints():
                    # Линия не должна изгибаться вокруг растровой маски:
                    # на линейном склоне x постоянен.
                    self.assertAlmostEqual(x, expected_x, delta=0.1)
                    point = ogr.Geometry(ogr.wkbPoint)
                    point.AddPoint_2D(x, y)
                    self.assertFalse(
                        polygon.Contains(point),
                        "изолиния заходит на воду",
                    )
                for x, y in (
                    geometry.GetPoint_2D(0),
                    geometry.GetPoint_2D(geometry.GetPointCount() - 1),
                ):
                    if abs(y - 600) < 0.1 or abs(y - 1200) < 0.1:
                        endpoints_on_water += 1
            self.assertGreaterEqual(endpoints_on_water, 30)
            result = None


class NativeTerrainResolutionTest(unittest.TestCase):
    """Уклон и отмывка считаются в разрешении исходного DEM."""

    def test_fine_dem_keeps_native_size(self):
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
            dataset = driver.Create(
                str(dem_path), 40, 40, 1, gdal.GDT_Float32
            )
            # 0,0002° — 0,72″, точнее одной секунды.
            dataset.SetGeoTransform((0, 0.0002, 0, 0, 0, -0.0002))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            x = np.linspace(0, 100, 40, dtype=np.float32)
            dataset.GetRasterBand(1).WriteArray(np.tile(x, (40, 1)))
            dataset = None

            generate_hillshade_slope(config, None)

            for name in (
                "slope.tif",
                "hillshade_composite.tif",
            ):
                raster = gdal.Open(str(root / name))
                self.assertIsNotNone(raster, name)
                self.assertEqual(raster.RasterXSize, 40, name)
                self.assertEqual(raster.RasterYSize, 40, name)
                if name == "slope.tif":
                    band = raster.GetRasterBand(1)
                    self.assertEqual(band.DataType, gdal.GDT_Byte)
                    self.assertEqual(band.GetNoDataValue(), 255)
                raster = None
            self.assertFalse((root / "merged_dem_upscaled.tif").exists())
            self.assertFalse((root / "merged_dem_downscaled.tif").exists())
            self.assertFalse((root / "merged_dem_x2.tif").exists())

    def test_coarse_dem_keeps_native_size(self):
        """DEM 1″ больше не укрупняется: растры остаются нативными."""
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
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(dem_path), 40, 40, 1, gdal.GDT_Float32
            )
            # 0,0002772° — это 0,998″; так GLO-30 выглядит после варпа.
            dataset.SetGeoTransform((0, 0.0002772, 0, 0, 0, -0.0002772))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            x = np.linspace(0, 100, 40, dtype=np.float32)
            dataset.GetRasterBand(1).WriteArray(np.tile(x, (40, 1)))
            dataset = None

            generate_hillshade_slope(config, None)

            for name in (
                "slope.tif",
                "hillshade_composite.tif",
            ):
                raster = gdal.Open(str(root / name))
                self.assertIsNotNone(raster, name)
                self.assertEqual(raster.RasterXSize, 40, name)
                self.assertEqual(raster.RasterYSize, 40, name)
                if name == "slope.tif":
                    band = raster.GetRasterBand(1)
                    self.assertEqual(band.DataType, gdal.GDT_Byte)
                    self.assertEqual(band.GetNoDataValue(), 255)
                raster = None
            self.assertFalse((root / "merged_dem_upscaled.tif").exists())


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


class DiskSpaceGuardTest(unittest.TestCase):
    """Без свободного места подготовка объясняет это понятной ошибкой."""

    def test_transform_raster_reports_missing_space(self):
        from osgeo import gdal, osr
        import numpy as np
        from unittest import mock

        from qgis_xtopo.core import disk_space, terrain

        gdal.UseExceptions()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.tif"
            destination_path = root / "destination.tif"
            driver = gdal.GetDriverByName("GTiff")
            source = driver.Create(str(source_path), 8, 8, 1, gdal.GDT_Float32)
            source.SetGeoTransform((0, 0.01, 0, 0, 0, -0.01))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            source.SetProjection(srs.ExportToWkt())
            source.GetRasterBand(1).WriteArray(
                np.zeros((8, 8), dtype="float32")
            )
            source = None

            with mock.patch.object(disk_space, "free_bytes", return_value=0):
                with self.assertRaises(
                    disk_space.InsufficientSpace
                ) as context:
                    terrain._transform_raster(
                        source_path, destination_path, lambda array: array
                    )
            self.assertIn("Недостаточно места", str(context.exception))
            # Битый файл на забитом диске только зря занимает место.
            self.assertFalse(destination_path.exists())

    def test_write_error_is_reported_as_missing_space(self):
        from unittest import mock

        from qgis_xtopo.core import disk_space, terrain

        with mock.patch.object(disk_space, "free_bytes", return_value=0):
            error = terrain._write_error(
                "slope.tif", RuntimeError("TIFFAppendToStrip:Write error")
            )
        self.assertIsInstance(error, disk_space.InsufficientSpace)
        self.assertIn("slope.tif", str(error))

    def test_transform_raster_cleans_up_after_write_error(self):
        """Ошибка записи на заполненном диске понятна, битый файл убран."""
        import numpy as np
        from unittest import mock

        from qgis_xtopo.core import disk_space, terrain

        class Band:
            def GetNoDataValue(self):
                return None

            def SetNoDataValue(self, value):
                pass

            def ReadAsArray(self, x, y, columns, rows):
                return np.zeros((rows, columns), dtype="float32")

            def WriteArray(self, block, x, y):
                raise RuntimeError(
                    "TIFFAppendToStrip:Write error at scanline 13262"
                )

            def FlushCache(self):
                pass

        class Dataset:
            RasterXSize = 8
            RasterYSize = 8

            def GetGeoTransform(self):
                return (0, 0.01, 0, 0, 0, -0.01)

            def GetProjection(self):
                return ""

            def GetRasterBand(self, index):
                return Band()

        class Driver:
            def Create(self, path, *args, **kwargs):
                Path(path).write_bytes(b"partial raster")
                return Dataset()

        gdal = mock.Mock()
        gdal.GetDriverByName.return_value = Driver()
        gdal.Open.return_value = Dataset()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "destination.tif"
            with mock.patch.object(
                terrain, "_gdal", return_value=(gdal, None, None)
            ), mock.patch.object(disk_space, "free_bytes", return_value=0):
                with self.assertRaises(
                    disk_space.InsufficientSpace
                ) as context:
                    terrain._transform_raster(
                        root / "source.tif", destination,
                        lambda array: array,
                    )
            self.assertIn("Недостаточно места", str(context.exception))
            self.assertFalse(destination.exists())

    def test_relief_stage_warns_about_missing_space(self):
        from osgeo import gdal, osr
        import numpy as np
        from unittest import mock

        from qgis_xtopo.core import disk_space
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
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(dem_path), 20, 20, 1, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((0, 0.01, 0, 0, 0, -0.01))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            dataset.GetRasterBand(1).WriteArray(
                np.zeros((20, 20), dtype="float32")
            )
            dataset = None

            messages = []
            with mock.patch.object(
                disk_space,
                "space_warning",
                return_value=(
                    "Недостаточно места для рельефа: нужно ≈1 ГБ, "
                    "свободно 0 ГБ."
                ),
            ):
                generate_hillshade_slope(
                    config, None, progress=messages.append
                )
            self.assertTrue(
                any(message.startswith("WARNING:") for message in messages),
                "предупреждение о месте должно попасть в журнал",
            )


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

    def test_label_color_is_lighter_on_basemap(self):
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import label_color_for

        for rgb in ((0, 255, 0), (220, 120, 10), (60, 120, 200)):
            source = QColor(*rgb)
            color = label_color_for(source, light=True)
            if source.hue() >= 0:
                self.assertEqual(color.hue(), source.hue())
            self.assertGreater(color.lightness(), source.lightness())
            self.assertGreaterEqual(color.value(), source.value())

    def test_apply_light_bold_labels(self):
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import (
            _isoline_label_styles, _label_settings, apply_isoline_color,
            label_color_for,
        )

        layer = self._layer()
        self.assertTrue(apply_isoline_color(
            layer, QColor("#00ff00"), light=True, bold=True
        ))
        labels = _isoline_label_styles(layer)
        self.assertEqual(
            labels[0][0], label_color_for(QColor("#00ff00"), light=True)
        )
        for _rule, settings in _label_settings(layer.labeling()):
            self.assertTrue(settings.format().font().bold())

    def test_apply_label_bold(self):
        from qgis_xtopo.gui.projectpanel import (
            _label_settings, apply_isoline_label_bold,
        )

        layer = self._layer()
        self.assertTrue(apply_isoline_label_bold(layer, True))
        for _rule, settings in _label_settings(layer.labeling()):
            self.assertTrue(settings.format().font().bold())
        self.assertFalse(apply_isoline_label_bold(layer, True))
        self.assertTrue(apply_isoline_label_bold(layer, False))
        for _rule, settings in _label_settings(layer.labeling()):
            self.assertFalse(settings.format().font().bold())


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
class BasemapLabelInversionTest(unittest.TestCase):
    """Инверсия надписей: цвет текста и цвет halo меняются местами."""

    LAYER = "mountain area"

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.core import basemap
        from qgis_xtopo.gui import projectpanel as pp

        self.basemap = basemap
        self.project = QgsProject.instance()
        self.project.clear()
        renderer, labeling = pp._template_layer_objects(self.LAYER)
        self.layer = QgsVectorLayer(
            "Polygon?crs=EPSG:4326", self.LAYER, "memory"
        )
        self.layer.setRenderer(renderer)
        self.layer.setLabeling(labeling)
        self.project.addMapLayer(self.layer)

    def _colors(self, layer=None):
        """Цвета текста и halo всех правил подписей слоя."""
        from qgis_xtopo.core import label_style

        layer = layer or self.layer
        colors = []
        for _key, settings in label_style.label_settings(layer.labeling()):
            text, halo, _enabled = label_style.label_colors(settings)
            colors.append((text, halo))
        return colors

    def _buffer_effects(self, layer=None):
        """Состояние paint effect у буфера каждого правила."""
        from qgis_xtopo.core import label_style

        layer = layer or self.layer
        return [
            label_style.label_buffer_effect(settings)
            for _key, settings in label_style.label_settings(layer.labeling())
        ]

    def test_inversion_swaps_text_and_halo_colors(self):
        before = self._colors()
        self.assertTrue(before)
        self.assertTrue(self.basemap.invert_labels(self.project))
        after = self._colors()
        for (text, halo), (new_text, new_halo) in zip(before, after):
            # Halo получает прежний цвет текста, текст — прежний halo.
            self.assertEqual(new_halo, text)
            self.assertEqual(new_text, halo.lighter(150))
            # Прозрачность не трогаем.
            self.assertEqual(new_text.alpha(), text.alpha())
            self.assertEqual(new_halo.alpha(), halo.alpha())

    def test_admin_level_labels_are_not_inverted(self):
        """Подписи административных границ сохраняют свои цвета."""
        from qgis.core import QgsVectorLayer
        from qgis_xtopo.gui import projectpanel as pp

        name = "admin level 2 (country names)"
        renderer, labeling = pp._template_layer_objects(name)
        layer = QgsVectorLayer(
            "Polygon?crs=EPSG:4326", name, "memory"
        )
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        self.project.addMapLayer(layer)
        before = self._colors(layer)
        self.assertTrue(before)

        self.basemap.invert_labels(self.project)
        self.assertEqual(self._colors(layer), before)
        self.assertIsNone(
            layer.customProperty(self.basemap.LABEL_COLORS_PROPERTY)
        )

    def test_inversion_removes_outer_buffer_glow(self):
        """Внешнее свечение буфера на подложке выключается."""
        before = self._buffer_effects()
        self.assertTrue(any(before), before)
        self.assertTrue(self.basemap.invert_labels(self.project))
        after = self._buffer_effects()
        for old, new in zip(before, after):
            if old is not None:
                self.assertFalse(new)
        self.assertTrue(self.basemap.restore_labels(self.project))
        self.assertEqual(self._buffer_effects(), before)

    def test_pre_disabled_buffer_glow_stays_disabled(self):
        """Если свечение было выключено, инверсия его не включает."""
        from qgis_xtopo.core import label_style

        for key, settings in label_style.label_settings(
            self.layer.labeling()
        ):
            label_style.set_label_buffer_effect(settings, False)
            label_style.set_label_settings(
                self.layer.labeling(), key, settings
            )
        before = self._buffer_effects()
        self.assertTrue(before)
        self.assertTrue(all(effect is not True for effect in before))

        self.basemap.invert_labels(self.project)
        self.assertTrue(
            all(effect is not True for effect in self._buffer_effects())
        )
        self.basemap.restore_labels(self.project)
        self.assertEqual(self._buffer_effects(), before)

    def test_halo_becomes_white_for_light_halo(self):
        """Не совсем белый halo усиливается до белого."""
        from qgis.PyQt.QtGui import QColor

        new_text, new_halo = self.basemap.inverted_label_colors(
            QColor(0, 0, 0, 255), QColor(243, 243, 243, 255)
        )
        self.assertEqual(new_text, QColor(255, 255, 255, 255))
        self.assertEqual(new_halo, QColor(0, 0, 0, 255))

    def test_restore_returns_original_colors(self):
        before = self._colors()
        self.basemap.invert_labels(self.project)
        self.assertTrue(self.basemap.restore_labels(self.project))
        self.assertEqual(self._colors(), before)
        self.assertIsNone(
            self.layer.customProperty(self.basemap.LABEL_COLORS_PROPERTY)
        )

    def test_inversion_is_not_applied_twice(self):
        self.basemap.invert_labels(self.project)
        inverted = self._colors()
        self.basemap.invert_labels(self.project)
        self.assertEqual(self._colors(), inverted)

    def test_inversion_returns_changed_layer_to_uniform_state(self):
        """Слой, стиль которого поменяли стороной, снова инвертируется."""
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.core import label_style

        self.basemap.invert_labels(self.project)
        inverted = self._colors()
        # Стиль слоя поменяли стороной: цвет текста вернули прежним.
        key, settings = next(
            iter(label_style.label_settings(self.layer.labeling()))
        )
        label_style.set_label_colors(settings, text_color=QColor(0, 0, 0))
        label_style.set_label_settings(self.layer.labeling(), key, settings)
        self.assertNotEqual(self._colors(), inverted)
        # Повторная инверсия снова делает все слои одинаковыми.
        self.basemap.invert_labels(self.project)
        self.assertEqual(self._colors(), inverted)

    def test_labels_without_halo_are_not_touched(self):
        """Подписи без halo инверсия не меняет: менять нечего."""
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.core import label_style
        from qgis_xtopo.gui import projectpanel as pp

        renderer, labeling = pp._template_layer_objects(pp.ISOLINES_LAYER)
        layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326", pp.ISOLINES_LAYER, "memory"
        )
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        self.project.addMapLayer(layer)

        def colors():
            result = []
            for _key, settings in label_style.label_settings(
                layer.labeling()
            ):
                text, halo, _enabled = label_style.label_colors(settings)
                result.append((text, halo))
            return result

        before = colors()
        self.basemap.invert_labels(self.project)
        self.assertEqual(colors(), before)
        self.assertIsNone(
            layer.customProperty(self.basemap.LABEL_COLORS_PROPERTY)
        )
        self.project.removeMapLayer(layer.id())

    def test_label_exclusion_follows_tags(self):
        """Тип дороги определяется по тегам, а не по цвету надписи."""
        from qgis_xtopo.core import label_tags

        main = label_tags.profile(
            "\"highway='motorway' OR highway='trunk' OR highway='primary'\""
        )
        self.assertEqual(
            main.classes, frozenset({"motorway", "trunk", "primary"})
        )
        self.assertTrue(label_tags.excluded(main))
        # Улицы нарисованы полосой так же, как магистрали.
        streets = label_tags.profile(
            "\"highway='residential' OR highway='service'\""
        )
        self.assertTrue(label_tags.excluded(streets))
        # Примыкания — тоже дороги.
        links = label_tags.profile(
            "\"highway='primary_link' OR highway='trunk_link'\""
        )
        self.assertTrue(label_tags.excluded(links))
        # Тропы нарисованы линией: их надписи инверсия меняет.
        paths = label_tags.profile(
            "\"highway='track' OR highway='path' OR highway='footway' "
            "OR highway='cycleway' OR highway='bridleway'\""
        )
        self.assertFalse(label_tags.excluded(paths))
        # Условие слоя «не улица» — это магистрали и второстепенные.
        major = label_tags.profile(
            "\"highway\" NOT IN ('residential','unclassified','living_street',"
            "'pedestrian','service')"
        )
        self.assertTrue(label_tags.excluded(major))
        # Дополнение к «NOT IN» тоже состоит из одних дорог.
        complement = label_tags.profile(
            "\"highway\" NOT IN ('residential','unclassified','service')"
        )
        self.assertTrue(label_tags.excluded(complement))

    def test_area_road_tags_are_recognized(self):
        """Площадная дорога отличается от площади по тегам."""
        from qgis_xtopo.core import label_tags

        area = label_tags.profile("\"area:highway='pedestrian'\"")
        self.assertTrue(area.area_road)
        self.assertFalse(area.square)
        self.assertTrue(label_tags.excluded(area))
        # Площадь: инверсия её надписи меняет.
        square = label_tags.profile("\"place='square'\"")
        self.assertTrue(square.square)
        self.assertFalse(label_tags.excluded(square))
        # Площадь, нарисованная площадной дорогой, остаётся площадью.
        both = label_tags.profile(
            "\"area:highway='pedestrian' AND place='square'\""
        )
        self.assertFalse(label_tags.excluded(both))

    # Условия слоёв дорог в шаблоне: вблизи все дороги в одном слое.
    ROAD_LAYERS = (
        ("highway main", ""),
    )

    def test_road_labels_are_not_inverted(self):
        """Надписи дорог инверсия не меняет: они лежат на самой полосе."""
        from qgis.core import QgsVectorLayer
        from qgis_xtopo.core import label_style, label_tags
        from qgis_xtopo.gui import projectpanel as pp

        for name, subset in self.ROAD_LAYERS:
            renderer, labeling = pp._template_layer_objects(name)
            layer = QgsVectorLayer(
                "LineString?crs=EPSG:4326", name, "memory"
            )
            layer.setRenderer(renderer)
            layer.setLabeling(labeling)
            layer.setSubsetString(subset)
            self.project.addMapLayer(layer)

            def colors():
                result = []
                for _key, settings in label_style.label_settings(
                    layer.labeling()
                ):
                    text, halo, _enabled = label_style.label_colors(settings)
                    result.append((text, halo))
                return result

            # Все правила слоя отбирают дороги: магистрали, улицы,
            # примыкания, щиты с номером дороги.
            filters = label_tags.rule_filters(layer.labeling())
            layer_item = label_tags.layer_profile(layer)
            for key in layer.labeling().subProviders():
                rule = label_tags.profile(filters.get(key, ""))
                self.assertTrue(
                    label_tags.excluded(
                        label_tags.combine(rule, layer_item)
                    ),
                    f"{name}: правило {key[:10]} не распознано как дорога",
                )

            before = colors()
            self.assertTrue(before)
            self.basemap.invert_labels(self.project)
            self.assertEqual(colors(), before, f"{name}: цвета изменились")
            self.project.removeMapLayer(layer.id())

    def test_blocking_road_layer_keeps_wide_band_labels(self):
        """Подписи слоя-блокировщика магистралей не инвертируются.

        Слой `[blocking] highway main` подписывает те же дороги, что и
        `highway main`, но его условие слоя («не улица») говорит о типах
        дорог: без него надписи магистралей инвертировались бы.
        """
        from qgis.core import QgsVectorLayer
        from qgis_xtopo.core import label_style
        from qgis_xtopo.gui import projectpanel as pp

        renderer, labeling = pp._template_layer_objects("[blocking] highway main")
        layer = QgsVectorLayer(
            "LineString?crs=EPSG=4326", "[blocking] highway main", "memory"
        )
        layer.setRenderer(renderer)
        layer.setLabeling(labeling)
        layer.setSubsetString(
            "\"highway\" NOT IN ('residential','unclassified',"
            "'living_street','pedestrian','service')"
        )
        self.project.addMapLayer(layer)

        def colors():
            result = []
            for _key, settings in label_style.label_settings(
                layer.labeling()
            ):
                text, halo, _enabled = label_style.label_colors(settings)
                result.append((text, halo))
            return result

        before = colors()
        self.assertTrue(before)
        self.basemap.invert_labels(self.project)
        self.assertEqual(colors(), before)
        self.project.removeMapLayer(layer.id())

    def _square_layer(self, place, area):
        """Слой с подписями площадей и объекты с тегами."""
        from qgis.PyQt.QtCore import QVariant
        from qgis.core import QgsField, QgsFeature, QgsVectorLayer
        from qgis_xtopo.gui import projectpanel as pp

        _renderer, labeling = pp._template_layer_objects(
            "place square highway area names"
        )
        layer = QgsVectorLayer(
            "Polygon?crs=EPSG=4326", "place square highway area names",
            "memory",
        )
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("area:highway", QVariant.String),
            QgsField("place", QVariant.String),
        ])
        layer.updateFields()
        feature = QgsFeature(layer.fields())
        feature.setAttributes([area, place])
        provider.addFeatures([feature])
        layer.setLabeling(labeling)
        self.project.addMapLayer(layer)
        return layer

    def test_area_road_labels_are_not_inverted(self):
        """Надписи площадных дорог инверсия не меняет."""
        from qgis_xtopo.core import label_style

        layer = self._square_layer(None, "pedestrian")

        def colors():
            result = []
            for _key, settings in label_style.label_settings(
                layer.labeling()
            ):
                text, halo, _enabled = label_style.label_colors(settings)
                result.append((text, halo))
            return result

        before = colors()
        self.assertTrue(before)
        self.basemap.invert_labels(self.project)
        self.assertEqual(colors(), before)
        self.assertIsNone(
            layer.customProperty(self.basemap.LABEL_COLORS_PROPERTY)
        )
        self.project.removeMapLayer(layer.id())

    def test_square_labels_are_inverted(self):
        """Надписи площадей инверсия меняет."""
        from qgis_xtopo.core import label_style

        layer = self._square_layer("square", None)

        def colors():
            result = []
            for _key, settings in label_style.label_settings(
                layer.labeling()
            ):
                text, halo, _enabled = label_style.label_colors(settings)
                result.append((text, halo))
            return result

        before = colors()
        self.basemap.invert_labels(self.project)
        after = colors()
        self.assertTrue(before)
        self.assertNotEqual(after, before)
        self.project.removeMapLayer(layer.id())


class BasemapSymbolInversionTest(unittest.TestCase):
    """Инверсия символов: дороги, скалы, ЛЭП, подъёмники, железные дороги."""

    TEMPLATE_LAYERS = (
        ("track", "LineString"),
        ("path", "LineString"),
        ("ridge, arete", "LineString"),
        ("power line", "LineString"),
        ("power minor line", "LineString"),
        ("aerialway cable car, gondola", "LineString"),
        ("railway all", "LineString"),
        ("rail tunnel", "LineString"),
        ("railway bridge", "LineString"),
        ("power tower", "Point"),
        ("power pole", "Point"),
        ("alpine hut", "Point"),
        ("places main", "Point"),
        ("admin level 2 (country borders)", "LineString"),
    )

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app
        from qgis_xtopo.gui import projectpanel as pp

        cls.app = qgis_app()
        cls.renderers = {}
        for name, _geometry in cls.TEMPLATE_LAYERS:
            renderer, _labeling = pp._template_layer_objects(name)
            cls.renderers[name] = renderer

    def setUp(self):
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.core import basemap

        self.basemap = basemap
        self.project = QgsProject.instance()
        self.project.clear()
        self.layers = {}
        for name, geometry in self.TEMPLATE_LAYERS:
            layer = QgsVectorLayer(
                f"{geometry}?crs=EPSG:4326", name, "memory"
            )
            layer.setRenderer(self.renderers[name].clone())
            self.project.addMapLayer(layer)
            self.layers[name] = layer

    def tearDown(self):
        self.project.clear()

    def _of_type(self, layer, classes):
        return [
            (key, symbol_layer)
            for key, symbol_layer
            in self.basemap._symbol_layers_by_key(layer).items()
            if isinstance(symbol_layer, classes)
        ]

    def _line_colors(self, layer):
        from qgis.core import (
            QgsHashedLineSymbolLayer,
            QgsSimpleLineSymbolLayer,
        )

        return [
            symbol_layer.color()
            for _key, symbol_layer in self._of_type(
                layer, (QgsSimpleLineSymbolLayer,
                        QgsHashedLineSymbolLayer)
            )
        ]

    def _all_line_colors(self, layer):
        from qgis.core import (
            QgsHashedLineSymbolLayer,
            QgsSimpleLineSymbolLayer,
            QgsSimpleMarkerSymbolLayer,
        )

        return [
            symbol_layer.color()
            for _key, symbol_layer in self._of_type(
                layer, (QgsSimpleLineSymbolLayer, QgsHashedLineSymbolLayer,
                        QgsSimpleMarkerSymbolLayer)
            )
        ]

    def _enabled_effects(self, layer):
        return [
            key for key, symbol_layer
            in self.basemap._symbol_layers_by_key(layer).items()
            if symbol_layer.paintEffect() is not None
            and symbol_layer.paintEffect().enabled()
        ]

    def test_roads_and_paths_become_light(self):
        from qgis.PyQt.QtGui import QColor

        expected = QColor(self.basemap.ROAD_COLOR)
        for name in ("track", "path"):
            layer = self.layers[name]
            before = self._line_colors(layer)
            self.assertTrue(before)
            self.assertTrue(self.basemap.invert_symbols(self.project))
            self.assertTrue(
                all(color == expected for color in self._line_colors(layer)),
                name,
            )
            self.assertTrue(self.basemap.restore_symbols(self.project))
            self.assertEqual(self._line_colors(layer), before, name)

    def test_admin_level_borders_are_not_inverted(self):
        """Символы административных границ инверсия не перекрашивает."""
        layer = self.layers["admin level 2 (country borders)"]
        before = self._line_colors(layer)
        self.assertTrue(before)
        self.basemap.invert_symbols(self.project)
        self.assertEqual(self._line_colors(layer), before)

    def test_ridge_is_light_brown(self):
        from qgis.PyQt.QtGui import QColor

        layer = self.layers["ridge, arete"]
        before = self._line_colors(layer)
        self.assertTrue(before)
        self.basemap.invert_symbols(self.project)
        expected = QColor(self.basemap.RIDGE_COLOR)
        self.assertTrue(
            all(color == expected for color in self._line_colors(layer))
        )
        self.basemap.restore_symbols(self.project)
        self.assertEqual(self._line_colors(layer), before)

    def test_power_lines_are_grey(self):
        from qgis.PyQt.QtGui import QColor

        expected = QColor(self.basemap.POWER_COLOR)
        for name in ("power line", "power minor line"):
            layer = self.layers[name]
            before = self._all_line_colors(layer)
            self.assertTrue(before)
            self.basemap.invert_symbols(self.project)
            self.assertTrue(
                all(
                    color == expected
                    for color in self._all_line_colors(layer)
                ),
                name,
            )
            self.basemap.restore_symbols(self.project)
            self.assertEqual(self._all_line_colors(layer), before, name)

    def test_aerialways_are_grey_white(self):
        from qgis.core import QgsSimpleLineSymbolLayer
        from qgis.PyQt.QtGui import QColor

        layer = self.layers["aerialway cable car, gondola"]
        before = self._line_colors(layer)
        self.basemap.invert_symbols(self.project)
        colors = {
            symbol_layer.width(): symbol_layer.color()
            for _key, symbol_layer in self._of_type(
                layer, QgsSimpleLineSymbolLayer
            )
        }
        self.assertIn(QColor(self.basemap.AERIALWAY_CASING_COLOR),
                      colors.values())
        self.assertIn(QColor(self.basemap.AERIALWAY_LINE_COLOR),
                      colors.values())
        self.assertNotIn(QColor("#000000"), colors.values())
        self.basemap.restore_symbols(self.project)
        self.assertEqual(self._line_colors(layer), before)

    def test_railways_are_white(self):
        from qgis.PyQt.QtGui import QColor

        expected = QColor(self.basemap.RAIL_COLOR)
        for name in ("railway all", "rail tunnel", "railway bridge"):
            layer = self.layers[name]
            before = self._line_colors(layer)
            self.assertTrue(before, name)
            self.basemap.invert_symbols(self.project)
            self.assertTrue(
                all(color == expected for color in self._line_colors(layer)),
                name,
            )
            self.basemap.restore_symbols(self.project)
            self.assertEqual(self._line_colors(layer), before, name)

    def test_power_poles_have_smaller_halo(self):
        """Столбам ЛЭП обводка вдвое уже общей."""
        from qgis.core import QgsOuterGlowEffect

        scale = self.basemap.POWER_POLE_GLOW_SCALE
        expected_spread = self.basemap.ICON_GLOW_SPREAD_MM * scale
        expected_blur = self.basemap.ICON_GLOW_BLUR_MM * scale
        for name in self.basemap.POWER_POLE_LAYERS:
            layer = self.layers[name]
            self.basemap.invert_symbols(self.project)
            effects = [
                symbol_layer.paintEffect()
                for _key, symbol_layer
                in self.basemap._symbol_layers_by_key(layer).items()
                if symbol_layer.paintEffect() is not None
                and symbol_layer.paintEffect().enabled()
            ]
            self.assertTrue(effects, name)
            for effect in effects:
                glow = effect.effect(0)
                self.assertIsInstance(glow, QgsOuterGlowEffect, name)
                self.assertAlmostEqual(glow.spread(), expected_spread, msg=name)
                self.assertAlmostEqual(
                    glow.blurLevel(), expected_blur, msg=name
                )
            self.basemap.restore_symbols(self.project)
            self.assertEqual(self._enabled_effects(layer), [], name)

    def test_black_icons_get_white_outline(self):
        from qgis.core import (
            QgsColorEffect,
            QgsDrawSourceEffect,
            QgsOuterGlowEffect,
            QgsSvgMarkerSymbolLayer,
        )
        from qgis.PyQt.QtGui import QColor

        layer = self.layers["alpine hut"]
        self.assertEqual(self._enabled_effects(layer), [])
        self.basemap.invert_symbols(self.project)
        icons = self._of_type(layer, QgsSvgMarkerSymbolLayer)
        self.assertTrue(icons)
        for key, symbol_layer in icons:
            effect = symbol_layer.paintEffect()
            self.assertIsNotNone(effect, key)
            self.assertTrue(effect.enabled(), key)
            effects = [effect.effect(index)
                       for index in range(effect.count())]
            # Обводка идёт первой, значок рисуется поверх неё и остаётся
            # исходным: цвета эффекта не меняют.
            self.assertIsInstance(effects[0], QgsOuterGlowEffect, key)
            self.assertEqual(
                effects[0].color(), QColor(*self.basemap.ICON_GLOW_COLOR)
            )
            self.assertEqual(effects[0].color().alpha(), 255)
            # Обводка остаётся узкой: широкое гало затеняет снимок.
            self.assertEqual(
                effects[0].spread(), self.basemap.ICON_GLOW_SPREAD_MM, key
            )
            self.assertEqual(
                effects[0].blurLevel(), self.basemap.ICON_GLOW_BLUR_MM, key
            )
            self.assertIsInstance(effects[-1], QgsDrawSourceEffect, key)
            self.assertFalse(
                any(isinstance(item, QgsColorEffect) for item in effects),
                key,
            )
        self.basemap.restore_symbols(self.project)
        self.assertEqual(self._enabled_effects(layer), [])
        self.assertIsNone(layer.customProperty(
            self.basemap.ICON_EFFECTS_PROPERTY
        ))

    def test_colored_icons_are_not_touched(self):
        from qgis.core import QgsSvgMarkerSymbolLayer

        layer = self.layers["places main"]
        self.basemap.invert_symbols(self.project)
        icons = self._of_type(layer, QgsSvgMarkerSymbolLayer)
        self.assertTrue(icons)
        self.assertEqual(self._enabled_effects(layer), [])

    def test_white_filled_icons_keep_their_path(self):
        """Светлая заливка остаётся исходной: копий SVG больше нет."""
        from qgis.core import (
            QgsMarkerSymbol,
            QgsSingleSymbolRenderer,
            QgsSvgMarkerSymbolLayer,
            QgsVectorLayer,
        )
        from qgis_xtopo.core.resources import resource_path

        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326", "station icons", "memory"
        )
        icon = QgsSvgMarkerSymbolLayer(
            resource_path("icons", "aerialway_station.svg")
        )
        layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol([icon])))
        self.project.addMapLayer(layer)
        original = icon.path()

        self.basemap.invert_symbols(self.project)
        outlined = next(
            iter(self.basemap._symbol_layers_by_key(layer).values())
        )
        self.assertEqual(outlined.path(), original)
        self.assertIsNotNone(outlined.paintEffect())

        self.basemap.restore_symbols(self.project)
        self.assertEqual(outlined.path(), original)
        self.assertIsNone(outlined.paintEffect())

    def test_black_svg_detection(self):
        self.assertTrue(self.basemap._is_black_icon("peak.svg"))
        self.assertTrue(
            self.basemap._is_black_icon("../icons/alpine_hut.svg")
        )
        self.assertFalse(self.basemap._is_black_icon("place_1.svg"))
        self.assertFalse(self.basemap._is_black_icon("water_point.svg"))

    def test_inversion_is_not_applied_twice(self):
        layer = self.layers["track"]
        self.basemap.invert_symbols(self.project)
        colors = self._line_colors(layer)
        effects = self._enabled_effects(self.layers["alpine hut"])
        self.basemap.invert_symbols(self.project)
        self.assertEqual(self._line_colors(layer), colors)
        self.assertEqual(
            self._enabled_effects(self.layers["alpine hut"]), effects
        )

    def test_symbols_survive_project_save_and_restore(self):
        from qgis.core import (
            QgsMarkerSymbol,
            QgsProject,
            QgsSingleSymbolRenderer,
            QgsSvgMarkerSymbolLayer,
            QgsVectorLayer,
        )
        from qgis_xtopo.core.resources import resource_path

        icon = QgsSvgMarkerSymbolLayer(
            resource_path("icons", "aerialway_station.svg")
        )
        station = QgsVectorLayer(
            "Point?crs=EPSG:4326", "station icons", "memory"
        )
        station.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol([icon])))
        self.project.addMapLayer(station)
        original = icon.path()

        before = self._line_colors(self.layers["track"])
        self.basemap.invert_symbols(self.project)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qgz"
            self.assertTrue(self.project.write(str(path)))
            saved = QgsProject()
            self.assertTrue(saved.read(str(path)))
            try:
                layer = saved.mapLayersByName("track")[0]
                self.assertTrue(self.basemap.restore_symbols(saved))
                self.assertEqual(self._line_colors(layer), before)
                hut = saved.mapLayersByName("alpine hut")[0]
                self.assertEqual(self._enabled_effects(hut), [])
                saved_station = saved.mapLayersByName("station icons")[0]
                saved_icon = next(
                    symbol_layer for symbol_layer
                    in self.basemap._symbol_layers_by_key(
                        saved_station
                    ).values()
                )
                self.assertEqual(saved_icon.path(), original)
            finally:
                saved.clear()


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class BasemapIsolineStyleTest(unittest.TestCase):
    """На подложке изогипсы зелёные, толстые и со светлыми жирными подписями."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.core import basemap
        from qgis_xtopo.gui import projectpanel as pp

        self.project = QgsProject.instance()
        self.project.clear()
        renderer, labeling = pp._template_layer_objects(pp.ISOLINES_LAYER)
        self.layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326", pp.ISOLINES_LAYER, "memory"
        )
        self.layer.setRenderer(renderer)
        self.layer.setLabeling(labeling)
        self.project.addMapLayer(self.layer)
        fake = QgsVectorLayer("Point?crs=EPSG:4326", "Google sat", "memory")
        fake.setCustomProperty(basemap.SOURCE_PROPERTY, "google")
        self.project.addMapLayer(fake)
        self.panel = pp.ProjectPanel(None)
        self.panel._capture_isoline_baseline()

    def tearDown(self):
        self.panel.deleteLater()
        self.project.clear()

    def _labels(self):
        from qgis_xtopo.gui import projectpanel as pp

        return [
            settings.format()
            for _rule, settings in pp._label_settings(self.layer.labeling())
        ]

    def test_basemap_forces_green_and_light_labels(self):
        """Подложка красит изогипсы в зелёный, ширину не меняет."""
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel as pp

        self.assertTrue(self.panel._apply_basemap_isoline_style())
        # Двойную ширину подложка не включает — это выбор пользователя.
        self.assertFalse(pp.isolines_width_doubled(self.project))
        colors = pp._isoline_symbol_colors(self.layer)
        self.assertEqual(colors[0], QColor("#00ff00"))
        expected = pp.label_color_for(QColor("#00ff00"), light=True)
        for fmt in self._labels():
            self.assertEqual(fmt.color(), expected)
            self.assertFalse(fmt.font().bold())
        self.assertEqual(
            self.layer.customProperty(pp.BASEMAP_WIDTH_PROPERTY), "0"
        )
        self.assertEqual(
            self.layer.customProperty(pp.BASEMAP_COLOR_PROPERTY), ""
        )

        self.assertTrue(self.panel._restore_isoline_style_after_basemap())
        self.assertFalse(pp.isolines_width_doubled(self.project))
        self.assertFalse(any(fmt.font().bold() for fmt in self._labels()))
        template = pp.template_isoline_styles(pp.ISOLINES_LAYER)
        self.assertEqual(pp._isoline_symbol_colors(self.layer), template[0])
        self.assertIsNone(
            self.layer.customProperty(pp.BASEMAP_WIDTH_PROPERTY)
        )
        self.assertIsNone(
            self.layer.customProperty(pp.BASEMAP_COLOR_PROPERTY)
        )

    def test_basemap_keeps_chosen_double_width(self):
        """Выбранную пользователем двойную ширину подложка не сбрасывает."""
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui import projectpanel as pp

        self.panel._isoline_width_toggled(True)
        self.assertTrue(pp.isolines_width_doubled(self.project))
        self.assertTrue(self.panel._apply_basemap_isoline_style())
        self.assertTrue(pp.isolines_width_doubled(self.project))
        expected = pp.label_color_for(QColor("#00ff00"), light=True)
        for fmt in self._labels():
            self.assertEqual(fmt.color(), expected)
            self.assertTrue(fmt.font().bold())
        self.assertTrue(self.panel._restore_isoline_style_after_basemap())
        self.assertTrue(pp.isolines_width_doubled(self.project))

    def test_double_width_makes_labels_bold(self):
        from qgis_xtopo.gui import projectpanel as pp

        self.panel._isoline_width_toggled(True)
        self.assertTrue(pp.isolines_width_doubled(self.project))
        self.assertTrue(all(fmt.font().bold() for fmt in self._labels()))
        self.panel._isoline_width_toggled(False)
        self.assertFalse(pp.isolines_width_doubled(self.project))
        self.assertFalse(any(fmt.font().bold() for fmt in self._labels()))


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

        path = Path(self.directory.name) / "slope.tif"
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
        raster = root / "raster" / "slope.tif"
        raster.touch()
        self.assertEqual(
            slope_style.project_raster(str(root / "map.qgz")), raster,
        )
        self.assertIsNone(slope_style.project_raster(""))

    def test_legacy_project_raster_path(self):
        """Старые проекты ещё читают slope_upscaled.tif."""
        from qgis_xtopo.core import slope_style

        root = Path(self.directory.name)
        (root / "raster").mkdir()
        raster = root / "raster" / "slope_upscaled.tif"
        raster.touch()
        self.assertEqual(
            slope_style.project_raster(str(root / "map.qgz")), raster,
        )
        (root / "raster" / "slope.tif").touch()
        self.assertEqual(
            slope_style.project_raster(str(root / "map.qgz")),
            root / "raster" / "slope.tif",
        )


if __name__ == "__main__":
    unittest.main()
