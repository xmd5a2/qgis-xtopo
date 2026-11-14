import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from osgeo import ogr, osr

from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config, default_queries
from qgis_xtopo.core.pipeline import (
    Pipeline, PipelineError, _ProgressTracker, _clean_layers,
    _report_terrain_stage, progress_stages,
)
from qgis_xtopo.core.query_recipes import RECIPES
from qgis_xtopo.core.resources import query_names


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
            output = _feature_collection(config.temp_dir / "output.geojson")
            runner = _FakeRunner(lambda parameters: {"output": str(output)})
            pipeline = Pipeline(config, BBox(1, 2, 3, 4), runner)
            result = pipeline._algorithm(
                "native:test",
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

    def test_spatialite_write_does_not_compress_geometry(self):
        """COMPRESS_GEOM вместе с SPATIALITE меняет топологию полигонов."""
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
        self.assertNotIn("COMPRESS_GEOM=YES", recorded["options"])

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


class TerrainStageMessageTest(unittest.TestCase):
    """Пока рельеф догружается, журнал должен говорить об этом."""

    def test_waiting_line_while_terrain_runs(self):
        started = threading.Event()
        release = threading.Event()

        def worker():
            started.set()
            release.wait(5)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            messages = []
            _report_terrain_stage(thread, messages.append)
            self.assertEqual(messages[0], "Обработка рельефа")
            self.assertIn("продолжается", messages[1])
        finally:
            release.set()
            thread.join(5)

    def test_no_waiting_line_when_terrain_done(self):
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join(5)
        messages = []
        _report_terrain_stage(thread, messages.append)
        self.assertEqual(messages, ["Обработка рельефа"])


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

    def test_water_overlay_for_rivers_is_fixed(self):
        """Вода для difference рек чинится: иначе слой river пропускается."""
        from qgis_xtopo.core.query_recipes import recipe_for

        steps = [operation for operation, _ in recipe_for("water_without_riverbanks").steps]
        self.assertEqual(steps, ["fix", "save"])

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




class LabelAxisTest(unittest.TestCase):
    """Шаг label_axis строит линии подписи без GRASS."""

    def test_builds_lines_and_keeps_fields(self):
        from osgeo import ogr

        from qgis_xtopo.core.pipeline import Pipeline

        ogr.RegisterAll()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory, project_name="logic")
            config.ensure_directories()
            source = config.temp_dir / "wood.gpkg"
            dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(source))
            layer = dataset.CreateLayer("wood", None, ogr.wkbPolygon)
            layer.CreateField(ogr.FieldDefn("id", ogr.OFTString))
            layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("id", "way/1")
            feature.SetField("name", "Лес")
            ring = ogr.Geometry(ogr.wkbLinearRing)
            corners = ((0.0, 0.0), (0.02, 0.0), (0.02, 0.006), (0.0, 0.006))
            for x, y in corners + corners[:1]:
                ring.AddPoint_2D(x, y)
            polygon = ogr.Geometry(ogr.wkbPolygon)
            polygon.AddGeometry(ring)
            feature.SetGeometry(polygon)
            layer.CreateFeature(feature)
            dataset = None

            pipeline = Pipeline(config, BBox(0, 0, 0.02, 0.006), _FakeRunner(None))
            output = pipeline._label_axis("wood", source, False)
            dataset = ogr.Open(str(output))
            layer = dataset.GetLayer(0)
            self.assertEqual(layer.GetGeomType(), ogr.wkbMultiLineString)
            self.assertEqual(layer.GetFeatureCount(), 1)
            feature = next(iter(layer))
            self.assertEqual(feature.GetField("id"), "way/1")
            self.assertEqual(feature.GetField("name"), "Лес")
            self.assertEqual(
                feature.GetGeometryRef().GetGeometryName(), "MULTILINESTRING"
            )
            dataset = None

    def test_skips_duplicate_name_and_outline(self):
        from osgeo import ogr

        from qgis_xtopo.core.pipeline import Pipeline

        ogr.RegisterAll()
        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory, project_name="logic")
            config.ensure_directories()
            source = config.temp_dir / "glacier.gpkg"
            dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(source))
            layer = dataset.CreateLayer("glacier", None, ogr.wkbPolygon)
            layer.CreateField(ogr.FieldDefn("id", ogr.OFTString))
            layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
            corners = ((0.0, 0.0), (0.02, 0.0), (0.02, 0.006), (0.0, 0.006))
            for index, shift in enumerate((0.0, 1e-9, 0.05)):
                feature = ogr.Feature(layer.GetLayerDefn())
                feature.SetField("id", f"relation/{index + 1}")
                feature.SetField("name", "Ледник")
                ring = ogr.Geometry(ogr.wkbLinearRing)
                for x, y in corners + corners[:1]:
                    ring.AddPoint_2D(x + shift, y)
                polygon = ogr.Geometry(ogr.wkbPolygon)
                polygon.AddGeometry(ring)
                feature.SetGeometry(polygon)
                layer.CreateFeature(feature)
            dataset = None

            pipeline = Pipeline(config, BBox(0, 0, 0.07, 0.006), _FakeRunner(None))
            output = pipeline._label_axis("glacier", source, False)
            dataset = ogr.Open(str(output))
            layer = dataset.GetLayer(0)
            ids = [feature.GetField("id") for feature in layer]
            self.assertEqual(ids, ["relation/1", "relation/3"])
            dataset = None

    @unittest.skipUnless(
        os.environ.get("QGIS_XTOPO_TEST_AUTOMAP"),
        "нужен QGIS_XTOPO_TEST_AUTOMAP с каталогом vector проекта",
    )
    def test_real_duplicate_is_skipped(self):
        from osgeo import ogr

        from qgis_xtopo.core.pipeline import Pipeline

        ogr.RegisterAll()
        source = Path(os.environ["QGIS_XTOPO_TEST_AUTOMAP"]) / "glacier.sqlite"
        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory, project_name="logic")
            config.ensure_directories()
            pipeline = Pipeline(
                config, BBox(7.8, 46.0, 7.95, 46.12), _FakeRunner(None)
            )
            output = pipeline._label_axis("glacier", source, True)
            dataset = ogr.Open(str(output))
            layer = dataset.GetLayer(0)
            self.assertEqual(layer.GetFeatureCount(), 190)
            names = [feature.GetField("name") for feature in layer
                     if feature.GetField("name") == "Feegletscher"]
            self.assertEqual(len(names), 1)
            dataset = None


