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


if __name__ == "__main__":
    unittest.main()
