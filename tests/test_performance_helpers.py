import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        from qgis_xtopo.core.cpu import cpu_workers
        from qgis_xtopo.core.pipeline import Pipeline

        class FakeConfig:
            parallel_workers = 0

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = FakeConfig()
        # Здесь проверяется только потолок по ядрам; ограничение памяти
        # проверяется отдельно в MemoryBudgetTest.
        with mock.patch(
            "qgis_xtopo.core.pipeline.worker_cap",
            side_effect=lambda per_worker, cpu_limit=None, budget=None:
                cpu_limit,
        ):
            self.assertGreaterEqual(pipeline._worker_count(100), 1)
            self.assertEqual(
                pipeline._worker_count(100), min(cpu_workers(limit=8), 100)
            )

            FakeConfig.parallel_workers = 3
            self.assertEqual(pipeline._worker_count(100), 3)
            # потоков не может быть больше, чем слоёв
            self.assertEqual(pipeline._worker_count(2), 2)


class MemoryBudgetTest(unittest.TestCase):
    def test_budget_on_eight_gigabytes(self):
        from qgis_xtopo.core import memory

        budget = memory.memory_budget(
            total=8 * memory.GIB, available=5 * memory.GIB
        )
        self.assertGreaterEqual(budget, memory.MIN_BUDGET)
        self.assertLess(budget, 5 * memory.GIB)
        self.assertEqual(
            memory.worker_cap(
                memory.LAYER_WORKER_BYTES, cpu_limit=8, budget=budget
            ),
            2,
        )

    def test_more_memory_gives_more_workers(self):
        from qgis_xtopo.core import memory

        small = memory.memory_budget(
            total=8 * memory.GIB, available=5 * memory.GIB
        )
        large = memory.memory_budget(
            total=32 * memory.GIB, available=28 * memory.GIB
        )
        small_workers = memory.worker_cap(
            memory.LAYER_WORKER_BYTES, cpu_limit=16, budget=small
        )
        large_workers = memory.worker_cap(
            memory.LAYER_WORKER_BYTES, cpu_limit=16, budget=large
        )
        self.assertLess(small_workers, large_workers)
        self.assertGreaterEqual(large_workers, 8)

    def test_osmium_estimate_scales_with_map_size(self):
        from qgis_xtopo.core import memory

        with tempfile.TemporaryDirectory() as directory:
            small = Path(directory) / "small.osm.pbf"
            large = Path(directory) / "large.osm.pbf"
            small.write_bytes(b"")
            with large.open("wb") as handle:
                handle.truncate(100 * memory.MIB)
            small_estimate = memory.osmium_extract_bytes(small)
            large_estimate = memory.osmium_extract_bytes(large)
            filter_estimate = memory.osmium_filter_bytes(large)
        self.assertGreater(small_estimate, 0)
        self.assertGreater(large_estimate, small_estimate)
        self.assertLess(filter_estimate, large_estimate)

    def test_windows_memory_source_is_used(self):
        from qgis_xtopo.core import memory

        with mock.patch.object(memory.sys, "platform", "win32"), \
                mock.patch.object(memory.os, "name", "nt"), \
                mock.patch.object(
                    memory, "_windows_memory",
                    return_value=(8 * memory.GIB, 5 * memory.GIB),
                ):
            self.assertEqual(
                memory.memory_state(), (8 * memory.GIB, 5 * memory.GIB)
            )

    def test_limiter_serializes_tasks_over_budget(self):
        from qgis_xtopo.core.memory import MemoryLimiter

        limiter = MemoryLimiter(100)
        with limiter.reservation(80):
            self.assertEqual(limiter.used, 80)
        self.assertEqual(limiter.used, 0)
        # Задача крупнее бюджета всё равно должна запуститься одна.
        with limiter.reservation(200):
            self.assertEqual(limiter.used, 200)
        self.assertEqual(limiter.used, 0)


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
