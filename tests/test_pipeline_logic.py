import json
import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config
from qgis_xtopo.core.pipeline import (
    Pipeline, PipelineError, _ProgressTracker, _clean_layers, progress_stages,
)


class _FakeRunner:
    def __init__(self, result):
        self.result = result

    def has_algorithm(self, algorithm):
        return True

    def run(self, algorithm, parameters):
        return self.result(parameters)


def _feature_collection(path):
    path = Path(path)
    path.write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"id": 1},
                    "geometry": {"type": "Point", "coordinates": [1, 2]},
                }
            ],
        }),
        encoding="utf-8",
    )
    return path


class PipelineLogicTest(unittest.TestCase):
    def test_lowercase_algorithm_output(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="logic",
                bbox="1,2,3,4",
                array_queries=["water"],
            )
            config.ensure_directories()
            output = _feature_collection(config.temp_dir / "skeleton.geojson")
            runner = _FakeRunner(lambda parameters: {"output": str(output)})
            pipeline = Pipeline(config, BBox(1, 2, 3, 4), runner)
            result = pipeline._algorithm(
                "grass7:v.voronoi.skeleton",
                {"input": "source.geojson", "output": str(output)},
            )
        self.assertEqual(result, str(output))

    def test_empty_algorithm_output_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="logic",
                bbox="1,2,3,4",
                array_queries=["water"],
            )
            config.ensure_directories()
            output = config.temp_dir / "empty.geojson"
            output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
            runner = _FakeRunner(lambda parameters: {"OUTPUT": str(output)})
            pipeline = Pipeline(config, BBox(1, 2, 3, 4), runner)
            with self.assertRaises(PipelineError):
                pipeline._algorithm("native:test", {"INPUT": "source.geojson"})


class SplitLayerTest(unittest.TestCase):
    """split_layer раскладывает объекты по тегу layer без JSON."""

    def _source(self, config):
        path = config.temp_dir / "highway_main_source.geojson"
        path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"layer": value, "name": f"road {value}"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[1, 2], [3, 4]],
                        },
                    }
                    for value in ("0", "1", "-1", "2")
                ],
            }),
            encoding="utf-8",
        )
        return path

    def test_split_layer_writes_three_layers(self):
        from osgeo import ogr

        ogr.RegisterAll()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="logic",
                bbox="1,2,3,4",
                array_queries=["highway_main"],
            )
            config.ensure_directories()
            source = self._source(config)
            pipeline = Pipeline(
                config, BBox(1, 2, 3, 4), _FakeRunner(lambda parameters: {})
            )
            pipeline._split_layer("highway_main", source)
            expected = {
                "highway_main": 1,
                "highway_main_layer_1": 2,
                "highway_main_layer_-1": 1,
            }
            for name, count in expected.items():
                path = config.vector_dir / f"{name}.sqlite"
                self.assertTrue(path.exists(), name)
                dataset = ogr.Open(str(path))
                layer = dataset.GetLayer(0)
                self.assertEqual(layer.GetFeatureCount(), count, name)
                self.assertTrue(
                    layer.GetSpatialRef() is not None, name
                )
                dataset = None


class SaveVectorTest(unittest.TestCase):
    def test_write_is_not_slowed_down_by_skipfailures(self):
        from unittest import mock

        from osgeo import gdal, ogr

        from qgis_xtopo.core.pipeline import save_vector

        ogr.RegisterAll()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory, project_name="logic")
            config.ensure_directories()
            source = _feature_collection(config.temp_dir / "source.geojson")
            recorded = {}

            def fake_translate(destination, src, options=None):
                recorded["options"] = list(options or [])
                return object()

            with mock.patch.object(gdal, "VectorTranslate", fake_translate):
                save_vector(source, config.vector_dir / "water.sqlite", "water")
        self.assertNotIn("-skipfailures", recorded["options"])

    def _collection(self, directory, name, geometries):
        path = Path(directory) / name
        path.write_text(
            json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"id": index},
                        "geometry": geometry,
                    }
                    for index, geometry in enumerate(geometries, start=1)
                ],
            }),
            encoding="utf-8",
        )
        return path

    def test_mixed_geometry_is_not_flattened_to_single_type(self):
        """Полигоны relation не должны сплющиваться в линии way.

        Запросы way+relation дают GeoJSON с разнородными геометриями;
        заголовок такого слоя отражает тип лишь части объектов, и -nlt
        по нему превращал мультиполигоны в линии — слой переставал
        рисоваться (ледники на карте).
        """
        from qgis_xtopo.core.pipeline import _geometry_type

        polygon = {
            "type": "MultiPolygon",
            "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]],
        }
        line = {"type": "LineString", "coordinates": [[0, 0], [2, 2]]}
        with tempfile.TemporaryDirectory() as directory:
            mixed = self._collection(directory, "mixed.geojson", [line, polygon])
            self.assertEqual(_geometry_type(mixed), "GEOMETRY")
            polygons = self._collection(directory, "polygons.geojson", [polygon])
            self.assertEqual(_geometry_type(polygons), "MULTIPOLYGON")
            lines = self._collection(directory, "lines.geojson", [line])
            self.assertEqual(_geometry_type(lines), "LINESTRING")


