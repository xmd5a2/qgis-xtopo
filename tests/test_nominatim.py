import json
import threading
import unittest
import urllib.error
from unittest import mock

HAS_QGIS = True


class FakeResponse:
    """Ответ urllib: читается как файл и закрывается."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class NominatimTest(unittest.TestCase):
    def setUp(self):
        from qgis_xtopo.core import nominatim

        self.nominatim = nominatim
        nominatim.clear_cache()
        # Пауза между запросами в тестах не нужна.
        self.interval = nominatim.REQUEST_INTERVAL
        nominatim.REQUEST_INTERVAL = 0.0

    def tearDown(self):
        self.nominatim.REQUEST_INTERVAL = self.interval
        self.nominatim.clear_cache()

    def _response(self, payload):
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return FakeResponse(payload)

        return fake_urlopen, calls

    def test_search_parses_result(self):
        payload = json.dumps(
            [
                {
                    "display_name": "Кисловодск, Россия",
                    "lat": "43.9055311",
                    "lon": "42.7157283",
                    "boundingbox": [
                        "43.8472332", "43.9599627",
                        "42.6598370", "42.7922094",
                    ],
                    "type": "city",
                    "osm_type": "relation",
                    "osm_id": 3934,
                }
            ]
        ).encode()
        fake_urlopen, calls = self._response(payload)
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            places = self.nominatim.search("Кисловодск")
        self.assertEqual(len(places), 1)
        place = places[0]
        self.assertEqual(place.name, "Кисловодск, Россия")
        self.assertAlmostEqual(place.lat, 43.9055311, places=5)
        self.assertAlmostEqual(place.lon, 42.7157283, places=5)
        self.assertEqual(place.kind, "city")
        self.assertEqual(place.osm_id, 3934)
        self.assertEqual(
            place.bounds(),
            (42.6598370, 43.8472332, 42.7922094, 43.9599627),
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("q=", calls[0])
        self.assertIn("nominatim.openstreetmap.org", calls[0])

    def test_search_without_bounds(self):
        payload = json.dumps(
            [{"display_name": "Дом", "lat": "10.5", "lon": "20.25"}]
        ).encode()
        fake_urlopen, _calls = self._response(payload)
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            places = self.nominatim.search("Дом")
        self.assertEqual(len(places), 1)
        self.assertFalse(places[0].has_bounds)
        self.assertIsNone(places[0].bounds())

    def test_search_uses_cache(self):
        payload = json.dumps(
            [{"display_name": "Дом", "lat": "10.5", "lon": "20.25"}]
        ).encode()
        fake_urlopen, calls = self._response(payload)
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            first = self.nominatim.search("Дом")
            second = self.nominatim.search("дом")
        self.assertEqual(len(calls), 1)
        self.assertEqual(first[0].name, second[0].name)

    def test_empty_query_does_not_request(self):
        fake_urlopen, calls = self._response(b"[]")
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            self.assertEqual(self.nominatim.search("   "), [])
        self.assertEqual(calls, [])

    def test_http_error_is_reported(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 503, "Service Unavailable", None, None
            )

        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            with self.assertRaises(self.nominatim.NominatimError):
                self.nominatim.search("Кисловодск")

    def test_broken_answer_is_reported(self):
        fake_urlopen, _calls = self._response("не json".encode())
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            with self.assertRaises(self.nominatim.NominatimError):
                self.nominatim.search("Кисловодск")

    def test_too_many_requests_is_retried(self):
        """Ответ 429 с Retry-After: ждём и повторяем запрос."""
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "Too Many Requests",
                    {"Retry-After": "0"}, None
                )
            return FakeResponse(
                json.dumps(
                    [{"display_name": "Дом", "lat": "10.5", "lon": "20.25"}]
                ).encode()
            )

        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            places = self.nominatim.search("Дом")
        self.assertEqual(len(places), 1)
        self.assertEqual(len(calls), 2)

    def test_retry_after_is_read_from_header(self):
        error = urllib.error.HTTPError(
            "u", 429, "Too Many Requests", {"Retry-After": "7"}, None
        )
        self.assertEqual(self.nominatim._retry_after(error, 0), 7.0)
        # Без заголовка пауза считается по номеру попытки.
        plain = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        self.assertGreater(self.nominatim._retry_after(plain, 1), 0)

    def test_parallel_requests_are_deduplicated(self):
        """Одинаковые параллельные запросы выполняются один раз."""
        calls = []
        started = threading.Event()
        release = threading.Event()

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            started.set()
            release.wait(timeout=5)
            return FakeResponse(
                json.dumps(
                    [{"display_name": "Дом", "lat": "10.5", "lon": "20.25"}]
                ).encode()
            )

        def worker():
            try:
                self.nominatim.search("Дом")
            except Exception:
                pass

        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads:
                thread.start()
            started.wait(timeout=5)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(len(calls), 1)

    def test_cache_entry_expires(self):
        payload = json.dumps(
            [{"display_name": "Дом", "lat": "10.5", "lon": "20.25"}]
        ).encode()
        fake_urlopen, calls = self._response(payload)
        with mock.patch.object(self.nominatim, "_urlopen", fake_urlopen):
            self.nominatim.search("Дом")
            self.nominatim.search("Дом")
            self.assertEqual(len(calls), 1)
            with mock.patch.object(self.nominatim, "CACHE_TTL", 0):
                self.nominatim.search("Дом")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
