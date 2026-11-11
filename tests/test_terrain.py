import os
import shutil
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

    def test_single_dem_tile_is_not_merged(self):
        from unittest import mock

        from qgis_xtopo.core import terrain
        from qgis_xtopo.core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory)
            config.ensure_directories()
            tile = config.terrain_input_dir / "N42E044.hgt"
            tile.write_bytes(b"dem")
            config.project_dir.joinpath("crop.geojson").write_text(
                "{}", encoding="utf-8"
            )
            gdal = mock.Mock()
            gdal.WarpOptions.side_effect = lambda **options: options

            def warp(output, *args, **kwargs):
                Path(output).write_bytes(b"raster")
                return mock.Mock()

            gdal.Warp.side_effect = warp
            messages = []
            with mock.patch.object(
                terrain, "_gdal", return_value=(gdal, None, None)
            ):
                terrain.build_dem(config, BBox(44.7, 42.7, 44.8, 42.8), messages.append)
            gdal.BuildVRT.assert_not_called()
            self.assertEqual(gdal.Warp.call_args_list[0].args[1], str(tile))
            self.assertFalse(any("Merging" in message for message in messages))

    def test_reprojected_dem_uses_content_cache(self):
        from unittest import mock

        from osgeo import gdal, osr

        from qgis_xtopo.core import terrain
        from qgis_xtopo.core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory)
            config.ensure_directories()
            tile = config.terrain_input_dir / "N42E044.tif"
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(tile), 20, 20, 1, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((44.7, 0.001, 0, 42.8, 0, -0.001))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            dataset.GetRasterBand(1).Fill(1000)
            dataset = None
            config.project_dir.joinpath("crop.geojson").write_text(
                '{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":{"type":"Polygon","coordinates":'
                '[[[44.7,42.78],[44.72,42.78],[44.72,42.8],'
                '[44.7,42.8],[44.7,42.78]]]}}]}',
                encoding="utf-8",
            )
            bbox = BBox(44.7, 42.78, 44.72, 42.8)
            messages = []
            with mock.patch.object(gdal, "Warp", wraps=gdal.Warp) as warp:
                terrain.build_dem(config, bbox, messages.append)
                self.assertEqual(warp.call_count, 2)
                terrain.build_dem(config, bbox, messages.append)
                self.assertEqual(warp.call_count, 3)
                self.assertIn(
                    "Using cached reprojected DEM (EPSG:3857)", messages
                )

                dataset = gdal.Open(str(tile), gdal.GA_Update)
                dataset.GetRasterBand(1).Fill(2000)
                dataset = None
                terrain.build_dem(config, bbox, messages.append)
                self.assertEqual(warp.call_count, 5)

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


    def test_contours_are_cut_by_intermittent_water(self):
        """По пересыхающей воде (intermittent=yes) изолинии тоже режутся.

        Без этого срезания контуры шли по площади сухих озёр — например,
        по relation/3969167 (Chott el Djerid).
        """
        import numpy
        from osgeo import gdal, ogr, osr

        ogr.RegisterAll()
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.terrain import generate_isolines

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(qgis_projects_dir=str(root / "projects"))
            config.ensure_directories()
            config.isolines_step = 10

            # Пересыхающее озеро: полоса поперёк склона.
            water_path = config.vector_dir / "water_intermittent.sqlite"
            dataset = ogr.GetDriverByName("SQLite").CreateDataSource(str(water_path))
            layer = dataset.CreateLayer("water_intermittent", srs, ogr.wkbPolygon)
            ring = ogr.Geometry(ogr.wkbLinearRing)
            for x, y in ((0.55, 0.0), (1.45, 0.0), (1.45, 4.0), (0.55, 4.0), (0.55, 0.0)):
                ring.AddPoint(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetGeometry(polygon)
            layer.CreateFeature(feature)
            dataset = None

            # Склон: высоты растут с запада на восток.
            size = 40
            array = numpy.zeros((size, size), dtype="float32")
            for column in range(size):
                array[:, column] = column * 10.0 + 3.0
            raster = gdal.GetDriverByName("GTiff").Create(
                str(config.raster_dir / "merged_dem.tif"), size, size, 1, gdal.GDT_Float32
            )
            raster.SetGeoTransform((0.0, 0.1, 0.0, 4.0, 0.0, -0.1))
            raster.SetProjection(srs.ExportToWkt())
            raster.GetRasterBand(1).WriteArray(array)
            raster.FlushCache()
            raster = None

            generate_isolines(config, BBox(0.0, 0.0, 4.0, 4.0))

            result = ogr.Open(str(config.vector_dir / "isolines_full.sqlite"))
            contours = [
                feature.GetGeometryRef().Clone() for feature in result.GetLayer(0)
            ]
            result = None
            self.assertTrue(contours)
            water = ogr.CreateGeometryFromWkt(
                "POLYGON ((0.55 0, 1.45 0, 1.45 4, 0.55 4, 0.55 0))"
            )
            for contour in contours:
                self.assertFalse(contour.Intersects(water))

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

            # Обычные изолинии на площади ледника убираются: там рисуются
            # ледниковые (синие).
            regular_path = config.vector_dir / "isolines_regular.sqlite"
            self.assertTrue(regular_path.exists())
            regular_dataset = ogr.Open(str(regular_path))
            regular = regular_dataset.GetLayer(0)
            regular_features = list(regular)
            # Контур 100 м: две части вне ледника; контур 300 м: целиком.
            self.assertEqual(len(regular_features), 3)
            lengths = sorted(
                feature.GetGeometryRef().Length() for feature in regular_features
            )
            self.assertAlmostEqual(lengths[0], 1.0, places=6)
            self.assertAlmostEqual(lengths[1], 1.0, places=6)
            self.assertAlmostEqual(lengths[2], 2 ** 0.5, places=6)
            regular_dataset = None

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


class GlacierIsolinesWaterTest(unittest.TestCase):
    """Изолинии ледников прерываются на воде, как и обычные.

    Синтетический склон: высоты растут с запада на восток, поэтому
    изолинии идут полосами с запада на восток.  Ледник занимает часть
    склона, вода — поперечную полосу, пересекающую и ледник, и обычные
    изолинии.
    """

    # Границы полос в координатах DEM.
    GLACIER = (1.0, 2.0)
    WATER = (1.2, 1.6)

    def _srs(self):
        from osgeo import osr

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs

    def _polygon(self, srs, x_from, x_to):
        """Полоса во всю высоту растра."""
        from osgeo import ogr

        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in (
            (x_from, 0.0), (x_to, 0.0), (x_to, 4.0), (x_from, 4.0), (x_from, 0.0)
        ):
            ring.AddPoint(x, y)
        polygon = ogr.Geometry(ogr.wkbPolygon)
        polygon.AddGeometry(ring)
        return polygon

    def _write_layer(self, path, srs, geometry):
        """Слой с одним полигоном."""
        from osgeo import ogr

        dataset = ogr.GetDriverByName("SQLite").CreateDataSource(str(path))
        layer = dataset.CreateLayer(path.stem, srs, ogr.wkbPolygon)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(geometry)
        layer.CreateFeature(feature)
        dataset = None

    def _write_dem(self, config, srs):
        """Склон: высоты растут с запада на восток."""
        import numpy
        from osgeo import gdal

        size = 40
        array = numpy.zeros((size, size), dtype="float32")
        for column in range(size):
            array[:, column] = column * 10.0 + 3.0
        raster = gdal.GetDriverByName("GTiff").Create(
            str(config.raster_dir / "merged_dem.tif"), size, size, 1, gdal.GDT_Float32
        )
        raster.SetGeoTransform((0.0, 0.1, 0.0, 4.0, 0.0, -0.1))
        raster.SetProjection(srs.ExportToWkt())
        raster.GetRasterBand(1).WriteArray(array)
        raster.FlushCache()
        raster = None

    def _prepare(self, directory, water_name=None):
        """Проект со склоном, ледником и (по желанию) водой."""
        from qgis_xtopo.core.config import Config

        config = Config(qgis_projects_dir=str(Path(directory) / "projects"))
        config.ensure_directories()
        config.isolines_step = 10
        srs = self._srs()
        self._write_dem(config, srs)
        self._write_layer(
            config.vector_dir / "glacier.sqlite",
            srs,
            self._polygon(srs, *self.GLACIER),
        )
        if water_name:
            self._write_layer(
                config.vector_dir / water_name,
                srs,
                self._polygon(srs, *self.WATER),
            )
        return config, srs

    def _isolines(self, config, name):
        """Линии слоя изолиний."""
        from osgeo import ogr

        dataset = ogr.Open(str(config.vector_dir / name))
        layer = dataset.GetLayer(0)
        lines = [
            feature.GetGeometryRef().Clone()
            for feature in layer
            if feature.GetGeometryRef() is not None
            and not feature.GetGeometryRef().IsEmpty()
        ]
        dataset = None
        return lines

    def _length_inside(self, line, polygon):
        """Длина части линии внутри полигона."""
        outside = line.Difference(polygon)
        return line.Length() - (outside.Length() if outside is not None else 0.0)

    def _assert_cut_by_water(self, water_name):
        from qgis_xtopo.core.terrain import generate_isolines

        with tempfile.TemporaryDirectory() as directory:
            config, srs = self._prepare(directory, water_name)
            generate_isolines(config, BBox(0.0, 0.0, 4.0, 4.0))

            water = self._polygon(srs, *self.WATER)
            for name in ("isolines_glacier.sqlite", "isolines_regular.sqlite"):
                lines = self._isolines(config, name)
                self.assertTrue(lines, f"пустой слой {name}")
                for line in lines:
                    self.assertLessEqual(
                        self._length_inside(line, water),
                        1e-9,
                        f"{name}: изолиния заходит на воду",
                    )

    def test_glacier_isolines_are_cut_by_water(self):
        """Постоянная вода режет и ледниковые изолинии."""
        self._assert_cut_by_water("water.sqlite")

    def test_glacier_isolines_are_cut_by_intermittent_water(self):
        """Пересыхающая вода режет и ледниковые изолинии."""
        self._assert_cut_by_water("water_intermittent.sqlite")

    def test_without_water_glacier_isolines_cross_the_band(self):
        """Контроль: без воды изолинии ледников шли бы через полосу.

        Показывает, что проверка выше не пустая: режет изолинии именно
        маска воды.
        """
        from qgis_xtopo.core.terrain import generate_isolines

        with tempfile.TemporaryDirectory() as directory:
            config, srs = self._prepare(directory)
            generate_isolines(config, BBox(0.0, 0.0, 4.0, 4.0))

            water = self._polygon(srs, *self.WATER)
            lines = self._isolines(config, "isolines_glacier.sqlite")
            self.assertTrue(lines)
            inside = sum(self._length_inside(line, water) for line in lines)
            self.assertGreater(inside, 0.1)


class DemSourceTest(unittest.TestCase):
    def test_tiles_do_not_include_upper_boundary(self):
        from qgis_xtopo.core import dem_sources

        self.assertEqual(
            dem_sources.tile_names(BBox(7.0, 45.9, 7.2, 46.0)),
            ["N45E007"],
        )
        self.assertEqual(
            dem_sources.tile_names(BBox(7.0, 46.0, 7.2, 46.1)),
            ["N46E007"],
        )

    def test_range_source_outside_coverage(self):
        from qgis_xtopo.core import dem_sources

        state = dem_sources._range_availability(
            dem_sources.source("srtm"), BBox(15.0, 78.0, 16.0, 79.0)
        )
        self.assertFalse(state.available)
        self.assertIn("вне покрытия", state.message)

    def test_stac_source_without_items(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        with mock.patch.object(dem_sources, "stac_items", return_value=[]):
            state = dem_sources._stac_availability(
                dem_sources.source("3dep"), BBox(7.0, 46.0, 8.0, 47.0),
                timeout=1,
            )
        self.assertFalse(state.available)
        self.assertEqual(state.total, 1)

    def test_stac_source_partial_coverage(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        item = {"bbox": [7.0, 46.0, 8.0, 47.0], "assets": {}}
        with mock.patch.object(dem_sources, "stac_items", return_value=[item]):
            state = dem_sources._stac_availability(
                dem_sources.source("cop30"),
                BBox(7.0, 45.9, 8.0, 46.9), timeout=1,
            )
        self.assertTrue(state.available)
        self.assertTrue(state.partial)
        self.assertEqual(state.total, 2)
        self.assertEqual(state.missing, 1)

    def test_stac_source_full_coverage(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        items = [
            {"bbox": [7.0, 46.0, 8.0, 47.0], "assets": {}},
            {"bbox": [7.0, 45.0, 8.0, 46.0], "assets": {}},
        ]
        with mock.patch.object(dem_sources, "stac_items", return_value=items):
            state = dem_sources._stac_availability(
                dem_sources.source("cop30"),
                BBox(7.0, 45.9, 8.0, 46.9), timeout=1,
            )
        self.assertTrue(state.available)
        self.assertFalse(state.partial)
        self.assertEqual(state.missing, 0)
        self.assertEqual(
            state.covered_tiles, frozenset({(45, 7), (46, 7)})
        )
        self.assertFalse(state.missing_tiles)

    def test_stac_source_keeps_missing_tiles(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        item = {"bbox": [7.0, 46.0, 8.0, 47.0], "assets": {}}
        with mock.patch.object(dem_sources, "stac_items", return_value=[item]):
            state = dem_sources._stac_availability(
                dem_sources.source("cop30"),
                BBox(7.0, 45.9, 8.0, 46.9), timeout=1,
            )
        self.assertEqual(state.covered_tiles, frozenset({(46, 7)}))
        self.assertEqual(state.missing_tiles, frozenset({(45, 7)}))

    def test_preferred_source_ignores_ocean_tiles(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(-76.2538, 35.8625, -63.1295, 36.6597)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        land = frozenset({(35, -77), (35, -76), (36, -77), (36, -76)})
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
                missing_tiles=required - land,
            ),
            "alos": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
        }
        self.assertEqual(
            dem_sources.preferred_source(bbox, availability), "3dep"
        )
        shown = dem_sources.display_availability(
            "3dep", bbox, availability
        )
        self.assertFalse(shown.partial)
        self.assertIn("океан", shown.message)

    def test_preferred_source_ignores_ocean_tile_with_land_mask(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        bbox = BBox(-80.2101, 26.07, -79.1259, 26.6067)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        land = frozenset({(26, -81)})
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required,
                covered_tiles=land,
                missing_tiles=required - land,
            ),
            "alos": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        with mock.patch.object(
            dem_sources, "_world_map_land_tiles", return_value=land
        ):
            self.assertEqual(
                dem_sources.preferred_source(bbox, availability), "3dep"
            )
            shown = dem_sources.display_availability(
                "3dep", bbox, availability
            )
        self.assertIn("океан", shown.message)

    def test_world_map_land_mask_skips_ocean(self):
        from qgis_xtopo.core import dem_sources

        if dem_sources._world_map_path() is None:
            self.skipTest("QGIS world map is not available")
        bbox = BBox(-80.2101, 26.07, -79.1259, 26.6067)
        self.assertEqual(
            dem_sources._world_map_land_tiles(bbox),
            frozenset({(26, -81)}),
        )

    def test_preferred_source_requires_land_coverage(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(-76.2538, 35.8625, -63.1295, 36.6597)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        land = frozenset({(35, -77), (35, -76), (36, -77), (36, -76)})
        covered = land - {(36, -77)}
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=covered,
                missing_tiles=required - covered,
            ),
            "alos": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
        }
        self.assertIsNone(dem_sources.preferred_source(bbox, availability))

    def test_preferred_source_without_land_mask_is_strict(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 45.9, 8.0, 46.9)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        partial = dem_sources.Availability(
            available=True, checked=True,
            required_tiles=required,
            covered_tiles=frozenset({(45, 7)}),
            missing_tiles=frozenset({(46, 7)}),
        )
        self.assertIsNone(
            dem_sources.preferred_source(bbox, {"3dep": partial})
        )
        full = dem_sources.Availability(
            available=True, checked=True,
            required_tiles=required, covered_tiles=required,
            message="весь охват",
        )
        self.assertEqual(
            dem_sources.preferred_source(bbox, {"3dep": full}), "3dep"
        )
        shown = dem_sources.display_availability(
            "3dep", bbox, {"3dep": full}
        )
        self.assertEqual(shown.message, "весь охват")

    def test_source_warning_only_for_other_sources(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 45.9, 8.0, 46.9)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
            "alos": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        self.assertIn(
            "3DEP", dem_sources.source_warning("srtm", bbox, availability)
        )
        self.assertEqual(
            dem_sources.source_warning("3dep", bbox, availability), ""
        )
        self.assertEqual(
            dem_sources.source_warning(
                dem_sources.MANUAL_SOURCE, bbox, availability
            ),
            "",
        )
        self.assertEqual(dem_sources.source_warning("srtm", bbox, {}), "")

    def test_auto_plan_prefers_better_datasets(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
            "srtm": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        plan = dem_sources.auto_plan(bbox, availability, (46, 7))
        self.assertEqual(plan, {(46, 7): "3dep"})

    def test_auto_plan_falls_back_to_background(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        plan = dem_sources.auto_plan(bbox, availability, ())
        self.assertEqual(plan, {(46, 7): "cop30"})

    def test_auto_plan_sonny_wins_over_background(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        plan = dem_sources.auto_plan(bbox, availability, ((46, 7),))
        self.assertEqual(plan, {(46, 7): "sonny"})

    def test_auto_plan_priority_can_be_changed(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "3dep": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        plan = dem_sources.auto_plan(
            bbox, availability, ((46, 7),),
            priority=(dem_sources.SONNY_SOURCE, "cop30"),
        )
        self.assertEqual(plan, {(46, 7): "sonny"})

    def test_auto_availability_counts_datasets(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        state = dem_sources.auto_availability(bbox, availability, ())
        self.assertTrue(state.available)
        self.assertIn("Copernicus GLO-30", state.message)

    def test_mixed_datum_warning_for_mosaic(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 45.9, 8.0, 46.9)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        availability = {
            "cop30": dem_sources.Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=required,
            ),
        }
        warning = dem_sources.source_warning(
            dem_sources.AUTO_SOURCE, bbox, availability, ((46, 7),)
        )
        self.assertIn("EGM2008", warning)
        self.assertEqual(
            dem_sources.source_warning(
                dem_sources.AUTO_SOURCE, bbox, availability, ()
            ),
            "",
        )

    def test_sonny_availability_from_catalogue(self):
        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        state = dem_sources.sonny_availability(bbox, ((46, 7),))
        self.assertTrue(state.available)
        self.assertFalse(state.partial)
        self.assertFalse(
            dem_sources.sonny_availability(bbox, ()).available
        )

    def test_attribution_lists_mosaic_members(self):
        from qgis_xtopo.core import dem_sources

        attribution = dem_sources.attribution_for(dem_sources.AUTO_SOURCE)
        self.assertIn("Copernicus", attribution)
        self.assertIn("Sonny", attribution)
        keys = dem_sources.attribution_for_keys(["sonny"])
        self.assertEqual(keys, dem_sources.attribution_for("sonny"))

    def test_check_availability_adds_auto_mode(self):
        from unittest import mock

        from qgis_xtopo.core import dem_sources

        bbox = BBox(7.0, 46.0, 8.0, 47.0)
        required = frozenset(dem_sources.tiles_for_bbox(bbox))
        full = dem_sources.Availability(
            available=True, checked=True,
            required_tiles=required, covered_tiles=required,
        )

        def fake_stac(item, bbox, timeout=30, retries=2):
            return full

        with mock.patch.object(dem_sources, "_stac_availability", fake_stac):
            availability = dem_sources.check_availability(
                bbox, timeout=1, sonny_tiles=()
            )
        self.assertIn(dem_sources.AUTO_SOURCE, availability)
        self.assertTrue(availability[dem_sources.AUTO_SOURCE].available)
        # 3DEP имеет приоритет над остальными наборами.
        self.assertIn("3DEP", availability[dem_sources.AUTO_SOURCE].message)


class SonnyDatasetTest(unittest.TestCase):
    def test_tile_names(self):
        from qgis_xtopo.core import sonny

        self.assertEqual(sonny.tile_from_name("N46E009.zip"), (46, 9))
        self.assertEqual(sonny.tile_from_name("N27W016.hgt"), (27, -16))
        self.assertEqual(sonny.tile_from_name("S21W030.zip"), (-21, -30))
        self.assertIsNone(sonny.tile_from_name("readme.txt"))
        self.assertEqual(sonny.tile_name(46, 9), "N46E009")
        self.assertEqual(sonny.tile_name(-21, -30), "S21W030")

    def test_catalogue_picks_finest_dataset(self):
        from qgis_xtopo.core import sonny

        catalogue = {
            "europe-1s": {"N46E009.zip": "id1", "N47E010.zip": "id4"},
            "at-05s": {"N46E009.zip": "id05"},
        }
        with tempfile.TemporaryDirectory() as directory:
            sonny.store_catalogue(catalogue, directory)
            tiles = sonny.catalogue_tiles(directory)
            assignment = sonny.assign_datasets(catalogue, tiles)
        self.assertEqual(tiles[(46, 9)], "at-05s")
        self.assertEqual(tiles[(47, 10)], "europe-1s")
        self.assertEqual(assignment[(46, 9)], "at-05s")

    def test_catalogue_skips_unknown_datasets(self):
        from qgis_xtopo.core import sonny

        catalogue = {
            "europe-1s": {"N46E009.zip": "id1"},
            "europe-3s": {"N47E010.zip": "id3"},
        }
        with tempfile.TemporaryDirectory() as directory:
            sonny.store_catalogue(catalogue, directory)
            tiles = sonny.catalogue_tiles(directory)
        self.assertEqual(tiles, {(46, 9): "europe-1s"})

    def test_catalogue_age_and_missing_cache(self):
        from qgis_xtopo.core import sonny

        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(sonny.catalogue_age_days(directory))
            sonny.store_catalogue({"europe-1s": {}}, directory)
            age = sonny.catalogue_age_days(directory)
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    def test_hgt_vrt_matches_srtm_georeferencing(self):
        import numpy
        from osgeo import gdal

        from qgis_xtopo.core import sonny

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hgt = root / "N47E011.hgt"
            values = numpy.arange(
                7201 * 7201, dtype=">i2"
            ).reshape(7201, 7201)
            values.tofile(str(hgt))
            vrt = sonny.hgt_vrt(hgt, 0.5 / 3600.0, 7201)
            dataset = gdal.Open(str(vrt))
            transform = dataset.GetGeoTransform()
            array = dataset.GetRasterBand(1).ReadAsArray()
            size = dataset.RasterXSize
            dataset = None
        step = 0.5 / 3600.0
        self.assertEqual(size, 7201)
        self.assertAlmostEqual(transform[0], 11.0 - step / 2.0, 9)
        self.assertAlmostEqual(transform[3], 48.0 + step / 2.0, 9)
        self.assertAlmostEqual(transform[1], step, 12)
        self.assertEqual(array[0, 0], 0)
        self.assertEqual(array[1, 2], 7201 + 2)


class AutoMosaicTest(unittest.TestCase):
    def test_mosaic_grid_prefers_finest_that_fits(self):
        from qgis_xtopo.core import terrain

        resolution, width, height = terrain._mosaic_grid(
            1.0, 1.0, [1 / 3600.0, 1 / 10800.0]
        )
        self.assertAlmostEqual(resolution, 1 / 10800.0, 12)
        self.assertEqual(width, 10800)
        self.assertEqual(height, 10800)

    def test_mosaic_grid_reduces_large_extent(self):
        from qgis_xtopo.core import terrain

        resolution, width, height = terrain._mosaic_grid(
            10.0, 10.0, [1 / 3600.0, 1 / 10800.0]
        )
        self.assertLessEqual(width * height, terrain.MAX_DEM_PIXELS)
        self.assertGreater(resolution, 1 / 3600.0)

    def test_cells_window_covers_tiles(self):
        from qgis_xtopo.core import terrain

        window = terrain._cells_window({(46, 7), (46, 8), (47, 7)})
        self.assertEqual(window.lon_min, 7.0)
        self.assertEqual(window.lon_max, 9.0)
        self.assertEqual(window.lat_min, 46.0)
        self.assertEqual(window.lat_max, 48.0)

    def test_mosaic_layers_keep_priority(self):
        """Данные более позднего слоя перекрывают более ранний."""
        import numpy
        from osgeo import gdal, osr

        from qgis_xtopo.core import terrain

        def make(path, value, nodata=None):
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(path), 10, 10, 1, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((7.0, 0.01, 0, 47.0, 0, -0.01))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            band = dataset.GetRasterBand(1)
            if nodata is not None:
                band.SetNoDataValue(nodata)
            band.WriteArray(numpy.full((10, 10), value, dtype="float32"))
            dataset = None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.tif"
            overlay = root / "overlay.tif"
            make(background, 100.0)
            make(overlay, 250.0)
            bounds = terrain.BBox(7.0, 46.9, 7.1, 47.0)
            gdal, _o, _s = terrain._gdal()
            merged = terrain._write_layer(
                gdal, [str(background), str(overlay)], bounds, 10, 10,
                root, "merged.tif", None, None, "тест",
            )
            dataset = gdal.Open(str(merged))
            array = dataset.GetRasterBand(1).ReadAsArray()
            dataset = None
        self.assertEqual(array[0, 0], 250.0)

    def test_mosaic_layer_keeps_background_where_overlay_is_empty(self):
        """Пустота в верхнем слое не затирает данные нижнего."""
        import numpy
        from osgeo import gdal, osr

        from qgis_xtopo.core import terrain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.tif"
            overlay = root / "overlay.tif"

            def make(path, array, nodata=None):
                dataset = gdal.GetDriverByName("GTiff").Create(
                    str(path), 10, 10, 1, gdal.GDT_Float32
                )
                dataset.SetGeoTransform((7.0, 0.01, 0, 47.0, 0, -0.01))
                srs = osr.SpatialReference()
                srs.ImportFromEPSG(4326)
                dataset.SetProjection(srs.ExportToWkt())
                band = dataset.GetRasterBand(1)
                if nodata is not None:
                    band.SetNoDataValue(nodata)
                band.WriteArray(array)
                dataset = None

            make(background, numpy.full((10, 10), 100.0, dtype="float32"))
            empty = numpy.full((10, 10), -32768.0, dtype="float32")
            empty[:, 5:] = 250.0
            make(overlay, empty, nodata=-32768)
            bounds = terrain.BBox(7.0, 46.9, 7.1, 47.0)
            gdal, _o, _s = terrain._gdal()
            merged = terrain._write_layer(
                gdal, [str(background), str(overlay)], bounds, 10, 10,
                root, "merged.tif", None, None, "тест",
            )
            dataset = gdal.Open(str(merged))
            array = dataset.GetRasterBand(1).ReadAsArray()
            dataset = None
        self.assertEqual(array[0, 0], 100.0)
        self.assertEqual(array[0, 9], 250.0)


class CogDownloadTest(unittest.TestCase):
    def test_download_cog_dem_uses_cache(self):
        from unittest import mock

        import numpy
        from osgeo import gdal, osr

        from qgis_xtopo.core import terrain

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tif"
            size = 360
            array = numpy.fromfunction(
                lambda y, x: 1000 + x + y, (size, size), dtype="float32"
            )
            dataset = gdal.GetDriverByName("GTiff").Create(
                str(source), size, size, 1, gdal.GDT_Float32
            )
            dataset.SetGeoTransform((7.0, 1 / 360, 0, 47.0, 0, -1 / 360))
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            dataset.SetProjection(srs.ExportToWkt())
            dataset.GetRasterBand(1).WriteArray(array)
            dataset = None

            bbox = BBox(7.0, 46.0, 7.03, 46.03)
            cache = root / "cache"
            messages = []
            with mock.patch.object(
                terrain, "_cog_sources", return_value=[str(source)]
            ):
                first = terrain.download_cog_dem(
                    "cop30", bbox, root / "first",
                    progress=messages.append, cache_dir=cache,
                )
                second = terrain.download_cog_dem(
                    "cop30", bbox, root / "second",
                    progress=messages.append, cache_dir=cache,
                )
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(any("Использован кэш" in item for item in messages))
            self.assertTrue(list((cache / "cog" / "cop30").glob("*.tif")))

            dataset = gdal.Open(str(first))
            band = dataset.GetRasterBand(1)
            self.assertEqual(band.GetNoDataValue(), -32768)
            values = band.ReadAsArray()
            self.assertGreater(int(values.max()), 1000)
            dataset = None


class ContourTilingTest(unittest.TestCase):
    """Контуры по тайлам совпадают с расчётом по всему DEM."""

    @classmethod
    def setUpClass(cls):
        import numpy
        from osgeo import gdal, ogr, osr

        ogr.RegisterAll()
        cls.directory = tempfile.mkdtemp()
        cls.source = Path(cls.directory) / "dem.tif"
        raster = gdal.GetDriverByName("GTiff").Create(
            str(cls.source), 600, 600, 1, gdal.GDT_Float32)
        spatial_ref = osr.SpatialReference()
        spatial_ref.ImportFromEPSG(4326)
        raster.SetProjection(spatial_ref.ExportToWkt())
        raster.SetGeoTransform((0.0, 0.001, 0.0, 0.6, 0.0, -0.001))
        ys, xs = numpy.mgrid[0:600, 0:600]
        centre = 300.0
        distance = numpy.sqrt((xs - centre) ** 2 + (ys - centre) ** 2)
        raster.GetRasterBand(1).WriteArray(
            (1000.0 - distance * 2.0).astype("float32"))
        raster = None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.directory, ignore_errors=True)

    def _contours(self, side):
        """Контуры тайлов: уровни и суммарная длина."""
        from osgeo import gdal, ogr, osr

        from qgis_xtopo.core import terrain

        raster = gdal.Open(str(self.source))
        tiles = terrain._contour_tiles(raster, side, terrain.CONTOUR_HALO)
        raster = None
        levels = set()
        length = 0.0
        for index, tile in enumerate(tiles):
            dataset, _count = terrain._contour_tile(
                (str(self.source), tile, 50))
            layer = dataset.GetLayer(0)
            for feature in layer:
                levels.add(round(feature.GetField("ELEV"), 6))
                length += feature.GetGeometryRef().Length()
        return levels, length

    def _reference(self):
        """Контуры одним проходом по всему DEM."""
        from osgeo import gdal, ogr, osr

        raster = gdal.Open(str(self.source))
        band = raster.GetRasterBand(1)
        spatial_ref = osr.SpatialReference()
        spatial_ref.ImportFromWkt(raster.GetProjection())
        dataset = ogr.GetDriverByName("Memory").CreateDataSource("reference")
        layer = dataset.CreateLayer("isolines", spatial_ref, ogr.wkbLineString)
        layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
        layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
        gdal.ContourGenerate(band, 50.0, 0.0, [], 0, 0.0, layer, 0, 1)
        levels = set()
        length = 0.0
        for feature in layer:
            levels.add(round(feature.GetField("ELEV"), 6))
            length += feature.GetGeometryRef().Length()
        return levels, length

    def test_tiled_contours_match_single_pass(self):
        """Нарезка не меняет изолинии: те же уровни и та же длина."""
        reference_levels, reference_length = self._reference()
        for side in (2, 4):
            levels, length = self._contours(side)
            self.assertEqual(levels, reference_levels)
            self.assertAlmostEqual(length, reference_length, places=6)

    def test_tile_side_grows_with_raster(self):
        """Тайлов тем больше, чем больше растр."""
        from qgis_xtopo.core import terrain

        self.assertEqual(terrain._contour_tile_side(1_000_000), 1)
        self.assertGreaterEqual(terrain._contour_tile_side(8_000_000), 2)
        small = terrain._contour_tile_side(8_000_000)
        large = terrain._contour_tile_side(200_000_000)
        self.assertGreater(large, small)

    def test_workers_take_three_quarters_of_cores(self):
        """Потоков — три четверти ядер, но не больше числа тайлов."""
        from qgis_xtopo.core import terrain

        cores = os.cpu_count() or 2
        expected = max(1, int(cores * terrain.CONTOUR_TILE_WORKERS_RATIO))
        self.assertEqual(terrain._contour_workers(1000), expected)
        self.assertEqual(terrain._contour_workers(1), 1)

    def test_halo_covers_neighbouring_cells(self):
        """У тайла есть запас вокруг ядра: иначе линия у края рвётся."""
        from qgis_xtopo.core import terrain

        raster = None
        from osgeo import gdal

        raster = gdal.Open(str(self.source))
        tiles = terrain._contour_tiles(raster, 2, terrain.CONTOUR_HALO)
        raster = None
        self.assertEqual(len(tiles), 4)
        for tile in tiles:
            xoff, yoff, xsize, ysize = tile["core"]
            window_xoff, window_yoff, window_xsize, window_ysize = tile["window"]
            self.assertLessEqual(window_xoff, xoff)
            self.assertLessEqual(window_yoff, yoff)
            self.assertGreaterEqual(
                window_xoff + window_xsize, xoff + xsize)
            self.assertGreaterEqual(
                window_yoff + window_ysize, yoff + ysize)


if __name__ == "__main__":
    unittest.main()
