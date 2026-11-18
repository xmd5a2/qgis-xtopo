import importlib.util
import io
import itertools
import json
import math
import os
import socket
import struct
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None

from qgis_xtopo.core import disk_space, extracts, slice_estimate
from qgis_xtopo.core.bbox import BBox
from qgis_xtopo.core.config import Config


LISTING = """
<html><body>
<a href="../">Parent</a>
<a href="europe/">europe/</a>
<a href="russia/">russia/</a>
<a href="france-latest.osm.pbf">france-latest.osm.pbf</a>
<a href="france.osm.pbf.md5">france.osm.pbf.md5</a>
<a href="https://example.org/other/">внешняя ссылка</a>
</body></html>
"""

POLY = """
polygon
1
	35.31	50.96
	35.42	50.96
	35.42	51.10
	35.31	51.10
	35.31	50.96
"""

POLY_WITH_HOLE = """
polygon
1
	10.0	10.0
	20.0	10.0
	20.0	20.0
	10.0	20.0
	10.0	10.0
!1
	12.0	12.0
	14.0	12.0
	14.0	14.0
	12.0	14.0
	12.0	12.0
"""

GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"id": "one"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
            },
        }
    ],
}


class ListingTest(unittest.TestCase):
    def test_parse_listing(self):
        directories, files = extracts.parse_listing(LISTING)
        self.assertEqual(directories, ["europe", "russia"])
        self.assertEqual(
            files, ["france-latest.osm.pbf", "france.osm.pbf.md5"]
        )

    def test_names(self):
        self.assertEqual(extracts.humanize("belgorod_oblast"), "Belgorod Oblast")
        self.assertEqual(extracts.parent_slug("a/b/c"), "a/b")
        self.assertEqual(extracts.parent_slug("russia"), "")
        self.assertEqual(extracts.normalize_name("Central Federal District"),
                         "centralfederaldistrict")


class GeometryTest(unittest.TestCase):
    def test_geojson_feature_collection(self):
        geometry = extracts.geometry_from_geojson(GEOJSON)
        self.assertIsNotNone(geometry)
        self.assertAlmostEqual(geometry.boundingBox().xMinimum(), 0.0, places=6)
        self.assertAlmostEqual(geometry.boundingBox().xMaximum(), 10.0, places=6)

    def test_geojson_broken(self):
        self.assertIsNone(extracts.geometry_from_geojson({"type": "Feature"}))
        self.assertIsNone(extracts.geometry_from_geojson("не json"))

    def test_poly(self):
        geometry = extracts.geometry_from_poly(POLY)
        self.assertIsNotNone(geometry)
        box = geometry.boundingBox()
        self.assertAlmostEqual(box.xMinimum(), 35.31, places=6)
        self.assertAlmostEqual(box.yMaximum(), 51.10, places=6)

    def test_poly_with_hole(self):
        geometry = extracts.geometry_from_poly(POLY_WITH_HOLE)
        self.assertIsNotNone(geometry)
        self.assertAlmostEqual(geometry.boundingBox().xMinimum(), 10.0, places=6)
        self.assertAlmostEqual(geometry.boundingBox().xMaximum(), 20.0, places=6)

    def test_poly_empty(self):
        self.assertIsNone(extracts.geometry_from_poly(""))