class LabelAxisRecipeTest(unittest.TestCase):
    """Рецепт подписи не должен заранее сглаживать контур."""

    def test_label_axis_uses_unsmoothed_geometry(self):
        # buffer/simplify перед label_axis глушают узкие ветки ледников
        # (у Allalingletscher линия уходила под надпись «3346 м» вместо
        # главной ветки): алгоритм сам упрощает контур, как ему нужно.
        from qgis_xtopo.core.query_recipes import recipe_for

        for name in ("glacier", "bay_polygon", "wetland", "wood", "strait"):
            steps = [operation for operation, _ in recipe_for(name).steps]
            axis = steps.index("label_axis")
            self.assertNotIn("buffer", steps[:axis], name)
            self.assertNotIn("simplify", steps[:axis], name)


class ProcessingImportTest(unittest.TestCase):
    def test_empty_local_processing_dir_does_not_shadow_qgis(self):
        """Пустой processing/ в корне не должен подменять пакет QGIS."""
        import sys

        from tests import qgis_app

        qgis_app()
        from qgis_xtopo.core import pipeline

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "processing").mkdir()
            saved = {
                name: module for name, module in sys.modules.items()
                if name == "processing" or name.startswith("processing.")
            }
            for name in saved:
                del sys.modules[name]
            sys.path.insert(0, directory)
            try:
                module, _Processing = pipeline._load_processing()
                self.assertTrue(hasattr(module, "core"))
            finally:
                sys.path.remove(directory)
                for name in list(sys.modules):
                    if name == "processing" or name.startswith("processing."):
                        del sys.modules[name]
                sys.modules.update(saved)