class ProgressTrackerTest(unittest.TestCase):
    def test_progress_moves_through_stages(self):
        config = Config(
            project_name="progress",
            bbox="1,2,3,4",
            array_queries=["water", "forest"],
            source_mode="overpass",
        )
        values = []
        tracker = _ProgressTracker(
            progress_stages(config), values.append, queries=2
        )
        report = tracker.wrap(lambda message: None)
        for message in (
            "Проверка параметров",
            "Создание проекта QGIS",
            "Подготовка источника OSM",
            "water: downloaded",
            "Обработка слоёв",
            "[1/2] water",
            "[2/2] forest",
            "Удаление пустых слоёв",
        ):
            report(message)
        tracker.finish()
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 0)
        self.assertEqual(values[-1], 100)
        self.assertGreater(len(set(values)), 5)

    def test_download_and_pbf_progress_share_source_stage(self):
        config = Config(
            project_name="progress",
            bbox="1,2,3,4",
            array_queries=["water"],
        )
        values = []
        tracker = _ProgressTracker(progress_stages(config), values.append, queries=1)
        tracker.observe("Подготовка источника OSM")
        tracker.download_percent(50, "")
        after_download = values[-1]
        tracker.observe("PBF: processed 80000 objects (10%)")
        self.assertGreater(values[-1], after_download)
        tracker.finish()
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[-1], 100)

    def test_download_text_is_passed_to_status(self):
        config = Config(
            project_name="progress",
            bbox="1,2,3,4",
            array_queries=["water"],
        )
        messages = []
        tracker = _ProgressTracker(
            progress_stages(config), None, queries=1, status=messages.append
        )
        tracker.observe("Подготовка источника OSM")
        tracker.download_percent(10, "Загрузка карт: 10% · 0.5 МБ/с")
        self.assertEqual(messages[-1], "Загрузка карт: 10% · 0.5 МБ/с")

    def test_layer_fraction_stays_inside_stage(self):
        config = Config(
            project_name="progress",
            bbox="1,2,3,4",
            array_queries=["water", "forest"],
        )
        stages = progress_stages(config)
        low, high = None, None
        total = sum(weight for _, weight in stages)
        cursor = 0
        for name, weight in stages:
            if name == "Обработка слоёв":
                low = cursor / total * 100
                high = (cursor + weight) / total * 100
            cursor += weight
        values = []
        tracker = _ProgressTracker(stages, values.append, queries=2)
        tracker.observe("Обработка слоёв")
        tracker.fraction(0.5)
        self.assertGreater(values[-1], low)
        self.assertLess(values[-1], high)