class MovisdaProviderTest(unittest.TestCase):
    def test_url_from_code(self):
        provider = extracts.MOVISDA
        self.assertEqual(
            provider._url("RU-BEL"),
            "https://osm.download.movisda.io/admin/RU-BEL-latest.osm.pbf",
        )

    def test_catalogue_uses_index_links_and_lazy_geometry(self):
        feature = {
            "type": "Feature",
            "properties": {
                "id": "FR-ARA",
                "parent": "FR",
                "name": "Auvergne-Rhone-Alpes",
                "names": {"ru": "Овернь — Рона — Альпы"},
                "bytes": 759841211,
                "urls": {
                    "pbf": "https://osm.download.movisda.io/admin/"
                           "FR-ARA-latest.osm.pbf",
                    "geojson": "https://osm.download.movisda.io/admin/"
                               "FR-ARA-latest.geojson",
                },
            },
            "geometry": _square(1, 45, 3, 47),
        }
        payload = {"type": "FeatureCollection", "features": [feature]}

        def fake_download(url, destination, **_kwargs):
            Path(destination).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return Path(destination)

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory, project_name="movisda"
            )
            with mock.patch.object(
                extracts, "download_file", side_effect=fake_download
            ):
                catalogue = extracts.MOVISDA.catalogue(config)
        self.assertEqual(len(catalogue.maps), 1)
        item = catalogue.maps[0]
        self.assertEqual(item.id, "FR-ARA")
        self.assertEqual(item.parent, "FR")
        self.assertEqual(item.name, "Овернь — Рона — Альпы")
        self.assertEqual(
            item.url,
            "https://osm.download.movisda.io/admin/FR-ARA-latest.osm.pbf",
        )
        self.assertEqual(item.size, 759841211)
        self.assertIsNone(item.geometry)
        self.assertIsNotNone(item.boundary)
        geometry = extracts.MOVISDA.geometry(config, item, None)
        self.assertIsNotNone(geometry)
        self.assertAlmostEqual(geometry.boundingBox().xMinimum(), 1.0, places=6)
        self.assertIs(item.geometry, geometry)

    def test_invalidate_cache_handles_any_index_name(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory, project_name="movisda"
            )
            folder = config.extracts_dir / "movisda"
            folder.mkdir(parents=True)
            for name in ("index.geojson", "index.json"):
                path = folder / name
                path.write_text("{}", encoding="utf-8")
                path.with_name(name + ".meta").write_text(
                    '{"fetched_at": 123}', encoding="utf-8"
                )
            extracts.invalidate_cache(config, "movisda")
            for name in ("index.geojson", "index.json"):
                meta = json.loads(
                    (folder / (name + ".meta")).read_text(encoding="utf-8")
                )
                self.assertEqual(meta["fetched_at"], 0)
            # Каталог без кэша не должен ломать обновление.
            extracts.invalidate_cache(config, "osmfr")


def _square(x1, y1, x2, y2):
    return {
        "type": "Polygon",
        "coordinates": [
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]
        ],
    }


class WalkTest(unittest.TestCase):
    def test_walks_listing_tree(self):
        base = extracts.OSMFR.BASE
        pages = {
            base: (
                '<a href="../">../</a>'
                '<a href="europe/">europe/</a>'
                '<a href="merge/">merge/</a>'
                '<a href="world-latest.osm.pbf">world-latest.osm.pbf</a>'
                '<a href="world.osm.pbf.md5">world.osm.pbf.md5</a>'
            ),
            base + "europe/": (
                '<a href="france/">france/</a>'
                '<a href="france-latest.osm.pbf">france-latest.osm.pbf</a>'
            ),
            base + "europe/france/": (
                '<a href="alsace-latest.osm.pbf">alsace-latest.osm.pbf</a>'
            ),
        }
        messages = []

        def fetch(url, **_kwargs):
            return pages[url].encode("utf-8")

        with mock.patch.object(extracts, "fetch_bytes", side_effect=fetch):
            entries = extracts.OSMFR._walk(progress=messages.append)
        self.assertEqual(
            [entry["id"] for entry in entries],
            ["europe/france", "europe/france/alsace", "world"],
        )
        self.assertEqual(
            entries[1]["url"],
            base + "europe/france/alsace-latest.osm.pbf",
        )
        self.assertTrue(messages)