class GeometryRepairRecipeTest(unittest.TestCase):
    """Растворение воды создаёт самопересечения, ломающие difference."""

    def test_dissolved_water_is_repaired_after_singleparts(self):
        from qgis_xtopo.core.query_recipes import recipe_for

        for name in ("water", "water_intermittent"):
            steps = [operation for operation, _ in recipe_for(name).steps]
            after = steps[steps.index("singleparts") + 1:]
            self.assertIn("fix", after, name)
            self.assertEqual(steps[-1], "save", name)

    def test_raw_polygon_layers_are_repaired(self):
        from qgis_xtopo.core.query_recipes import recipe_for

        for name in ("bare_rock", "scree"):
            steps = [operation for operation, _ in recipe_for(name).steps]
            self.assertEqual(steps[0], "fix", name)

    def test_admin_boundaries_are_repaired_before_polygonstolines(self):
        from qgis_xtopo.core.query_recipes import recipe_for

        for name in ("admin_level_2", "admin_level_4"):
            steps = [operation for operation, _ in recipe_for(name).steps]
            self.assertEqual(steps[0], "fix", name)
            self.assertEqual(steps[1], "polygonstolines", name)


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


class _PeakConfig:
    def __init__(self, root, source="srtm"):
        self.vector_dir = Path(root) / "vector"
        self.raster_dir = Path(root) / "raster"
        self.terrain_cache_dir = Path(root) / "srtm-cache"
        self.terrain_result_cache_dir = Path(root) / "terrain-cache"
        self._source = source

    def resolved_terrain_source(self):
        return self._source