class AreaRoadTest(unittest.TestCase):
    """Площадные дороги: замкнутый контур должен стать полигоном."""

    def setUp(self):
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        ogr.UseExceptions()
        # После завершения QGIS в предыдущих тестах драйверы сброшены.
        ogr.RegisterAll()
        self.ogr = ogr

    def _ring(self, closed=True):
        ogr = self.ogr
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in ((1, 1), (2, 1), (2, 2), (1, 2)):
            line.AddPoint_2D(x, y)
        if closed:
            line.AddPoint_2D(1, 1)
        return line

    def test_closed_line_becomes_polygon(self):
        from qgis_xtopo.core.pipeline import _polygon_from_geometry

        polygon = _polygon_from_geometry(self._ring())
        self.assertIsNotNone(polygon)
        self.assertEqual(polygon.GetGeometryName(), "POLYGON")
        self.assertAlmostEqual(polygon.Area(), 1.0)

    def test_open_line_stays_line(self):
        from qgis_xtopo.core.pipeline import _polygon_from_geometry

        self.assertIsNone(_polygon_from_geometry(self._ring(closed=False)))

    def test_polygon_untouched(self):
        from qgis_xtopo.core.pipeline import _polygon_from_geometry

        polygon = self.ogr.CreateGeometryFromWkt(
            "POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"
        )
        self.assertIsNone(_polygon_from_geometry(polygon))

    def test_area_layers_convert_lines(self):
        from qgis_xtopo.core.query_recipes import recipe_for

        for name in ("highway_main_area", "place_square_highway_area"):
            steps = [operation for operation, _ in recipe_for(name).steps]
            self.assertIn("linestopolygons", steps)
            self.assertEqual(steps[-1], "save")

    def test_glacier_recipe_keeps_only_polygons(self):
        """В слое ледников — только полигоны: разомкнутые way отбрасываются."""
        from qgis_xtopo.core.query_recipes import recipe_for

        steps = recipe_for("glacier").steps
        self.assertEqual(steps[0][0], "linestopolygons")
        self.assertTrue(steps[0][1].get("drop_others"))

    def test_drop_others_removes_non_polygonal_geometry(self):
        from osgeo import ogr

        from qgis_xtopo.core.pipeline import Pipeline

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="area",
                bbox="1,1,3,3",
                array_queries=["glacier"],
            )
            config.ensure_directories()
            source = config.temp_dir / "glacier.geojson"
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"id": "closed-way"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [
                                [1, 1], [2, 1], [2, 2], [1, 2], [1, 1],
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"id": "open-way"},
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[1, 1], [2, 2]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"id": "relation"},
                        "geometry": {
                            "type": "MultiPolygon",
                            "coordinates": [[[
                                [0, 0], [1, 0], [1, 1], [0, 1], [0, 0],
                            ]]],
                        },
                    },
                    {
                        "type": "Feature",
                        "properties": {"id": "node"},
                        "geometry": {"type": "Point", "coordinates": [1, 1]},
                    },
                ],
            }), encoding="utf-8")
            pipeline = Pipeline(config, BBox(1, 1, 3, 3), _FakeRunner(None))
            current = pipeline._step(
                "glacier", source, "linestopolygons", {"drop_others": True}
            )
            dataset = ogr.Open(str(current))
            layer = dataset.GetLayer(0)
            features = list(layer)
            self.assertEqual(len(features), 2)
            for feature in features:
                name = feature.GetGeometryRef().GetGeometryName()
                self.assertIn(name, ("POLYGON", "MULTIPOLYGON"), feature.GetField("id"))
            dataset = None

    def test_layer_recipe_saves_polygons(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="area",
                bbox="1,1,3,3",
                array_queries=["highway_main_area"],
            )
            config.ensure_directories()
            source = config.temp_dir / "source.geojson"
            source.write_text(json.dumps({
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"name": "Площадь"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [1, 1], [2, 1], [2, 2], [1, 2], [1, 1],
                        ],
                    },
                }],
            }), encoding="utf-8")
            pipeline = Pipeline(config, BBox(1, 1, 3, 3), _FakeRunner(None))
            current = pipeline._step(
                "highway_main_area", source, "linestopolygons", {},
            )
            pipeline._step("highway_main_area", current, "save", {
                "suffix": "", "layer_name": None, "target_crs": None,
            })
            path = config.vector_dir / "highway_main_area.sqlite"
            from osgeo import ogr

            ogr.UseExceptions()
            dataset = ogr.Open(str(path))
            layer = dataset.GetLayerByName("highway_main_area")
            self.assertIsNotNone(layer)
            self.assertEqual(layer.GetFeatureCount(), 1)
            feature = next(iter(layer))
            self.assertEqual(
                ogr.GeometryTypeToName(
                    feature.GetGeometryRef().GetGeometryType()
                ),
                "Polygon",
            )


if __name__ == "__main__":
    unittest.main()