class BuildIndexTest(unittest.TestCase):
    """Выбор выгрузок по охвату: границы подставляются заглушкой."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(
            qgis_projects_dir=self.temporary.name, project_name="test"
        )
        self.maps = [
            extracts.ExtractMap(
                id="europe", name="Europe", parent="", url="http://example.org/e",
                namespace="geo2day",
            ),
            extracts.ExtractMap(
                id="europe/france", name="France", parent="europe",
                url="http://example.org/f", namespace="geo2day",
            ),
            extracts.ExtractMap(
                id="europe/france/alsace", name="Alsace", parent="europe/france",
                url="http://example.org/a", namespace="geo2day",
            ),
            extracts.ExtractMap(
                id="europe/germany", name="Germany", parent="europe",
                url="http://example.org/g", namespace="geo2day",
            ),
        ]
        self.geometries = {
            "europe": _square(0, 0, 30, 60),
            "france": _square(0, 40, 10, 50),
            "alsace": _square(1, 45, 3, 47),
            "germany": _square(8, 47, 20, 55),
        }
        catalogue = extracts.Catalogue(maps=list(self.maps), fetched_at=1.0)
        patch_catalogue = mock.patch.object(
            extracts.GEO2DAY, "catalogue", return_value=catalogue
        )
        patch_geometry = mock.patch.object(
            extracts.Boundaries,
            "geometry",
            side_effect=lambda key, **kwargs: extracts.geometry_from_geojson(
                self.geometries[key]
            ),
        )
        patch_catalogue.start()
        patch_geometry.start()
        self.addCleanup(patch_catalogue.stop)
        self.addCleanup(patch_geometry.stop)

    def test_picks_smallest_region(self):
        bbox = BBox(1.5, 45.5, 2.5, 46.5)
        index, _catalogue = extracts.build_index(
            self.config, "geo2day", bbox
        )
        required = [item.id for item in index.required(bbox)]
        self.assertEqual(required, ["europe/france/alsace"])

    def test_lists_nearby_regions(self):
        bbox = BBox(1.5, 45.5, 2.5, 46.5)
        index, _catalogue = extracts.build_index(
            self.config, "geo2day", bbox
        )
        candidates = [item.id for item in index.candidates(bbox)]
        self.assertIn("europe/france", candidates)
        self.assertIn("europe/france/alsace", candidates)
        self.assertNotIn("europe/germany", candidates)

    def test_country_roots_descend_into_regions(self):
        """У movisda корень — страна: сначала проверяются страны, потом области."""
        maps = [
            extracts.ExtractMap(
                id="FR", name="Франция", parent="", url="http://example.org/fr",
                namespace="movisda",
            ),
            extracts.ExtractMap(
                id="FR-ARA", name="Овернь", parent="FR",
                url="http://example.org/fr-ara", namespace="movisda",
            ),
            extracts.ExtractMap(
                id="IT", name="Италия", parent="", url="http://example.org/it",
                namespace="movisda",
            ),
            extracts.ExtractMap(
                id="IT-23", name="Валле-д’Аоста", parent="IT",
                url="http://example.org/it-23", namespace="movisda",
            ),
        ]
        geometries = {
            "FR": _square(0, 40, 10, 50),
            "FR-ARA": _square(1, 45, 3, 47),
            "IT": _square(7, 44, 19, 47),
            "IT-23": _square(6.5, 45.5, 7.5, 46),
        }
        catalogue = extracts.Catalogue(maps=maps, fetched_at=1.0)
        bbox = BBox(1.5, 45.5, 2.5, 46.5)
        with mock.patch.object(
            extracts.MOVISDA, "catalogue", return_value=catalogue
        ), mock.patch.object(
            extracts.MOVISDA,
            "geometry",
            side_effect=lambda config, item, boundaries, progress=None,
                cancel=None: extracts.geometry_from_geojson(
                    geometries[item.id]
                ),
        ):
            index, _catalogue = extracts.build_index(
                self.config, "movisda", bbox
            )
        self.assertEqual(
            [item.id for item in index.required(bbox)], ["FR-ARA"]
        )
        self.assertNotIn(
            "IT-23", [item.id for item in index.candidates(bbox)]
        )


class _FakeResponse:
    """Ответ сервера, который отдаёт данные кусками и обрывается."""

    def __init__(self, payload, status=200, cut=False, headers=None):
        self.payload = payload
        self.status = status
        self.cut = cut
        self.offset = 0
        self.headers = headers or {"Content-Length": str(len(payload))}

    def read1(self, size):
        if self.offset >= len(self.payload):
            if self.cut:
                raise socket.timeout("server stopped")
            return b""
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def read(self, size):
        return self.read1(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class DownloadTest(unittest.TestCase):
    """Загрузка по частям: сервер отдаёт ответ обрывками."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "map.osm.pbf"

    def _pbf(self, size):
        """PBF с блоком заголовка и блоком данных."""
        header = b"\x0a\x09OSMHeader"
        body = b"\x00" * max(0, size - len(header) - 4)
        return (
            struct.pack(">I", len(header)) + header
            + struct.pack(">I", len(body)) + body
        )

    def test_resumes_after_partial_response(self):
        payload = self._pbf(40000)
        calls = []

        def fake_urlopen(request, timeout=None):
            span = request.headers.get("Range")
            calls.append(span)
            start = 0
            if span:
                start = int(span.split("=")[1].split("-")[0])
            # Каждый ответ отдаёт кусок и обрывается.
            response = _FakeResponse(payload[start:start + 16384], cut=True)
            if span:
                # Сервер, который уважает Range, отвечает 206.
                response.status = 206
            return response

        with mock.patch.object(extracts, "_urlopen", side_effect=fake_urlopen):
            with mock.patch.object(extracts, "head_size", return_value=(True, len(payload), "")):
                result = extracts.download_file(
                    "http://example.org/map.osm.pbf", self.destination
                )
        self.assertTrue(result.exists())
        self.assertEqual(result.stat().st_size, len(payload))
        self.assertGreater(len(calls), 1)

    def test_rejects_non_pbf(self):
        with mock.patch.object(extracts, "_urlopen", return_value=_FakeResponse(b"not pbf")):
            with mock.patch.object(extracts, "head_size", return_value=(True, 7, "")):
                with self.assertRaises(extracts.ExtractError):
                    extracts.download_file(
                        "http://example.org/map.osm.pbf", self.destination
                    )

    def test_allows_json_when_pbf_check_disabled(self):
        payload = b'{"type": "FeatureCollection", "features": []}'
        with mock.patch.object(
            extracts, "_urlopen", return_value=_FakeResponse(payload)
        ), mock.patch.object(
            extracts, "head_size", return_value=(True, len(payload), "")
        ):
            result = extracts.download_file(
                "http://example.org/index.geojson", self.destination,
                check_pbf=False,
            )
        self.assertEqual(result.read_bytes(), payload)

    def test_progress_is_reported_less_often_than_every_megabyte(self):
        payload = b"\x00" * (8 * 1024 * 1024)
        messages = []
        with mock.patch.object(
            extracts, "_urlopen", return_value=_FakeResponse(payload)
        ), mock.patch.object(
            extracts, "head_size", return_value=(True, len(payload), "")
        ):
            extracts.download_file(
                "http://example.org/map.osm.pbf", self.destination,
                progress=messages.append, check_pbf=False,
            )
        self.assertTrue(messages)
        self.assertLessEqual(len(messages), 20)

    def test_download_is_refused_without_free_space(self):
        """Скачивать файл, для которого нет места, бессмысленно."""
        payload = self._pbf(40000)
        with mock.patch.object(
            extracts, "_urlopen", return_value=_FakeResponse(payload)
        ), mock.patch.object(
            extracts, "head_size", return_value=(True, len(payload), "")
        ), mock.patch.object(
            disk_space, "free_bytes", return_value=1024
        ):
            with self.assertRaises(extracts.ExtractError) as context:
                extracts.download_file(
                    "http://example.org/map.osm.pbf", self.destination
                )
        self.assertIn("Недостаточно места", str(context.exception))

    def test_truncated_download_is_not_published(self):
        """Сервер обещал больше, чем прислал: файл не публикуется."""
        payload = self._pbf(40000)

        def fake_urlopen(request, timeout=None):
            return _FakeResponse(
                payload[:100],
                headers={"Content-Length": str(len(payload))},
            )

        with mock.patch.object(
            extracts, "_urlopen", side_effect=fake_urlopen
        ), mock.patch.object(
            extracts, "head_size", return_value=(True, len(payload), "")
        ):
            with self.assertRaises(extracts.ExtractError):
                extracts.download_file(
                    "http://example.org/map.osm.pbf", self.destination
                )
        self.assertFalse(self.destination.exists())

    def test_pbf_without_data_block_is_rejected(self):
        """Только заголовка мало: это оборванная загрузка."""
        header = b"\x0a\x09OSMHeader"
        broken = Path(self.temporary.name) / "broken.osm.pbf"
        broken.write_bytes(struct.pack(">I", len(header)) + header)
        self.assertFalse(extracts.looks_like_pbf(broken))


