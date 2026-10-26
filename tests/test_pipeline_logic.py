import json
import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config
from qgis_xtopo.core.pipeline import (
    Pipeline, PipelineError, _ProgressTracker, progress_stages,
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


if __name__ == "__main__":
    unittest.main()
