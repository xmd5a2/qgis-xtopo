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


def _links_supported(directory):
    """Умеет ли файловая система жёсткие ссылки."""
    source = Path(directory) / "link-source"
    source.write_bytes(b"data")
    try:
        os.link(source, Path(directory) / "link-target")
    except OSError:
        return False
    return True


class TerrainCacheTest(unittest.TestCase):
    def _config(self, directory):
        config = Config(qgis_projects_dir=directory)
        config.ensure_directories()
        return config

    def test_dem_is_restored_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            names = list(terrain_cache.DEM_FILES)
            for name in names:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            self.assertTrue(terrain_cache.store_dem(config, key))
            for name in names:
                (config.raster_dir / name).unlink()
            self.assertTrue(terrain_cache.restore_dem(config, key))
            for name in names:
                self.assertTrue((config.raster_dir / name).exists(), name)

    def test_relief_is_restored_without_touching_raw_dem(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).write_bytes(b"raw")
            dem_key = terrain_cache.dem_signature(config, bbox)
            self.assertTrue(terrain_cache.store_dem(config, dem_key))

            _write_layer(
                config.vector_dir / "water_dissolved.sqlite",
                "water_dissolved", [(43.05, 42.05)],
            )
            relief_key = terrain_cache.relief_signature(config, bbox, dem_key)
            for name in terrain_cache.RELIEF_FILES:
                # В terrain.py маска пишет новый файл и подменяет им
                # старый; прямая запись изменила бы кэш сырого DEM.
                (config.raster_dir / name).unlink(missing_ok=True)
                (config.raster_dir / name).write_bytes(b"masked")
            self.assertTrue(terrain_cache.store_relief(config, relief_key))
            for name in terrain_cache.RELIEF_FILES:
                (config.raster_dir / name).unlink()

            self.assertTrue(terrain_cache.restore_dem(config, dem_key))
            self.assertTrue(
                terrain_cache.restore_relief(config, relief_key)
            )
            for name in terrain_cache.RELIEF_FILES:
                self.assertEqual(
                    (config.raster_dir / name).read_bytes(), b"masked", name,
                )
            # Маскированный DEM заменил файл в проекте, но не в кэше
            # сырого рельефа: иначе следующий прогон начал бы с маски.
            raw_cache = terrain_cache._group_dir(config, "dem", dem_key)
            self.assertEqual(
                (raw_cache / "merged_dem.tif").read_bytes(), b"raw",
            )

            (config.vector_dir / "water_dissolved.sqlite").unlink()
            _write_layer(
                config.vector_dir / "water_dissolved.sqlite",
                "water_dissolved", [(43.06, 42.06)],
            )
            self.assertNotEqual(
                relief_key,
                terrain_cache.relief_signature(config, bbox, dem_key),
            )

    def test_dem_cached_checks_files_without_restoring(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            self.assertFalse(terrain_cache.dem_cached(config, bbox))
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, bbox)
            self.assertTrue(terrain_cache.store_dem(config, key))
            terrain_cache.store_dem_hint(
                config, terrain_cache.dem_hint(config, bbox), key
            )
            self.assertTrue(terrain_cache.dem_cached(config, bbox))
            # Неполная запись кэша не должна считаться готовой.
            required = terrain_cache.dem_required(config)[0]
            terrain_cache._group_dir(config, "dem", key).joinpath(
                required
            ).unlink()
            self.assertFalse(terrain_cache.dem_cached(config, bbox))

    def test_restored_rasters_are_links_not_copies(self):
        """Крупные растры возвращаются ссылкой: копия гигабайтов стоит
        больше 20 секунд.

        Перед записью terrain.py удаляет прежний файл, поэтому ссылка
        не даёт перезаписать кэш на месте.
        """
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            names = list(terrain_cache.DEM_FILES)
            for name in names:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            terrain_cache.store_dem(config, key)
            for name in names:
                (config.raster_dir / name).unlink()
            self.assertTrue(terrain_cache.restore_dem(config, key))
            cache_dir = terrain_cache._group_dir(config, "dem", key)
            for name in names:
                restored = config.raster_dir / name
                self.assertTrue(restored.exists(), name)
                self.assertEqual(restored.read_bytes(), b"raster", name)
                if _links_supported(directory):
                    self.assertTrue(
                        os.path.samefile(restored, cache_dir / name), name,
                    )

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

    def test_dem_signature_depends_on_land_tiles(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            first = terrain_cache.dem_signature(
                config, bbox, required_tiles={(42, 43)},
            )
            second = terrain_cache.dem_signature(
                config, bbox, required_tiles={(42, 44)},
            )
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

    def test_layer_digest_is_stable(self):
        """Отпечаток слоя не меняется: от него зависят ключи кэшей.

        Поля читаются по индексу, а не по имени (поиск имени на каждом
        объекте стоил почти всё время отпечатка), но значение отпечатка
        остаётся прежним — иначе у пользователей пропадут кэши DEM,
        изолиний и линий подписи.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layer.sqlite"
            _write_layer(path, "layer", [(1.0, 2.0), (3.0, 4.0)])
            self.assertEqual(
                terrain_cache.layer_digest(path),
                "6f80610d91885cfb9a102bed5eb30c0206e37d8e",
            )

    def test_isolines_signature_depends_on_step(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            first = terrain_cache.isolines_signature(config, bbox, key)
            config.isolines_step = 25
            self.assertNotEqual(first, terrain_cache.isolines_signature(config, bbox, key))

    def test_isolines_signature_depends_on_units(self):
        """Метры и футы не переиспользуют один и тот же кэш."""
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            first = terrain_cache.isolines_signature(config, bbox, key)
            config.isolines_units = "ft"
            self.assertNotEqual(
                first, terrain_cache.isolines_signature(config, bbox, key)
            )

    def test_isolines_are_restored_from_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            bbox = BBox(43.0, 42.0, 43.1, 42.1)
            key = terrain_cache.dem_signature(config, bbox)
            isolines_key = terrain_cache.isolines_signature(config, bbox, key)
            for name in terrain_cache.ISOLINES_FILES:
                (config.vector_dir / name).write_bytes(name.encode())
            self.assertTrue(terrain_cache.store_isolines(config, isolines_key))
            for name in terrain_cache.ISOLINES_FILES:
                (config.vector_dir / name).unlink()
            self.assertTrue(terrain_cache.restore_isolines(config, isolines_key))
            for name in terrain_cache.ISOLINES_FILES:
                self.assertTrue((config.vector_dir / name).exists(), name)

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

    def test_restore_updates_the_time_of_the_entry(self):
        """Часто используемая запись не должна вытесняться первой."""
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).write_bytes(b"raster")
            key = terrain_cache.dem_signature(config, BBox(43.0, 42.0, 43.1, 42.1))
            terrain_cache.store_dem(config, key)
            entry = terrain_cache._group_dir(config, "dem", key)
            # Делаем вид, что запись давно не использовалась.
            stale = entry.stat().st_mtime_ns - 10 ** 9
            os.utime(entry, (stale, stale))
            for name in terrain_cache.DEM_FILES:
                (config.raster_dir / name).unlink()
            self.assertTrue(terrain_cache.restore_dem(config, key))
            self.assertGreater(entry.stat().st_mtime_ns, stale)

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