class BoundariesTest(unittest.TestCase):
    """Границы регионов: неоднозначные имена и гонка за индексом."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(
            qgis_projects_dir=self.temporary.name, project_name="test"
        )

    def _boundaries(self, slugs):
        boundaries = extracts.Boundaries(self.config)
        boundaries._regions = {
            slug: {"slug": slug} for slug in slugs
        }
        boundaries._loaded = True
        return boundaries

    def test_full_slug_match_wins(self):
        boundaries = self._boundaries(["europe/georgia", "asia/georgia"])
        region = boundaries.region("europegeorgia")
        self.assertEqual(region["slug"], "europe/georgia")

    def test_ambiguous_name_is_not_chosen(self):
        """Имя «georgia» есть и в Азии, и в Европе: чужую границу
        подставлять нельзя."""
        boundaries = self._boundaries(["europe/georgia", "asia/georgia"])
        with self.assertWarns(UserWarning):
            self.assertIsNone(boundaries.region("georgia"))

    def test_unique_last_segment_is_found(self):
        boundaries = self._boundaries(["europe/georgia"])
        region = boundaries.region("georgia")
        self.assertEqual(region["slug"], "europe/georgia")

    def test_index_is_loaded_once(self):
        """Восемь потоков не должны качать индекс и писать в один файл."""
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            time.sleep(0.05)
            return {"regions": [{"slug": "europe/france"}]}

        with mock.patch.object(extracts, "fetch_json", side_effect=fake_fetch):
            boundaries = extracts.Boundaries(self.config)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _index: boundaries._index(), range(4)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(results), 4)


class StatusesTest(unittest.TestCase):
    """Состояние выгрузок: непроверенная не значит доступная."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(
            qgis_projects_dir=self.temporary.name, project_name="test"
        )

    def test_unchecked_maps_are_not_available(self):
        maps = [
            extracts.ExtractMap(
                id=f"m{index}", name="M", parent="",
                url=f"http://example.org/m{index}",
            )
            for index in range(extracts.MAX_HEAD_CHECKS + 3)
        ]
        catalogue = extracts.Catalogue(maps=maps)
        with mock.patch.object(
            extracts, "head_size", return_value=(True, 10, "")
        ):
            statuses = extracts._statuses(self.config, maps, catalogue)
        self.assertTrue(statuses[0].available)
        self.assertFalse(statuses[-1].available)
        self.assertIn("не проверено", statuses[-1].error)


