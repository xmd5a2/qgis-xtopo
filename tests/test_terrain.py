import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.terrain import srtm_tile_names


class TerrainTest(unittest.TestCase):
    def test_tiles(self):
        names = srtm_tile_names(BBox(44.7, 42.7, 45.1, 42.9))
        self.assertEqual(names, ["N42E044", "N42E045"])

    def test_southern(self):
        names = srtm_tile_names(BBox(-1.05, -1.05, -1.01, -1.01))
        self.assertEqual(names, ["S02W002"])

    def test_download_srtm_uses_cache(self):
        from qgis_xtopo.core import terrain

        bbox = BBox(37.5, 55.7, 37.7, 55.8)
        tile = terrain.srtm_tile_names(bbox)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            destination = root / "input_terrain"
            cached = terrain._cached_tile(cache, tile)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"cached tile")
            messages = []
            terrain.download_srtm(
                bbox,
                destination,
                progress=messages.append,
                cache_dir=cache,
            )
            self.assertTrue((destination / f"{tile}.hgt").exists())
            self.assertEqual((destination / f"{tile}.hgt").read_bytes(), b"cached tile")
            self.assertTrue(any("cached" in item for item in messages))

    def test_cache_dir_is_shared_between_projects(self):
        from qgis_xtopo.core.config import Config

        config = Config(qgis_projects_dir="/tmp/projects")
        self.assertEqual(
            config.terrain_cache_dir,
            Path("/tmp/projects") / "qgisxtopo-cache" / "srtm",
        )

    def test_contours_are_cut_by_water(self):
        from osgeo import ogr, osr

        ogr.RegisterAll()
        from qgis_xtopo.core.terrain import _subtract_water_from_layer

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        with tempfile.TemporaryDirectory() as directory:
            water_path = Path(directory) / "water.geojson"
            water_dataset = ogr.GetDriverByName("GeoJSON").CreateDataSource(str(water_path))
            water_layer = water_dataset.CreateLayer("water", srs, ogr.wkbPolygon)
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)):
                ring.AddPoint(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature = ogr.Feature(water_layer.GetLayerDefn())
            feature.SetGeometry(polygon)
            water_layer.CreateFeature(feature)
            water_dataset = None

            target_srs = srs.Clone()
            target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dataset = ogr.GetDriverByName("Memory").CreateDataSource("contours")
            layer = dataset.CreateLayer("isolines", target_srs, ogr.wkbLineString)
            layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTInteger))
            for elevation, points in (
                (100, ((-1, 0.5), (2, 0.5))),
                (200, ((0.2, 0.2), (0.8, 0.8))),
                (300, ((2, 2), (3, 3))),
            ):
                line = ogr.Geometry(ogr.wkbLineString)
                for x, y in points:
                    line.AddPoint(x, y)
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetField("ELEV", elevation)
                feature.SetGeometry(line)
                layer.CreateFeature(feature)

            changed = _subtract_water_from_layer(layer, [water_path], target_srs)
            self.assertEqual(changed, 2)
            self.assertEqual(layer.GetFeatureCount(), 3)
            geometries = {}
            for feature in layer:
                geometries.setdefault(feature.GetField("ELEV"), []).append(
                    feature.GetGeometryRef().Clone()
                )
            self.assertNotIn(200, geometries)
            self.assertEqual(len(geometries[100]), 2)
            self.assertAlmostEqual(
                sum(geometry.Length() for geometry in geometries[100]), 2.0
            )
            self.assertAlmostEqual(geometries[300][0].Length(), 2 ** 0.5)


    def test_download_srtm_reports_speed(self):
        import gzip
        from unittest import mock

        from qgis_xtopo.core import terrain

        payload = gzip.compress(b"0" * 4096)

        class FakeResponse:
            def __init__(self, data):
                self._data = data
                self._position = 0

            def read(self, size):
                chunk = self._data[self._position:self._position + size]
                self._position += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                return False

        original = terrain._Speed
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                terrain.urllib.request, "urlopen",
                return_value=FakeResponse(payload),
            ):
                with mock.patch.object(
                    terrain, "_Speed", lambda: original(window=0.0)
                ):
                    terrain.download_srtm(
                        BBox(44.7, 42.7, 44.8, 42.8),
                        Path(directory),
                        status=messages.append,
                    )
            self.assertTrue((Path(directory) / "N42E044.hgt").exists())
        self.assertTrue(messages)
        self.assertIn("МБ/с", messages[-1])
        self.assertIn("Загрузка рельефа", messages[-1])

    def test_speed_meter_counts_bytes(self):
        import time as time_module

        from qgis_xtopo.core.terrain import _Speed

        meter = _Speed(window=0.0)
        self.assertTrue(meter.add(1048576))
        self.assertEqual(meter.total, 1048576)
        self.assertGreater(meter.speed, 0)
        meter2 = _Speed(window=60.0)
        self.assertFalse(meter2.add(1024))
        self.assertEqual(meter2.speed, 0.0)

    def test_glacier_isolines_are_clipped_by_glaciers(self):
        from osgeo import ogr, osr

        ogr.RegisterAll()
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.terrain import _generate_glacier_isolines

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()

            # Полигон ледника в отдельном слое
            glacier_path = config.vector_dir / "glacier.sqlite"
            driver = ogr.GetDriverByName("SQLite")
            glacier_dataset = driver.CreateDataSource(str(glacier_path))
            glacier_layer = glacier_dataset.CreateLayer("glacier", srs, ogr.wkbPolygon)
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)):
                ring.AddPoint(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature = ogr.Feature(glacier_layer.GetLayerDefn())
            feature.SetGeometry(polygon)
            glacier_layer.CreateFeature(feature)
            glacier_dataset = None

            # Линии контуров
            shape_path = config.temp_dir / "isolines_full.shp"
            shape_dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(
                str(shape_path)
            )
            layer = shape_dataset.CreateLayer("isolines", srs, ogr.wkbLineString)
            layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTInteger))
            for elevation, points in (
                (100, ((-1, 0.5), (2, 0.5))),
                (300, ((2, 2), (3, 3))),
            ):
                line = ogr.Geometry(ogr.wkbLineString)
                for x, y in points:
                    line.AddPoint(x, y)
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetField("ELEV", elevation)
                feature.SetGeometry(line)
                layer.CreateFeature(feature)
            shape_dataset = None

            messages = []
            result = _generate_glacier_isolines(
                config, shape_path, progress=messages.append
            )
            self.assertIsNotNone(result)
            self.assertTrue((config.vector_dir / "isolines_glacier.sqlite").exists())
            dataset = ogr.Open(str(result))
            clipped = dataset.GetLayer(0)
            self.assertEqual(clipped.GetFeatureCount(), 1)
            features = list(clipped)
            self.assertEqual(len(features), 1)
            self.assertEqual(features[0].GetField("ELEV"), 100)
            self.assertAlmostEqual(
                features[0].GetGeometryRef().Length(), 1.0, places=6
            )
            self.assertTrue(any("Glacier" in item for item in messages))

    def test_glacier_isolines_handle_multiline_intersection(self):
        """Контур, входящий в ледник дважды, даёт несколько отрезков.

        Пересечение возвращает MULTILINESTRING, части которого нельзя
        брать ссылкой на родителя: после его освобождения указатель
        становится недействительным и GDAL падает.
        """
        from osgeo import ogr, osr

        ogr.RegisterAll()
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.terrain import _generate_glacier_isolines

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()

            glacier_path = config.vector_dir / "glacier.sqlite"
            dataset = ogr.GetDriverByName("SQLite").CreateDataSource(str(glacier_path))
            layer = dataset.CreateLayer("glacier", srs, ogr.wkbPolygon)
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ((0, 0), (10, 0), (10, 10), (0, 10), (0, 0)):
                ring.AddPoint(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(polygon)
            layer.CreateFeature(feature)
            dataset = None

            shape_path = config.temp_dir / "isolines_full.shp"
            dataset = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(str(shape_path))
            layer = dataset.CreateLayer("isolines", srs, ogr.wkbLineString)
            layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTInteger))
            # Линия входит в ледник, выходит и входит снова.
            line = ogr.Geometry(ogr.wkbLineString)
            for x, y in ((-1, 2), (11, 2), (11, 8), (-1, 8)):
                line.AddPoint(x, y)
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("ELEV", 100)
            feature.SetGeometry(line)
            layer.CreateFeature(feature)
            dataset = None

            result = _generate_glacier_isolines(config, shape_path)
            self.assertIsNotNone(result)
            dataset = ogr.Open(str(result))
            clipped = dataset.GetLayer(0)
            features = list(clipped)
            self.assertEqual(len(features), 2)
            lengths = sorted(round(item.GetGeometryRef().Length(), 6) for item in features)
            self.assertEqual(lengths, [10.0, 10.0])
            for item in features:
                self.assertEqual(item.GetField("ELEV"), 100)

    def test_glacier_isolines_skipped_without_glacier_layer(self):
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.terrain import _generate_glacier_isolines

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()
            result = _generate_glacier_isolines(
                config, config.temp_dir / "missing.shp"
            )
            self.assertIsNone(result)
            self.assertFalse(
                (config.vector_dir / "isolines_glacier.sqlite").exists()
            )


if __name__ == "__main__":
    unittest.main()
