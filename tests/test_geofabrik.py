import importlib.util
import json
import os
import struct
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None

from qgis_xtopo.core import geofabrik
from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config


def _square(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]


def _feature(identifier, parent, name, ring):
    return {
        "type": "Feature",
        "properties": {
            "id": identifier,
            "parent": parent,
            "name": name,
            "urls": {"pbf": f"https://example.org/{identifier}.osm.pbf"},
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


INDEX_PAYLOAD = {
    "type": "FeatureCollection",
    "features": [
        _feature("europe", "", "Europe", _square(0, 0, 10, 10)),
        _feature("country", "europe", "Country", _square(0, 0, 5, 10)),
        _feature("region", "country", "Region", _square(0, 0, 2, 2)),
        _feature("overlay", "europe", "Overlay", _square(0, 0, 8, 8)),
        _feature("other", "europe", "Other", _square(5, 0, 10, 10)),
    ],
}

# Родительская выгрузка шире дочерней (как "france" и "corse"): дочерняя
# покрывает только часть bbox, остальное приходится на морскую полосу.
NESTED_PAYLOAD = {
    "type": "FeatureCollection",
    "features": [
        _feature("europe", "", "Europe", _square(0, 0, 10, 10)),
        _feature("land", "europe", "Land", _square(0, 0, 4, 4)),
        _feature("island", "land", "Island", _square(0.5, 0.5, 1.5, 1.5)),
        _feature("neighbour", "europe", "Neighbour", _square(4, 0, 6, 4)),
    ],
}


def _dummy_pbf(path):
    header = b"\x0a\x09OSMHeader"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(struct.pack(">I", len(header)) + header + b"\x00" * 32)


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._body = body
        self._position = 0
        self.headers = headers or {}

    def read(self, size=-1):
        if size < 0:
            data = self._body[self._position:]
            self._position = len(self._body)
            return data
        data = self._body[self._position:self._position + size]
        self._position += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class GeofabrikIndexTest(unittest.TestCase):
    def setUp(self):
        geofabrik.clear_caches()
        self.index = geofabrik._parse_index(INDEX_PAYLOAD)

    def ids(self, bbox):
        return [item.id for item in self.index.required(bbox)]

    def test_region_only(self):
        self.assertEqual(self.ids(BBox(0.5, 0.5, 1.0, 1.0)), ["region"])

    def test_country_when_region_does_not_intersect(self):
        self.assertEqual(self.ids(BBox(3.0, 3.0, 4.0, 4.0)), ["country"])

    def test_two_neighbours(self):
        self.assertEqual(
            self.ids(BBox(4.5, 4.5, 5.5, 5.5)), ["country", "other"]
        )

    def test_parent_and_overlay_excluded(self):
        self.assertEqual(self.ids(BBox(7.0, 7.0, 8.0, 9.0)), ["other"])

    def test_empty_area(self):
        self.assertEqual(self.ids(BBox(20, 20, 21, 21)), [])

    def test_required_maps_have_urls(self):
        maps = self.index.required(BBox(0.5, 0.5, 1.0, 1.0))
        self.assertEqual(maps[0].url, "https://example.org/region.osm.pbf")


class GeofabrikHierarchyTest(unittest.TestCase):
    """Подбор карт не должен тянуть родительскую выгрузку целиком."""

    def setUp(self):
        geofabrik.clear_caches()
        self.index = geofabrik._parse_index(NESTED_PAYLOAD)

    def ids(self, bbox):
        return [item.id for item in self.index.required(bbox)]

    def test_parent_is_not_added_next_to_child(self):
        # "land" шире "island" и покрывает остаток bbox, но данные те же
        self.assertEqual(self.ids(BBox(0, 0, 1.5, 1.5)), ["island"])

    def test_parent_is_used_without_child(self):
        self.assertEqual(self.ids(BBox(2.0, 2.0, 3.0, 3.0)), ["land"])

    def test_neighbour_is_added(self):
        self.assertEqual(
            self.ids(BBox(0.5, 0.5, 4.5, 3.5)), ["island", "neighbour"]
        )

    def test_candidates_lists_every_intersecting_map_except_europe(self):
        found = [item.id for item in self.index.candidates(BBox(0, 0, 1.5, 1.5))]
        self.assertEqual(found, ["island", "land"])
        self.assertNotIn("europe", found)


class GeofabrikFetchTest(unittest.TestCase):
    def setUp(self):
        geofabrik.clear_caches()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(qgis_projects_dir=self.directory.name)
        self.body = json.dumps(INDEX_PAYLOAD).encode("utf-8")
        self.meta_path = self.config.geofabrik_dir / "index-meta.json"
        self.index_path = self.config.geofabrik_dir / "index-v1.json"

    def _response(self):
        return _Response(
            200, self.body,
            {
                "Content-Length": str(len(self.body)),
                "ETag": '"tag"',
                "Last-Modified": "Tue, 20 Oct 2026 04:04:13 GMT",
            },
        )

    def test_downloads_and_uses_cache(self):
        with mock.patch.object(geofabrik, "_urlopen", return_value=self._response()):
            state = geofabrik.fetch_index(self.config)
        self.assertTrue(self.index_path.exists())
        self.assertEqual(len(state.index.maps), 5)
        self.assertFalse(state.warning)
        with mock.patch.object(
            geofabrik, "_urlopen", side_effect=AssertionError("network used")
        ):
            cached = geofabrik.fetch_index(self.config)
        self.assertEqual(len(cached.index.maps), 5)

    def test_conditional_request_304(self):
        with mock.patch.object(geofabrik, "_urlopen", return_value=self._response()):
            geofabrik.fetch_index(self.config)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["fetched_at"] = 0
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")

        def not_modified(_request, timeout=None):
            raise urllib.error.HTTPError(
                geofabrik.INDEX_URL, 304, "Not Modified", {}, None
            )

        with mock.patch.object(geofabrik, "_urlopen", side_effect=not_modified):
            state = geofabrik.fetch_index(self.config)
        self.assertEqual(len(state.index.maps), 5)
        self.assertFalse(state.warning)
        updated = json.loads(self.meta_path.read_text(encoding="utf-8"))
        self.assertGreater(updated["fetched_at"], 0)

    def test_network_error_uses_cache(self):
        with mock.patch.object(geofabrik, "_urlopen", return_value=self._response()):
            geofabrik.fetch_index(self.config)
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        meta["fetched_at"] = 0
        self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with mock.patch.object(
            geofabrik, "_urlopen", side_effect=urllib.error.URLError("offline")
        ):
            state = geofabrik.fetch_index(self.config)
        self.assertEqual(len(state.index.maps), 5)
        self.assertIn("Не удалось обновить", state.warning)

    def test_network_error_without_cache_raises(self):
        with mock.patch.object(
            geofabrik, "_urlopen", side_effect=urllib.error.URLError("offline")
        ):
            with self.assertRaises(geofabrik.GeofabrikError):
                geofabrik.fetch_index(self.config)


class GeofabrikDownloadTest(unittest.TestCase):
    def setUp(self):
        geofabrik.clear_caches()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(qgis_projects_dir=self.directory.name)
        self.first = geofabrik.GeofabrikMap(
            "first", "First", "", "https://example.org/first.osm.pbf"
        )
        self.second = geofabrik.GeofabrikMap(
            "second", "Second", "", "https://example.org/second.osm.pbf"
        )

    def test_check_maps_reports_local(self):
        _dummy_pbf(geofabrik.local_path(self.config, self.first))

        def head(url):
            if url == self.first.url:
                return True, geofabrik.local_path(self.config, self.first).stat().st_size, ""
            return False, 0, "404"

        with mock.patch.object(geofabrik, "_head_size", side_effect=head):
            statuses = geofabrik.check_maps(self.config, [self.first, self.second])
        self.assertTrue(statuses[0].available)
        self.assertTrue(statuses[0].local)
        self.assertFalse(statuses[1].available)
        self.assertFalse(statuses[1].local)

    def test_ensure_maps_skips_current_files(self):
        path = geofabrik.local_path(self.config, self.first)
        _dummy_pbf(path)
        status = geofabrik.MapStatus(
            map=self.first, available=True, size=path.stat().st_size
        )
        with mock.patch.object(
            geofabrik, "_download_one", side_effect=AssertionError("downloaded")
        ):
            paths = geofabrik.ensure_maps(
                self.config, [self.first], statuses=[status]
            )
        self.assertEqual(paths, [path])

    def test_ensure_maps_rejects_unavailable_map(self):
        status = geofabrik.MapStatus(map=self.first, available=False, error="404")
        with self.assertRaises(geofabrik.GeofabrikError):
            geofabrik.ensure_maps(self.config, [self.first], statuses=[status])

    def test_download_writes_file(self):
        body = struct.pack(">I", 11) + b"\x0a\x09OSMHeader" + b"\x00" * 16
        response = _Response(200, body, {"Content-Length": str(len(body))})
        status = geofabrik.MapStatus(map=self.first, available=True, size=len(body))
        transfer = geofabrik._Transfer(len(body), 1)
        with mock.patch.object(geofabrik, "_urlopen", return_value=response):
            path = geofabrik._download_one(
                self.config, self.first, status, transfer, None, None
            )
        self.assertTrue(path.exists())
        self.assertTrue(geofabrik._looks_like_pbf(path))
        self.assertEqual(transfer.done, len(body))
        self.assertEqual(transfer.maps, 1)

    def test_transfer_reports_aggregate_progress(self):
        values = []
        transfer = geofabrik._Transfer(200, 2, lambda value, text: values.append((value, text)))
        transfer.add(50)
        transfer.map_done()
        transfer.add(150)
        transfer.finish()
        self.assertEqual(values[-1][0], 100)
        self.assertIn("Загрузка карт", values[-1][1])
        self.assertIn("2/2", values[-1][1])

    def test_transfer_without_sizes_stays_indeterminate(self):
        values = []
        transfer = geofabrik._Transfer(0, 1, lambda value, text: values.append(value))
        transfer.add(1048576)
        self.assertEqual(values[-1], -1)

    def test_transfer_reports_download_speed(self):
        values = []
        transfer = geofabrik._Transfer(100 * 1048576, 2, lambda value, text: values.append((value, text)))
        # Первая секунда: скачано 4 МБ, скорость около 4 МБ/с.
        transfer._speed_time = time.monotonic() - 1.0
        transfer._speed_done = 0
        transfer.add(4 * 1048576)
        self.assertIn("МБ/с", values[-1][1])
        self.assertIn("4.0 МБ/с", values[-1][1])

    def test_transfer_speed_is_smoothed(self):
        values = []
        transfer = geofabrik._Transfer(100 * 1048576, 1, lambda value, text: values.append((value, text)))
        transfer._speed_time = time.monotonic() - 1.0
        transfer._speed_done = 0
        transfer.add(10 * 1048576)
        # Следующий отчёт: 10 МБ за полсекунды — вдвое быстрее прежнего.
        transfer._speed_time = time.monotonic() - 0.5
        transfer._last = 0.0
        transfer.add(10 * 1048576)
        # Второй отчёт усредняет скорость: 10 и 20 МБ/с дают 15 МБ/с.
        self.assertIn("15.0 МБ/с", values[-1][1])

    def test_source_paths_requires_files(self):
        with self.assertRaises(geofabrik.GeofabrikError) as context:
            geofabrik.source_paths(self.config, [self.first])
        self.assertIn("Подготовить данные", str(context.exception))

    def test_source_paths_prefers_merged_file(self):
        first = geofabrik.local_path(self.config, self.first)
        _dummy_pbf(first)
        merged = geofabrik.merged_path(self.config, [self.first], [first])
        _dummy_pbf(merged)
        with mock.patch.object(geofabrik, "find_osmium", return_value=None):
            paths = geofabrik.source_paths(self.config, [self.first])
        self.assertEqual(paths, [merged])


class GeofabrikSizeLimitTest(unittest.TestCase):
    """Карты крупнее 7 ГБ не предлагаются к скачиванию."""

    def setUp(self):
        geofabrik.clear_caches()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config(qgis_projects_dir=self.directory.name)
        self.small = geofabrik.GeofabrikMap(
            "small", "Small", "", "https://example.org/small.osm.pbf"
        )
        self.huge = geofabrik.GeofabrikMap(
            "huge", "Huge", "", "https://example.org/huge.osm.pbf"
        )

    def _plan(self, huge_size):
        sizes = {self.small.id: 100 * 1024 * 1024, self.huge.id: huge_size}

        def head(url):
            for item in (self.small, self.huge):
                if url == item.url:
                    return True, sizes[item.id], ""
            return False, 0, "404"

        state = mock.Mock()
        state.warning = ""
        state.last_modified = ""
        state.fetched_at = 0
        state.index.required.return_value = [self.small, self.huge]
        state.index.candidates.return_value = [self.small, self.huge]
        state.index.entries = []
        with mock.patch.object(
            geofabrik, "fetch_index", return_value=state
        ), mock.patch.object(geofabrik, "_head_size", side_effect=head):
            return geofabrik.analyse(self.config, BBox(0, 0, 1, 1))

    def test_big_map_is_not_offered(self):
        plan = self._plan(geofabrik.MAX_MAP_SIZE + 1)
        self.assertEqual([item.id for item in plan.maps], ["small"])
        self.assertEqual([item.id for item in plan.extra], [])
        self.assertEqual([item.id for item, _ in plan.skipped], ["huge"])

    def test_map_at_the_limit_is_offered(self):
        plan = self._plan(geofabrik.MAX_MAP_SIZE)
        self.assertEqual(
            sorted(item.id for item in plan.maps + plan.extra), ["huge", "small"]
        )
        self.assertEqual(plan.skipped, [])

    def test_ensure_maps_refuses_big_map(self):
        status = geofabrik.MapStatus(
            map=self.huge, available=True, size=geofabrik.MAX_MAP_SIZE + 1
        )
        with self.assertRaises(geofabrik.GeofabrikError):
            geofabrik.ensure_maps(self.config, [self.huge], statuses=[status])


class GeofabrikSourcesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config(
            qgis_projects_dir=self.directory.name,
            project_name="demo",
            bbox="1,2,3,4",
            array_queries=["water"],
            source_mode="geofabrik",
        )
        self.item = geofabrik.GeofabrikMap(
            "monaco", "Monaco", "",
            "https://example.org/monaco.osm.pbf",
        )

    def tearDown(self):
        self.directory.cleanup()

    def _state(self):
        state = mock.Mock()
        state.warning = ""
        state.index.required.return_value = [self.item]
        return state

    def test_missing_maps_are_downloaded_automatically(self):
        from qgis_xtopo.core import sources

        state = self._state()
        dataset = mock.Mock(warnings=[])
        percents = []
        with mock.patch.object(
            sources.geofabrik, "fetch_index", return_value=state
        ), mock.patch.object(
            sources.geofabrik, "local_ready", return_value=False
        ), mock.patch.object(
            sources.geofabrik, "ensure_maps", return_value=[Path("/tmp/x.pbf")]
        ) as ensure, mock.patch.object(
            sources.geofabrik, "merge_if_available", return_value=""
        ), mock.patch.object(
            sources.geofabrik, "source_paths", return_value=[Path("/tmp/x.pbf")]
        ), mock.patch.object(
            sources.pbf_source, "prepare_dataset", return_value=dataset
        ):
            result = sources.prepare_sources(
                self.config, BBox(1, 2, 3, 4), ["water"],
                percent=lambda value, text: percents.append(value),
            )
        ensure.assert_called_once()
        self.assertIn("percent", ensure.call_args.kwargs)
        self.assertIs(result.dataset, dataset)

    def test_local_maps_do_not_trigger_download(self):
        from qgis_xtopo.core import sources

        state = self._state()
        dataset = mock.Mock(warnings=[])
        with mock.patch.object(
            sources.geofabrik, "fetch_index", return_value=state
        ), mock.patch.object(
            sources.geofabrik, "local_ready", return_value=True
        ), mock.patch.object(
            sources.geofabrik, "ensure_maps"
        ) as ensure, mock.patch.object(
            sources.geofabrik, "source_paths", return_value=[Path("/tmp/x.pbf")]
        ), mock.patch.object(
            sources.pbf_source, "prepare_dataset", return_value=dataset
        ):
            sources.prepare_sources(self.config, BBox(1, 2, 3, 4), ["water"])
        ensure.assert_not_called()

    def test_merged_file_failure_falls_back_to_separate_maps(self):
        """Склеенный файл с повторяющимися id не читается — карты берём по отдельности."""
        from qgis_xtopo.core import sources

        state = self._state()
        dataset = mock.Mock(warnings=[])
        separate = [Path("/tmp/a.osm.pbf"), Path("/tmp/b.osm.pbf")]

        def prepare_dataset(config, bbox, names, **kwargs):
            if kwargs.get("sources") == [Path("/tmp/merged.osm.pbf")]:
                raise RuntimeError("Non increasing node id")
            return dataset

        with mock.patch.object(
            sources.geofabrik, "fetch_index", return_value=state
        ), mock.patch.object(
            sources.geofabrik, "local_ready", return_value=True
        ), mock.patch.object(
            sources.geofabrik, "source_paths",
            return_value=[Path("/tmp/merged.osm.pbf")],
        ), mock.patch.object(
            sources.geofabrik, "local_maps", return_value=separate
        ), mock.patch.object(
            sources.pbf_source, "prepare_dataset", side_effect=prepare_dataset
        ) as prepare:
            result = sources.prepare_sources(
                self.config, BBox(1, 2, 3, 4), ["water"],
            )
        self.assertIs(result.dataset, dataset)
        self.assertEqual(prepare.call_args.kwargs["sources"], separate)
        self.assertTrue(
            any("по отдельности" in warning for warning in result.warnings),
            result.warnings,
        )

    def test_user_selection_overrides_auto_detection(self):
        from qgis_xtopo.core import sources

        index = geofabrik._parse_index(INDEX_PAYLOAD)
        config = Config(
            qgis_projects_dir=self.directory.name,
            project_name="demo",
            bbox="1,2,3,4",
            array_queries=["water"],
            source_mode="geofabrik",
            geofabrik_maps=["region"],
        )
        maps = sources._selected_maps(index, config, BBox(0.5, 0.5, 1.0, 1.0))
        self.assertEqual([item.id for item in maps], ["region"])

    def test_empty_selection_falls_back_to_auto_detection(self):
        from qgis_xtopo.core import sources

        index = geofabrik._parse_index(INDEX_PAYLOAD)
        maps = sources._selected_maps(index, self.config, BBox(0.5, 0.5, 1.0, 1.0))
        self.assertEqual([item.id for item in maps], ["region"])

    def test_unknown_map_is_reported(self):
        from qgis_xtopo.core import sources

        index = geofabrik._parse_index(INDEX_PAYLOAD)
        config = Config(
            qgis_projects_dir=self.directory.name,
            project_name="demo",
            bbox="1,2,3,4",
            array_queries=["water"],
            source_mode="geofabrik",
            geofabrik_maps=["atlantis"],
        )
        with self.assertRaises(sources.SourceError) as context:
            sources._selected_maps(index, config, BBox(0.5, 0.5, 1.0, 1.0))
        self.assertIn("atlantis", str(context.exception))


class GeofabrikConfigTest(unittest.TestCase):
    def test_default_mode(self):
        self.assertEqual(Config().source_mode, "geofabrik")

    def test_validate_geofabrik(self):
        Config(
            project_name="demo",
            bbox="1,2,3,4",
            array_queries=["water"],
            source_mode="geofabrik",
        ).validate()

    def test_cache_directory(self):
        config = Config(qgis_projects_dir="/tmp/projects")
        self.assertEqual(
            config.geofabrik_dir,
            Path("/tmp/projects") / "qgisxtopo-cache" / "geofabrik",
        )


if __name__ == "__main__":
    unittest.main()
