import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core import sources
from qgis_xtopo.core.config import Config


class SourceCacheTest(unittest.TestCase):
    def test_query_signature_detects_change(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            text = 'node["a"="b"];'
            self.assertFalse(sources._cache_is_current(cache, "layer", text))
            sources._store_query_signature(cache, "layer", text)
            self.assertTrue(sources._cache_is_current(cache, "layer", text))
            # изменился текст запроса — кеш больше не актуален
            self.assertFalse(
                sources._cache_is_current(cache, "layer", 'node["a"="c"];')
            )
            # пробелы вокруг запроса не считаются изменением
            self.assertTrue(
                sources._cache_is_current(cache, "layer", '  node["a"="b"];  ')
            )


class ParallelWorkersTest(unittest.TestCase):
    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="1,2,3,4",
                array_queries=["water"],
                parallel_workers=5,
            )
            config.to_file(path)
            loaded = Config.from_file(path)
            self.assertEqual(loaded.parallel_workers, 5)

    def test_worker_count(self):
        from qgis_xtopo.core.pipeline import Pipeline

        class FakeConfig:
            parallel_workers = 0

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = FakeConfig()
        self.assertGreaterEqual(pipeline._worker_count(100), 1)
        self.assertLessEqual(pipeline._worker_count(100), 8)

        FakeConfig.parallel_workers = 3
        self.assertEqual(pipeline._worker_count(100), 3)
        # потоков не может быть больше, чем слоёв
        self.assertEqual(pipeline._worker_count(2), 2)


class TimingsTest(unittest.TestCase):
    def test_summary_and_top_steps(self):
        from qgis_xtopo.core.pipeline import Timings

        timings = Timings()
        timings.add_stage("слои", 12.5)
        timings.add_step("water", "dissolve", 3.0)
        timings.add_step("wood", "buffer", 5.0)
        self.assertIn("слои", timings.summary()[0])
        top = timings.top_steps(1)
        self.assertEqual(top[0][1], "wood")


if __name__ == "__main__":
    unittest.main()