class SliceOsmTest(unittest.TestCase):
    """Вырезка по охвату: заказ, ожидание и разбор ответа."""

    def test_submit(self):
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=None):
                return b'"uuid-1234"'

        def fake_open(url, **kwargs):
            captured["url"] = url
            captured["data"] = kwargs.get("data")
            captured["method"] = kwargs.get("method")
            return _Response()

        with mock.patch.object(extracts, "_open", side_effect=fake_open):
            uuid = extracts.SliceOsm().submit(BBox(1, 2, 3, 4))
        self.assertEqual(uuid, "uuid-1234")
        self.assertEqual(captured["method"], "POST")
        payload = extracts.json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(payload["RegionType"], "bbox")
        # Сервис ждёт min_lat, min_lon, max_lat, max_lon.
        self.assertEqual(payload["RegionData"], [2.0, 1.0, 4.0, 3.0])

    def test_submit_reports_service_error_body(self):
        error = urllib.error.HTTPError(
            extracts.SliceOsm.API, 400, "Bad Request", {},
            io.BytesIO(b"Error: the limit of nodes was exceeded."),
        )
        with mock.patch.object(extracts, "_open", side_effect=error):
            with self.assertRaises(extracts.ExtractError) as raised:
                extracts.SliceOsm().submit(BBox(1, 2, 3, 4))
        self.assertIn("limit of nodes", str(raised.exception))

    def test_is_complete(self):
        client = extracts.SliceOsm()
        self.assertTrue(client.is_complete({"Complete": "true"}))
        self.assertTrue(client.is_complete({"Complete": True}))
        self.assertFalse(client.is_complete({"Complete": "false"}))

    def test_size_and_share(self):
        client = extracts.SliceOsm()
        payload = {
            "NodesTotal": 1000, "NodesProg": 250, "SizeBytes": 4096,
        }
        self.assertEqual(client.size_bytes(payload), 4096)
        self.assertAlmostEqual(client.share(payload), 0.25)
        # До готовности размер неизвестен, и прогресс нулевой.
        self.assertEqual(client.size_bytes({"SizeBytes": 0}), 0)
        self.assertEqual(client.share({"NodesTotal": 0}), 0.0)

    def test_progress_text_estimates_the_rest(self):
        client = extracts.SliceOsm()
        payload = {"NodesTotal": 1000, "NodesProg": 500}
        text = client._progress_text(payload, 60.0)
        self.assertIn("50%", text)
        self.assertIn("1 мин", text)
        # Пока сервис не сказал, сколько узлов ждать, оценки нет.
        self.assertEqual(
            client._progress_text({"NodesTotal": 0}, 60.0),
            "SliceOSM: сборка выреза…",
        )

    def test_service_status(self):
        client = extracts.SliceOsm()
        with mock.patch.object(
            extracts, "fetch_json", return_value={"QueueSize": 2},
        ):
            self.assertEqual(client.service_status()["QueueSize"], 2)

    def test_wait_keeps_waiting_while_service_moves(self):
        """Пока сервис продвигается, ожидание не обрывается по таймауту."""
        client = extracts.SliceOsm()
        payloads = [
            {"CellsProg": 10, "NodesProg": 0, "Complete": False},
            {"CellsProg": 10, "NodesProg": 100, "Complete": False},
            {"CellsProg": 10, "NodesProg": 200, "Complete": True},
        ]
        clock = itertools.count(0.0)
        with mock.patch.object(
            client, "status", side_effect=payloads
        ), mock.patch.object(
            extracts.time, "sleep"
        ), mock.patch.object(
            extracts.time, "monotonic",
            side_effect=lambda: next(clock) * 0.01,
        ):
            payload = client.wait("uuid", timeout=1)
        self.assertTrue(client.is_complete(payload))

    def test_wait_does_not_repeat_identical_progress(self):
        """Неизменная оценка не попадает в журнал дважды подряд."""
        client = extracts.SliceOsm()
        payloads = [
            {"NodesTotal": 100, "NodesProg": 99, "Complete": False},
            {"NodesTotal": 100, "NodesProg": 99, "Complete": False},
            {"NodesTotal": 100, "NodesProg": 99, "Complete": False},
            {"Complete": True},
        ]
        messages = []
        with mock.patch.object(
            client, "status", side_effect=payloads
        ), mock.patch.object(
            extracts.time, "sleep"
        ), mock.patch.object(
            extracts.time, "monotonic", return_value=0.0,
        ):
            client.wait("uuid", timeout=1, progress=messages.append)
        self.assertEqual(len(messages), 1)
        self.assertIn("99%", messages[0])


