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
            "qgis_xtopo.core.pipeline.layer_worker_limit",
            side_effect=lambda cpu_limit, terrain=False: cpu_limit,
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

    def test_small_machine_runs_more_layers(self):
        """До 6 ГБ запас на слой ниже, но не больше трёх воркеров."""
        from qgis_xtopo.core import memory

        with mock.patch.object(
            memory, "memory_state",
            return_value=(4 * memory.GIB, int(3.7 * memory.GIB)),
        ):
            self.assertEqual(memory.layer_worker_limit(4), 2)
            # рельеф забирает свой резерв, второй слой уже не помещается
            self.assertEqual(memory.layer_worker_limit(4, terrain=True), 1)
        with mock.patch.object(
            memory, "memory_state",
            return_value=(6 * memory.GIB, int(5.7 * memory.GIB)),
        ):
            self.assertEqual(memory.layer_worker_limit(4), 3)
        with mock.patch.object(
            memory, "memory_state",
            return_value=(32 * memory.GIB, 28 * memory.GIB),
        ):
            # на мощной машине прежняя оценка и потолок по ядрам
            self.assertGreaterEqual(memory.layer_worker_limit(8), 8)

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

    def test_osmium_peak_reflects_measured_memory(self):
        """Пик с запасом: 100 МБ дают ~2.6 ГБ и не влезают в 4 ГБ."""
        from qgis_xtopo.core import memory

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.osm.pbf"
            with source.open("wb") as handle:
                handle.truncate(100 * memory.MIB)
            peak = memory.osmium_extract_peak_bytes(source)
            self.assertGreater(peak, int(100 * memory.MIB * 20))
            self.assertLess(peak, int(100 * memory.MIB * 30))
            with mock.patch.object(
                memory, "memory_state",
                return_value=(4 * memory.GIB, 3 * memory.GIB),
            ):
                self.assertFalse(memory.osmium_extract_fits(source))
            with mock.patch.object(
                memory, "memory_state",
                return_value=(16 * memory.GIB, 12 * memory.GIB),
            ):
                self.assertTrue(memory.osmium_extract_fits(source))

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


class PerformanceEstimateTest(unittest.TestCase):
    class Store:
        def __init__(self, values=None):
            self.values = dict(values or {})

        def value(self, key, default=None):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

    def test_cpu_score_uses_fresh_cache(self):
        import time

        from qgis_xtopo.core import performance

        store = self.Store(
            {
                performance.SETTINGS_CPU: "1.25",
                performance.SETTINGS_CPU_TIME: str(time.time()),
            }
        )
        with mock.patch.object(
            performance, "measure_cpu_score", return_value=3.0
        ) as measure:
            self.assertAlmostEqual(performance.cpu_score(store), 1.25)
        measure.assert_not_called()

    def test_cpu_score_measures_after_expiry(self):
        import time

        from qgis_xtopo.core import performance

        store = self.Store(
            {
                performance.SETTINGS_CPU: "0.5",
                performance.SETTINGS_CPU_TIME: str(
                    time.time() - performance.CPU_MAX_AGE - 60
                ),
            }
        )
        with mock.patch.object(
            performance, "measure_cpu_score", return_value=1.75
        ):
            self.assertAlmostEqual(performance.cpu_score(store), 1.75)
        self.assertEqual(
            float(store.values[performance.SETTINGS_CPU]), 1.75
        )

    def test_fast_machine_gets_shorter_estimate(self):
        from qgis_xtopo.core import performance

        fast = performance.estimate_preparation_seconds(
            2_000_000, 100_000, 0.5, 120,
            bytes_per_second=60 * 1024 * 1024,
            cpu=1.5, workers=8,
        )
        slow = performance.estimate_preparation_seconds(
            2_000_000, 100_000, 0.5, 120,
            bytes_per_second=10 * 1024 * 1024,
            cpu=0.6, workers=3,
        )
        self.assertLess(fast, slow)

    def test_map_estimate_before_download_scales_with_pc(self):
        from qgis_xtopo.core import performance

        fast = performance.estimate_map_seconds(100, cpu=2.0, workers=8)
        slow = performance.estimate_map_seconds(100, cpu=0.5, workers=3)
        self.assertAlmostEqual(fast, 27.0)
        self.assertAlmostEqual(slow, 168.0)
        self.assertLess(fast, slow)

    def test_read_score_is_clamped(self):
        from qgis_xtopo.core import performance

        reference = 30 * 1024 * 1024
        self.assertAlmostEqual(performance.read_score(reference), 1.0)
        self.assertEqual(performance.read_score(reference / 100), 0.5)
        self.assertEqual(performance.read_score(reference * 100), 2.0)
        self.assertEqual(performance.read_score(0), 1.0)

    def test_terrain_estimate_depends_on_source_and_cache(self):
        from qgis_xtopo.core import performance

        cold = performance.estimate_terrain_seconds(
            1000, source="auto", cpu=1.0, workers=8,
        )
        cached = performance.estimate_terrain_seconds(
            1000, source="auto", cpu=1.0, workers=8, cached=True,
        )
        highres = performance.estimate_terrain_seconds(
            1000, source="3dep", cpu=1.0, workers=8,
        )
        self.assertLess(cached, cold)
        self.assertGreater(highres, cold)

    def test_terrain_isolines_depend_on_frame_area(self):
        from qgis_xtopo.core import performance

        without = performance.estimate_terrain_seconds(
            10_000, source="cop30", cpu=1.0, workers=8,
        )
        with_isolines = performance.estimate_terrain_seconds(
            10_000, source="cop30", cpu=1.0, workers=8,
            isolines=True, frame_area_km2=250_000,
        )
        # Изолинии считаются по всей рамке, а не только по суше.
        self.assertGreater(with_isolines, without + 300)

    def test_preparation_estimate_includes_terrain(self):
        from qgis_xtopo.core import performance

        common = dict(
            bytes_per_second=30 * 1024 * 1024, cpu=1.0, workers=8,
        )
        without = performance.estimate_preparation_seconds(
            1_000_000, 50_000, 0.5, 100, **common,
        )
        # Рельеф идёт параллельно слоям: короткий рельеф не добавляет
        # времени, длинный — становится ограничителем.
        short = performance.estimate_preparation_seconds(
            1_000_000, 50_000, 0.5, 100, terrain_seconds=1.0, **common,
        )
        long = performance.estimate_preparation_seconds(
            1_000_000, 50_000, 0.5, 100, terrain_seconds=100.0, **common,
        )
        self.assertAlmostEqual(short, without)
        self.assertAlmostEqual(
            long, 100.0 + performance.POSTPROCESS_SECONDS
        )


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

    def test_layer_sum_is_explained(self):
        """Сумма операций по слоям больше этапа: слои идут параллельно.

        В строке должны быть и сумма операций, и время этапа, и число
        потоков — иначе число выглядит противоречивым.
        """
        from qgis_xtopo.core.pipeline import Timings

        timings = Timings()
        timings.add_stage("обработка слоёв", 7.2)
        timings.add_step("water", "dissolve", 3.0)
        timings.add_step("wood", "buffer", 5.0)
        timings.set_workers(3)
        lines = timings.summary()
        self.assertEqual(len(lines), 2)
        line = lines[1]
        self.assertIn("8.0", line)
        self.assertIn("7.2", line)
        self.assertIn("3", line)


if __name__ == "__main__":
    unittest.main()