class PeakFilterTest(unittest.TestCase):
    BBOX = BBox(30.0, 35.99, 30.01, 36.0)

    @staticmethod
    def _write_dem(path, value=1000.0, size=60, step=1.0 / 3600.0):
        import numpy as np
        from osgeo import gdal, osr

        gdal.UseExceptions()
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(str(path), size, size, 1, gdal.GDT_Float32)
        dataset.SetGeoTransform((30.0, step, 0.0, 36.0, 0.0, -step))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        dataset.SetProjection(srs.ExportToWkt())
        dataset.GetRasterBand(1).WriteArray(
            np.full((size, size), value, dtype="float32")
        )
        dataset = None

    @staticmethod
    def _write_layer(path, peaks):
        from osgeo import ogr, osr

        ogr.UseExceptions()
        path = Path(path)
        path.unlink(missing_ok=True)
        driver = ogr.GetDriverByName("SQLite")
        dataset = driver.CreateDataSource(str(path))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        layer = dataset.CreateLayer("peak", srs, ogr.wkbPoint)
        for name in ("name", "@id", "ele"):
            layer.CreateField(ogr.FieldDefn(name, ogr.OFTString))
        for peak in peaks:
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetField("@id", peak["id"])
            if peak.get("name"):
                feature.SetField("name", peak["name"])
            if peak.get("ele") is not None:
                feature.SetField("ele", str(peak["ele"]))
            geometry = ogr.Geometry(ogr.wkbPoint)
            geometry.AddPoint_2D(peak["lon"], peak["lat"])
            feature.SetGeometry(geometry)
            layer.CreateFeature(feature)
        dataset = None

    def _root(self, directory):
        root = Path(directory)
        (root / "vector").mkdir()
        (root / "raster").mkdir()
        return root

    def test_removes_false_peaks_and_updates_ele(self):
        from unittest import mock

        from qgis_xtopo.core import peak_filter, peak_placement

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write_dem(root / "raster" / "merged_dem.tif")
            self._write_layer(root / "vector" / "peak.sqlite", [
                {"id": "node/1", "ele": 900, "lon": 30.005, "lat": 35.995},
                {"id": "node/2", "ele": 1000, "lon": 30.006, "lat": 35.996},
                {"id": "node/3", "name": "Named", "ele": 500,
                 "lon": 30.007, "lat": 35.997},
                {"id": "node/4", "ele": None, "lon": 30.008, "lat": 35.998},
            ])
            messages = []

            def fake_measure(_peak, _dem, _radii, _thresholds):
                return type("Metrics", (), {"dem": 1000.0})()

            def fake_classify(peak, _metrics, _thresholds, neighbour=None):
                if peak.osm_id in ("node/2", "node/4"):
                    return "error", [], None
                return "ok", [], None

            with mock.patch.object(peak_placement, "measure", fake_measure), \
                    mock.patch.object(peak_placement, "classify", fake_classify):
                stats = peak_filter.filter_peak_layer(
                    _PeakConfig(root), self.BBOX, progress=messages.append
                )

            self.assertEqual(stats, {
                "checked": 3, "removed": 2, "ele_fixed": 1, "no_data": 0,
            })
            self.assertIn(
                "Пики: проверено 3, удалено 2, исправлено ele 1", messages
            )
            import sqlite3

            database = sqlite3.connect(root / "vector" / "peak.sqlite")
            identifiers = {
                row[0] for row in database.execute('select "@id" from peak')
            }
            self.assertEqual(identifiers, {"node/1", "node/3"})
            ele = database.execute(
                'select ele from peak where "@id" = ?', ("node/1",)
            ).fetchone()[0]
            self.assertEqual(ele, "1000")
            database.close()

    def test_cached_result_is_reapplied(self):
        """Повторный прогон с тем же рельефом и пиками не считает метрики."""
        from unittest import mock

        from qgis_xtopo.core import peak_filter, peak_placement

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write_dem(root / "raster" / "merged_dem.tif")
            peaks = [
                {"id": "node/1", "ele": 900, "lon": 30.005, "lat": 35.995},
                {"id": "node/2", "ele": 1000, "lon": 30.006, "lat": 35.996},
                {"id": "node/3", "name": "Named", "ele": 500,
                 "lon": 30.007, "lat": 35.997},
                {"id": "node/4", "ele": None, "lon": 30.008, "lat": 35.998},
            ]
            layer_path = root / "vector" / "peak.sqlite"
            self._write_layer(layer_path, peaks)

            def fake_measure(_peak, _dem, _radii, _thresholds):
                return type("Metrics", (), {"dem": 1000.0})()

            def fake_classify(peak, _metrics, _thresholds, neighbour=None):
                if peak.osm_id in ("node/2", "node/4"):
                    return "error", [], None
                return "ok", [], None

            with mock.patch.object(peak_placement, "measure", fake_measure), \
                    mock.patch.object(peak_placement, "classify", fake_classify):
                first = peak_filter.filter_peak_layer(
                    _PeakConfig(root), self.BBOX, dem_key="dem-1"
                )
            self.assertEqual(first, {
                "checked": 3, "removed": 2, "ele_fixed": 1, "no_data": 0,
            })

            self._write_layer(layer_path, peaks)
            messages = []
            with mock.patch.object(
                    peak_placement, "measure",
                    side_effect=AssertionError("метрики не должны считаться"),
            ), mock.patch.object(
                    peak_placement, "classify",
                    side_effect=AssertionError("классификация не должна считаться"),
            ):
                second = peak_filter.filter_peak_layer(
                    _PeakConfig(root), self.BBOX, dem_key="dem-1",
                    progress=messages.append,
                )
            self.assertEqual(second, first)
            self.assertIn(
                "Пики: проверено 3, удалено 2, исправлено ele 1", messages
            )
            import sqlite3

            database = sqlite3.connect(layer_path)
            identifiers = {
                row[0] for row in database.execute('select "@id" from peak')
            }
            self.assertEqual(identifiers, {"node/1", "node/3"})
            ele = database.execute(
                'select ele from peak where "@id" = ?', ("node/1",)
            ).fetchone()[0]
            self.assertEqual(ele, "1000")
            database.close()

    def test_ele_mismatch_is_corrected_not_removed(self):
        from unittest import mock

        from qgis_xtopo.core import peak_filter, peak_placement

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write_dem(root / "raster" / "merged_dem.tif")
            self._write_layer(root / "vector" / "peak.sqlite", [
                {"id": "node/1", "ele": 500, "lon": 30.005, "lat": 35.995},
            ])
            metrics = peak_placement.Metrics()
            metrics.dem = 1000.0
            metrics.ele_diff = 50.0
            metrics.ele_below = -50.0

            with mock.patch.object(peak_placement, "measure",
                                   lambda *_args: metrics):
                stats = peak_filter.filter_peak_layer(
                    _PeakConfig(root), self.BBOX
                )

            self.assertEqual(stats["removed"], 0)
            self.assertEqual(stats["ele_fixed"], 1)
            import sqlite3

            database = sqlite3.connect(root / "vector" / "peak.sqlite")
            ele = database.execute("select ele from peak").fetchone()[0]
            self.assertEqual(ele, "1000")
            database.close()

    def test_no_terrain_skips(self):
        from qgis_xtopo.core import peak_filter

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            self._write_layer(root / "vector" / "peak.sqlite", [
                {"id": "node/1", "lon": 30.005, "lat": 35.995},
            ])
            messages = []
            stats = peak_filter.filter_peak_layer(
                _PeakConfig(root), self.BBOX, progress=messages.append
            )
            self.assertIsNone(stats)
            self.assertTrue(
                any("рельеф не подготовлен" in message for message in messages)
            )

    def test_project_dem_window(self):
        from qgis_xtopo.core import peak_filter

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            path = root / "raster" / "merged_dem.tif"
            self._write_dem(path, value=1234.0, size=30)
            dem = peak_filter.ProjectDem(path)
            try:
                self.assertTrue(dem.covers(self.BBOX))
                window = dem.window(30.005, 35.995, 2, 2)
                self.assertEqual(window.shape, (5, 5))
                self.assertTrue((window == 1234.0).all())
            finally:
                dem.close()

    def test_resampled_window(self):
        from qgis_xtopo.core import peak_filter

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            path = root / "raster" / "merged_dem.tif"
            self._write_dem(path, value=500.0, size=30, step=1.0 / 1200.0)
            dem = peak_filter.ProjectDem(path)
            try:
                self.assertFalse(dem.direct)
                window = dem.window(30.005, 35.995, 2, 2)
                self.assertEqual(window.shape, (5, 5))
                self.assertTrue((window == 500.0).all())
            finally:
                dem.close()


