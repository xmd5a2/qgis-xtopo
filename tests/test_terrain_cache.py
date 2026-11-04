import os
import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core import terrain_cache
from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config


def _write_layer(path, name, points):
    """Простой слой SQLite для проверки хеша содержимого."""
    from osgeo import ogr, osr

    driver = ogr.GetDriverByName("SQLite")
    dataset = driver.CreateDataSource(str(path))
    spatial_ref = osr.SpatialReference()
    spatial_ref.ImportFromEPSG(4326)
    layer = dataset.CreateLayer(name, spatial_ref, ogr.wkbPoint)
    for x, y in points:
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({x} {y})"))
        layer.CreateFeature(feature)
    dataset = None


class TerrainCacheTest(unittest.TestCase):
    def _config(self, directory):
        config = Config(qgis_projects_dir=directory)
        config.ensure_directories()
        return config

    def test_dem_is_restored_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            names = [
                "merged_dem.tif",
                "merged_dem_3857.tif",
                "merged_dem_upscaled.tif",
                "slope_upscaled.tif",
                "hillshade_composite.tif",
            ]
            for name in names:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            self.assertTrue(terrain_cache.store_dem(config, key))
            for name in names:
                (config.raster_dir / name).unlink()
            self.assertTrue(terrain_cache.restore_dem(config, key))
            for name in names:
                self.assertTrue((config.raster_dir / name).exists(), name)

    def test_dem_is_not_restored_for_other_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            terrain_cache.store_dem(config, key)
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).unlink()
            other = terrain_cache.dem_signature(config, BBox(44.0, 42.0, 44.1, 42.1))
            self.assertNotEqual(key, other)
            self.assertFalse(terrain_cache.restore_dem(config, other))

    def test_dem_signature_depends_on_tiles(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            (config.terrain_input_dir / "N42E043.tif").write_bytes(b"dem")
            first = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            (config.terrain_input_dir / "N42E043.tif").write_bytes(b"other dem")
            second = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            self.assertNotEqual(first, second)

    def test_isolines_signature_depends_on_water_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            first = terrain_cache.isolines_signature(config, bbox, key)
            _write_layer(config.vector_dir / "water.sqlite", "water", [(1.0, 2.0)])
            second = terrain_cache.isolines_signature(config, bbox, key)
            self.assertNotEqual(first, second)
            _write_layer(
                config.vector_dir / "water.sqlite", "water", [(1.0, 2.0), (3.0, 4.0)]
            )
            third = terrain_cache.isolines_signature(config, bbox, key)
            self.assertNotEqual(second, third)

    def test_isolines_signature_depends_on_step(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            first = terrain_cache.isolines_signature(config, bbox, key)
            config.isolines_step = 25
            self.assertNotEqual(first, terrain_cache.isolines_signature(config, bbox, key))

    def test_isolines_are_restored_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            isolines_key = terrain_cache.isolines_signature(config, bbox, key)
            (config.vector_dir / "isolines_regular.sqlite").write_bytes(b"lines")
            (config.vector_dir / "isolines_glacier.sqlite").write_bytes(b"glacier")
            self.assertTrue(terrain_cache.store_isolines(config, isolines_key))
            (config.vector_dir / "isolines_regular.sqlite").unlink()
            (config.vector_dir / "isolines_glacier.sqlite").unlink()
            self.assertTrue(terrain_cache.restore_isolines(config, isolines_key))
            self.assertTrue(
                (config.vector_dir / "isolines_regular.sqlite").exists()
            )
            self.assertTrue(
                (config.vector_dir / "isolines_glacier.sqlite").exists()
            )

    def test_cache_grows_only_by_a_few_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).write_bytes(b"raster")
            for index in range(terrain_cache.KEEP_ENTRIES + 3):
                key = f"key{index}"
                terrain_cache.store_dem(config, key)
            group = config.terrain_result_cache_dir / "dem"
            self.assertLessEqual(
                len([item for item in group.iterdir() if item.is_dir()]),
                terrain_cache.KEEP_ENTRIES,
            )

    def test_plugin_version_is_read(self):
        version = terrain_cache.plugin_version()
        self.assertRegex(version, r"^\d+\.\d+")


class SourceTimingTest(unittest.TestCase):
    def test_download_is_counted_separately(self):
        from qgis_xtopo.core.pipeline import _source_timings

        download, prepared = _source_timings(590.6, 120.0)
        self.assertAlmostEqual(download, 120.0)
        self.assertAlmostEqual(prepared, 470.6)

    def test_download_is_limited_by_total_time(self):
        from qgis_xtopo.core.pipeline import _source_timings

        download, prepared = _source_timings(10.0, 25.0)
        self.assertAlmostEqual(download, 10.0)
        self.assertAlmostEqual(prepared, 0.0)

    def test_local_source_has_no_download_time(self):
        from qgis_xtopo.core.pipeline import _source_timings

        download, prepared = _source_timings(42.0, 0.0)
        self.assertAlmostEqual(download, 0.0)
        self.assertAlmostEqual(prepared, 42.0)


class ExtentMeasurementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_frame_sizes_are_measured_in_kilometres(self):
        from qgis_xtopo.gui.extentdialog import frame_sizes_km

        width, height = frame_sizes_km(BBox(43.0, 42.0, 43.1, 42.1))
        self.assertAlmostEqual(width, 8.27, places=1)
        self.assertAlmostEqual(height, 11.12, places=1)

    def test_length_is_formatted_with_precision(self):
        from qgis_xtopo.gui.extentdialog import format_length

        self.assertEqual(format_length(8.271), "8.27 км")
        self.assertEqual(format_length(12.34), "12.3 км")
        self.assertEqual(format_length(123.4), "123 км")


if __name__ == "__main__":
    unittest.main()