class SliceEstimateTest(unittest.TestCase):
    """Предварительная оценка выреза до обращения к сервису."""

    class _Image:
        """Карта плотности с одним ненулевым пикселем z12."""

        width = 4096
        height = 4096

        def __init__(self, value):
            self.value = value

        def pixel(self, x, y):
            if (x, y) == (2048, 2048):
                return self.value >> 8, self.value & 0xFF
            return 0, 0

        def block(self, x, y, size):
            if x <= 2048 < x + size and y <= 2048 < y + size:
                return self.value
            return 0

    @staticmethod
    def _tile_bbox(x, y, zoom=12):
        count = 1 << zoom
        lon_min = x / count * 360.0 - 180.0
        lon_max = (x + 1) / count * 360.0 - 180.0
        lat_max = math.degrees(
            math.atan(math.sinh(math.pi * (1 - 2 * y / count)))
        )
        lat_min = math.degrees(
            math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / count)))
        )
        return BBox(lon_min, lat_min, lon_max, lat_max)

    def test_estimate_nodes_for_one_z12_pixel(self):
        bbox = self._tile_bbox(2048, 2048)
        self.assertEqual(
            slice_estimate.estimate_nodes(bbox, self._Image(1000)),
            1000 * slice_estimate.NODE_SCALE,
        )

    def test_tile_bounds_cover_whole_world(self):
        bbox = BBox(-180.0, -85.0, 180.0, 85.0)
        self.assertEqual(slice_estimate._tile_bounds(bbox, 0), (0, 0, 0, 0))
        zoom, bounds = slice_estimate._covering(bbox)
        self.assertEqual(zoom, 4)
        self.assertEqual(slice_estimate._tile_count(bounds), 256)

    def test_covering_stays_within_tile_limit(self):
        """Берётся самый подробный зум, у которого тайлов не больше лимита."""
        bbox = BBox(13.0, 52.0, 14.0, 53.0)
        zoom, bounds = slice_estimate._covering(bbox)
        self.assertLessEqual(
            slice_estimate._tile_count(bounds), slice_estimate.MAX_TILES
        )
        self.assertEqual(zoom, 12)

    def test_estimate_size_and_build_time_match_measurements(self):
        # Замер сервиса для этой рамки: 2 973 537 узлов, 23,9 МБ, 38,1 с.
        bbox = BBox(40.1649638, 55.1542466, 42.1219571, 56.2688148)
        result = slice_estimate.estimate(
            bbox, nodes=2_973_537,
            service={"QueueSize": 0, "NodesLimit": 100_000_000},
            cpu=1.0, workers=8,
        )
        self.assertAlmostEqual(
            result.size_bytes, 23_883_693, delta=3_000_000
        )
        self.assertAlmostEqual(result.build_seconds, 38.05, delta=1.5)
        self.assertFalse(result.over_limit)
        self.assertEqual(result.queue_seconds, 0.0)

    def test_cached_cutout_skips_build_and_download(self):
        bbox = BBox(1.0, 2.0, 3.0, 4.0)
        result = slice_estimate.estimate(
            bbox, cached_bytes=123_456,
            service={"QueueSize": 5, "NodesLimit": 100},
        )
        self.assertEqual(result.size_bytes, 123_456)
        self.assertEqual(result.build_seconds, 0.0)
        self.assertEqual(result.queue_seconds, 0.0)
        self.assertEqual(result.download_seconds, 0.0)
        self.assertGreater(result.prepare_seconds, 0.0)

    def test_over_limit_and_queue(self):
        bbox = BBox(1.0, 2.0, 3.0, 4.0)
        result = slice_estimate.estimate(
            bbox, nodes=200,
            service={"QueueSize": 2, "NodesLimit": 100},
        )
        self.assertTrue(result.over_limit)
        self.assertEqual(result.queue_size, 2)
        self.assertGreater(result.queue_seconds, 0.0)

    def test_rgb_image_block(self):
        data = bytearray(2 * 12)
        data[12 + 3] = 0x12
        data[12 + 4] = 0x34
        image = slice_estimate.RgbImage(4, 2, 12, bytes(data))
        self.assertEqual(image.block(1, 1, 1), 0x1234)
        self.assertEqual(image.block(0, 0, 2), 0x1234)

    def test_nodes_image_is_cached_and_falls_back_to_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory, project_name="slice"
            )
            payload = (
                slice_estimate._PNG_SIGNATURE + b"x" * 100
                + b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )

            with mock.patch.object(
                slice_estimate, "NODES_IMAGE_MIN_SIZE", 64,
            ), mock.patch.object(
                slice_estimate, "NODES_IMAGE_TTL", 3600,
            ), mock.patch.object(
                extracts, "fetch_bytes", return_value=payload,
            ) as download:
                path = slice_estimate.ensure_nodes_image(config)
                self.assertTrue(slice_estimate._valid_png(path))
                self.assertEqual(download.call_count, 1)
                # Свежий кэш повторно не скачивается.
                slice_estimate.ensure_nodes_image(config)
                self.assertEqual(download.call_count, 1)

                # Принудительное обновление не ждёт конца срока кэша.
                slice_estimate.ensure_nodes_image(config, force=True)
                self.assertEqual(download.call_count, 2)

                old = time.time() - 7200
                os.utime(path, (old, old))
                with mock.patch.object(
                    extracts, "fetch_bytes",
                    side_effect=extracts.ExtractError("offline"),
                ):
                    self.assertEqual(
                        slice_estimate.ensure_nodes_image(config), path
                    )

                path.unlink()
                with mock.patch.object(
                    extracts, "fetch_bytes", return_value=b"broken",
                ):
                    with self.assertRaises(extracts.ExtractError):
                        slice_estimate.ensure_nodes_image(config)
                self.assertFalse(path.exists())

    def test_slice_cache_path_uses_bbox_hash(self):
        import hashlib

        bbox = BBox(1.0, 2.0, 3.0, 4.0)
        expected = hashlib.sha1(bbox.as_csv().encode("utf-8")).hexdigest()[:12]
        self.assertEqual(
            slice_estimate.slice_cache_path(Config(), bbox).name,
            f"slice_{expected}.osm.pbf",
        )


@unittest.skipUnless(
    os.environ.get("QGIS_XTOPO_TEST_PROVIDERS"),
    "QGIS_XTOPO_TEST_PROVIDERS is not set",
)
class LiveProvidersTest(unittest.TestCase):
    """Живая проверка провайдеров: нужна сеть."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(
            qgis_projects_dir=self.temporary.name, project_name="live"
        )
        self.bbox = BBox(36.0, 50.5, 36.6, 51.0)

    def test_geo2day_index(self):
        index, catalogue = extracts.build_index(
            self.config, "geo2day", self.bbox
        )
        self.assertGreater(len(catalogue.maps), 500)
        required = [item.id for item in index.required(self.bbox)]
        self.assertTrue(
            any("belgorod" in item for item in required), required
        )

    def test_osmfr_index(self):
        index, catalogue = extracts.build_index(
            self.config, "osmfr", self.bbox
        )
        self.assertGreater(len(catalogue.maps), 100)
        required = [item.id for item in index.required(self.bbox)]
        self.assertTrue(
            any("belgorod" in item for item in required), required
        )


if __name__ == "__main__":
    unittest.main()