class PoiRecipeCrsTest(unittest.TestCase):
    """Слои со значками POI остаются в EPSG:4326.

    Расстояние подавления значков задано в градусах.  Рецепт, который
    перепроецирует слой в EPSG:3857, ломает сравнение: overlay_nearest
    начнёт мерить метры, и соседи не находятся.
    """

    ICON_RECIPES = (
        "mountain_pass",
        "railway_station_icons",
        "railway_halt_icons",
        "aerialway_station",
    )

    def test_icon_recipes_do_not_reproject(self):
        from qgis_xtopo.core.query_recipes import RECIPES

        for name in self.ICON_RECIPES:
            operations = [operation for operation, _params in RECIPES[name].steps]
            self.assertNotIn("reproject", operations, name)

    def test_mountain_pass_file_keeps_plain_name(self):
        from qgis_xtopo.core.query_recipes import RECIPES

        saves = [
            params for operation, params in RECIPES["mountain_pass"].steps
            if operation == "save"
        ]
        self.assertTrue(saves)
        self.assertEqual(saves[-1].get("suffix"), "")


class RailwayQueryTest(unittest.TestCase):
    """Фуникулёр и грузовая канатная дорога попадают в запросы подготовки."""

    def test_funicular_bridges_and_tunnels(self):
        from qgis_xtopo.core.resources import read_query

        for name, tag in (
            ("rail_bridge", "bridge"), ("rail_tunnel", "tunnel"),
        ):
            selector = f'way["railway"="funicular"]["{tag}"="yes"];'
            self.assertIn(selector, read_query(name), name)

    def test_goods_aerialway_uses_cableway_layer(self):
        from qgis_xtopo.core.resources import read_query

        self.assertIn(
            'way["aerialway"="goods"];',
            read_query("aerialway_cable_car_gondola"),
        )


def _write_polygon_layer(path, layer_name, geometries, name="upper"):
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset = ogr.GetDriverByName("SQLite").CreateDataSource(str(path))
    layer = dataset.CreateLayer(layer_name, srs, ogr.wkbUnknown)
    layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
    for index, wkt in enumerate(geometries):
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("name", f"{name}{index}")
        feature.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        layer.CreateFeature(feature)
    dataset = None


def _read_geometries(path):
    dataset = ogr.Open(str(path))
    layer = dataset.GetLayer(0)
    result = [feature.GetGeometryRef().Clone() for feature in layer]
    dataset = None
    return result


def _interior_rings(geometry):
    if geometry.GetGeometryName() == "MULTIPOLYGON":
        return sum(
            max(0, geometry.GetGeometryRef(index).GetGeometryCount() - 1)
            for index in range(geometry.GetGeometryCount())
        )
    return max(0, geometry.GetGeometryCount() - 1)


