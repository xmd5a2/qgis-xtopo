import importlib.util
import os
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None

from qgis_xtopo.core import extracts
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


class MovisdaUrlTest(unittest.TestCase):
    def test_url_from_code(self):
        provider = extracts.MOVISDA
        self.assertEqual(
            provider._url("RU-BEL"),
            "https://osm.download.movisda.io/pbf/admin/europe/ru/bel.osm.pbf",
        )
        self.assertEqual(
            provider._url("EG"),
            "https://osm.download.movisda.io/pbf/admin/eg/eg.osm.pbf",
        )

    def test_url_from_slug(self):
        provider = extracts.MOVISDA
        self.assertEqual(
            provider._url_from_slug("europe/france"),
            "https://osm.download.movisda.io/pbf/admin/europe/france.osm.pbf",
        )
        self.assertEqual(
            provider._url_from_slug(
                "russia/central_federal_district/belgorod_oblast"
            ),
            "https://osm.download.movisda.io/pbf/admin/europe/russia/"
            "central_federal_district/belgorod_oblast.osm.pbf",
        )


def _square(x1, y1, x2, y2):
    return {
        "type": "Polygon",
        "coordinates": [
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]
        ],
    }


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


class _FakeResponse:
    """Ответ сервера, который отдаёт данные кусками и обрывается."""

    def __init__(self, payload, status=200, cut=False):
        self.payload = payload
        self.status = status
        self.cut = cut
        self.offset = 0
        self.headers = {"Content-Length": str(len(payload))}

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
        header = b"\x00\x00\x00\x0d\x0a\x09OSMHeader"
        body = b"\x00" * max(0, size - len(header))
        return header + body

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


class SliceOsmTest(unittest.TestCase):
    """Вырез по рамке: заказ, ожидание и разбор ответа."""

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

    def test_is_complete(self):
        client = extracts.SliceOsm()
        self.assertTrue(client.is_complete({"Complete": "true"}))
        self.assertTrue(client.is_complete({"Complete": True}))
        self.assertFalse(client.is_complete({"Complete": "false"}))


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