class GrassFieldNameTest(unittest.TestCase):
    """Имена полей после импорта в GRASS и их возврат."""

    def test_grass_field_name(self):
        from qgis_xtopo.core.pipeline import _grass_field_name

        self.assertEqual(_grass_field_name("@id"), "xid")
        self.assertEqual(_grass_field_name("@type"), "xtype")
        self.assertEqual(_grass_field_name("name:ru"), "name_ru")
        self.assertEqual(_grass_field_name("name"), "name")
        self.assertEqual(_grass_field_name("1abc"), "xabc")
        self.assertEqual(_grass_field_name("a b"), "a_b")

    def test_field_names_are_restored_after_skeleton(self):
        from osgeo import ogr

        from qgis_xtopo.core.pipeline import Pipeline

        ogr.RegisterAll()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory, project_name="logic"
            )
            config.ensure_directories()
            path = config.temp_dir / "skeleton.gpkg"
            dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
            layer = dataset.CreateLayer("skeleton", None, ogr.wkbLineString)
            for name in ("xid", "xtype", "name_ru", "name"):
                layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("xid", "relation/1")
            feature.SetField("xtype", "relation")
            feature.SetField("name_ru", "ледник Зопхито")
            feature.SetField("name", "Zopkhitos")
            line = ogr.Geometry(ogr.wkbLineString)
            line.AddPoint(0, 0)
            line.AddPoint(1, 1)
            feature.SetGeometry(line)
            layer.CreateFeature(feature)
            dataset = None

            pipeline = Pipeline(config, BBox(1, 1, 3, 3), _FakeRunner(None))
            restored = pipeline._restore_field_names(
                "glacier", path, ["@id", "@type", "name:ru", "name"]
            )
            self.assertNotEqual(str(restored), str(path))
            dataset = ogr.Open(str(restored))
            names = [
                dataset.GetLayer(0).GetLayerDefn().GetFieldDefn(index).GetName()
                for index in range(
                    dataset.GetLayer(0).GetLayerDefn().GetFieldCount()
                )
            ]
            self.assertEqual(names, ["@id", "@type", "name:ru", "name"])
            feature = next(iter(dataset.GetLayer(0)))
            self.assertEqual(feature.GetField("name:ru"), "ледник Зопхито")
            self.assertEqual(feature.GetField("@id"), "relation/1")
            dataset = None


class DissolveFieldsTest(unittest.TestCase):
    """Растворение по пустому или отсутствующему полю не сливает слой."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = Config(
            qgis_projects_dir=self.directory.name, project_name="logic"
        )
        config.ensure_directories()
        self.pipeline = Pipeline(
            config, BBox(1, 1, 3, 3), _FakeRunner(None)
        )
        self.counter = 0

    def _layer(self, field_names, rows):
        from osgeo import ogr

        ogr.RegisterAll()
        self.counter += 1
        path = Path(self.directory.name) / f"source_{self.counter}.sqlite"
        dataset = ogr.GetDriverByName("SQLite").CreateDataSource(str(path))
        layer = dataset.CreateLayer("data", None, ogr.wkbPoint)
        for name in field_names:
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
        for row in rows:
            feature = ogr.Feature(layer.GetLayerDefn())
            for name, value in row.items():
                if value is not None:
                    feature.SetField(name, value)
            point = ogr.Geometry(ogr.wkbPoint)
            point.AddPoint(0, 0)
            feature.SetGeometry(point)
            layer.CreateFeature(feature)
        dataset = None
        return path

    def test_missing_field_falls_back_to_id(self):
        path = self._layer(["@id", "highway"], [
            {"@id": "node/1", "highway": "ford"},
            {"@id": "node/2", "highway": "ford"},
        ])
        self.assertEqual(self.pipeline._dissolve_fields(path, ["name"]), ["@id"])

    def test_empty_field_falls_back_to_id(self):
        path = self._layer(["@id", "name"], [
            {"@id": "node/1", "name": None},
            {"@id": "node/2", "name": ""},
        ])
        self.assertEqual(self.pipeline._dissolve_fields(path, ["name"]), ["@id"])

    def test_field_with_values_is_kept(self):
        path = self._layer(["@id", "name"], [
            {"@id": "way/1", "name": "Бартуй"},
            {"@id": "way/2", "name": None},
        ])
        self.assertEqual(self.pipeline._dissolve_fields(path, ["name"]), ["name"])

    def test_empty_field_list_stays_empty(self):
        path = self._layer(["@id"], [{"@id": "way/1"}])
        self.assertEqual(self.pipeline._dissolve_fields(path, []), [])


class CleanLayersTest(unittest.TestCase):
    """Перед подготовкой удаляются прежние слои проекта."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(
            qgis_projects_dir=self.directory.name, project_name="demo"
        )
        self.config.ensure_directories()
        self.vector = self.config.vector_dir / "roads.sqlite"
        self.raster = self.config.raster_dir / "hillshade.tif"
        self.dem = self.config.raster_dir / "merged_dem.tif"
        self.marker = self.config.raster_dir / "merged_dem_3857.tif.sha256"
        for path in (self.vector, self.raster, self.dem, self.marker):
            path.write_text("data", encoding="utf-8")

    def test_layers_are_removed(self):
        removed = _clean_layers(self.config)
        self.assertEqual(removed, 2)
        self.assertFalse(self.vector.exists())
        self.assertFalse(self.raster.exists())

    def test_dem_cache_is_kept(self):
        _clean_layers(self.config)
        self.assertTrue(self.dem.exists())
        self.assertTrue(self.marker.exists())

    def test_missing_directories_are_ok(self):
        empty = Config(
            qgis_projects_dir=self.directory.name, project_name="empty"
        )
        self.assertEqual(_clean_layers(empty), 0)