class PolygonOverlapTest(unittest.TestCase):
    """Меньший полигон вырезается из большего, но не наоборот."""

    def _config(self, directory):
        config = Config(
            qgis_projects_dir=directory,
            project_name="demo",
            array_queries=["farmland", "grass"],
        )
        config.ensure_directories()
        return config

    def test_smaller_polygon_is_cut_out(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((4 4,4 6,6 6,6 4,4 4))"],
            )
            results = resolve_polygon_overlaps(config)
            geometry = _read_geometries(
                config.vector_dir / "farmland.sqlite"
            )[0]
        self.assertEqual(results, [("farmland", "grass", 1)])
        self.assertAlmostEqual(geometry.Area(), 96.0, places=6)
        self.assertEqual(_interior_rings(geometry), 1)

    def test_small_overlap_is_kept(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((0 0,0 0.00005,0.00005 0.00005,0.00005 0,0 0))"],
            )
            results = resolve_polygon_overlaps(config)
            geometry = _read_geometries(
                config.vector_dir / "farmland.sqlite"
            )[0]
        self.assertEqual(results, [("farmland", "grass", 0)])
        self.assertAlmostEqual(geometry.Area(), 100.0, places=6)
        self.assertEqual(_interior_rings(geometry), 0)

    def test_progress_is_one_compact_line(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((4 4,4 6,6 6,6 4,4 4))"],
            )
            messages = []
            resolve_polygon_overlaps(config, progress=messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn("изменено 1 объектов", messages[0])
        self.assertIn("farmland/grass — 1", messages[0])

    def test_progress_reports_nothing_changed(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((0 0,0 20,20 20,20 0,0 0))"],
            )
            messages = []
            resolve_polygon_overlaps(config, progress=messages.append)
        self.assertEqual(
            messages, ["Вырезание пересечений: изменений нет"],
        )

    def test_larger_lower_polygon_is_not_cut(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((4 4,4 6,6 6,6 4,4 4))"],
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((0 0,0 20,20 20,20 0,0 0))"],
            )
            results = resolve_polygon_overlaps(config)
            geometry = _read_geometries(
                config.vector_dir / "farmland.sqlite"
            )[0]
        self.assertEqual(results, [("farmland", "grass", 0)])
        self.assertAlmostEqual(geometry.Area(), 4.0, places=6)
        self.assertEqual(_interior_rings(geometry), 0)

    def test_water_uses_dissolved_layer(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "water_dissolved.sqlite",
                "water_dissolved",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
            )
            _write_polygon_layer(
                config.vector_dir / "island.sqlite",
                "island",
                ["POLYGON((4 4,4 6,6 6,6 4,4 4))"],
            )
            results = resolve_polygon_overlaps(config)
            geometry = _read_geometries(
                config.vector_dir / "water_dissolved.sqlite"
            )[0]
        self.assertEqual(results, [("water", "island", 1)])
        self.assertAlmostEqual(geometry.Area(), 96.0, places=6)
        self.assertEqual(_interior_rings(geometry), 1)

    def test_attributes_are_preserved(self):
        from qgis_xtopo.core.polygon_overlaps import resolve_polygon_overlaps

        with tempfile.TemporaryDirectory() as directory:
            config = self._config(directory)
            _write_polygon_layer(
                config.vector_dir / "farmland.sqlite",
                "farmland",
                ["POLYGON((0 0,0 10,10 10,10 0,0 0))"],
                name="field",
            )
            _write_polygon_layer(
                config.vector_dir / "grass.sqlite",
                "grass",
                ["POLYGON((4 4,4 6,6 6,6 4,4 4))"],
            )
            resolve_polygon_overlaps(config)
            dataset = ogr.Open(str(config.vector_dir / "farmland.sqlite"))
            feature = next(iter(dataset.GetLayer(0)))
            name = feature.GetField("name")
            dataset = None
        self.assertEqual(name, "field0")


class GeofabrikMergedFallbackTest(unittest.TestCase):
    """Склеенный osmium файл не читается — читаем карты по отдельности."""

    def test_retry_uses_separate_maps_without_osmium(self):
        from types import SimpleNamespace
        from unittest import mock

        from qgis_xtopo.core import sources
        from qgis_xtopo.core.bbox import parse_bbox

        config = Config(
            project_name="fallback",
            bbox="41.8,42.9,43.2,43.6",
            array_queries=["places_main"],
            source_mode="geofabrik",
            geofabrik_maps=["georgia", "north-caucasus-fed-district"],
        )
        maps = [SimpleNamespace(id="georgia"),
                SimpleNamespace(id="north-caucasus-fed-district")]
        state = SimpleNamespace(
            index=SimpleNamespace(maps=maps), warning="")
        paths = [Path("/tmp/georgia.osm.pbf"),
                 Path("/tmp/north-caucasus.osm.pbf")]
        dataset = SimpleNamespace(warnings=[], notes=[], feature_files={})
        calls = []

        def prepare(_config, _bbox, _names, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError()
            return dataset

        with mock.patch.object(
                sources.geofabrik, "fetch_index", return_value=state,
        ), mock.patch.object(
                sources, "_selected_maps", return_value=maps,
        ), mock.patch.object(
                sources.osmium_tool, "find_osmium",
                return_value="/usr/bin/osmium",
        ), mock.patch.object(
                sources.geofabrik, "local_ready", return_value=True,
        ), mock.patch.object(
                sources.geofabrik, "source_paths", return_value=paths,
        ), mock.patch.object(
                sources.geofabrik, "local_maps", return_value=paths,
        ), mock.patch.object(
                sources.pbf_source, "prepare_dataset", side_effect=prepare,
        ):
            result = sources.prepare_sources(
                config, parse_bbox(config.bbox), ["places_main"])

        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1].get("use_osmium", True))
        self.assertEqual(calls[1]["sources"], paths)
        self.assertTrue(any("по отдельности" in item
                            for item in result.warnings))


class FlowAccumulationTest(unittest.TestCase):
    """Новый векторный расчёт водосбора повторяет прежний поячеечный."""

    @staticmethod
    def _reference(surface, lat):
        import math

        import numpy as np

        from qgis_xtopo.core import peak_placement

        rows, columns = surface.shape
        metres_lat = 111320.0 / 3600.0
        metres_lon = 111320.0 * math.cos(math.radians(lat)) / 3600.0
        steepest = np.zeros(surface.shape, dtype=np.int8)
        slope = np.zeros(surface.shape)
        for index, (dy, dx) in enumerate(peak_placement.FLOW_NEIGHBOURS):
            distance = math.hypot(dy * metres_lat, dx * metres_lon)
            neighbour = np.full(surface.shape, np.nan)
            source_rows = slice(max(0, dy), rows + min(0, dy))
            source_columns = slice(max(0, dx), columns + min(0, dx))
            target_rows = slice(max(0, -dy), rows + min(0, -dy))
            target_columns = slice(max(0, -dx), columns + min(0, -dx))
            neighbour[target_rows, target_columns] = surface[
                source_rows, source_columns
            ]
            drop = np.nan_to_num((surface - neighbour) / distance, nan=-1e9)
            better = drop > slope
            slope = np.where(better, drop, slope)
            steepest = np.where(better, index, steepest)
        area = np.ones(surface.shape, dtype=np.float64)
        order = np.argsort(surface, axis=None)[::-1]
        flat_steepest = steepest.ravel()
        flat_area = area.ravel()
        for cell in order:
            dy, dx = peak_placement.FLOW_NEIGHBOURS[
                int(flat_steepest[cell])]
            row, column = divmod(int(cell), columns)
            target_row = row + dy
            target_column = column + dx
            if 0 <= target_row < rows and 0 <= target_column < columns:
                flat_area[target_row * columns + target_column] += (
                    flat_area[cell]
                )
        return area

    def test_matches_previous_algorithm(self):
        import numpy as np

        from qgis_xtopo.core import peak_placement

        generator = np.random.default_rng(7)
        surface = generator.uniform(100, 300, size=(41, 55))
        expected = self._reference(surface, 43.0)
        actual = peak_placement.flow_accumulation(surface, 43.0)
        self.assertTrue(np.allclose(actual, expected))


class RouteHikingTest(unittest.TestCase):
    def test_not_prepared(self):
        """route_hiking не готовится: ни запроса, ни рецепта, ни в умолчаниях."""
        self.assertNotIn("route_hiking", query_names())
        self.assertNotIn("route_hiking", RECIPES)
        self.assertNotIn("route_hiking", default_queries())


if __name__ == "__main__":
    unittest.main()
