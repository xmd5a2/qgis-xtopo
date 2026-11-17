import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qgis_xtopo.core.overpass_client import (
    OverpassClient, OverpassError, valid_osm,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self._position = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        if size < 0:
            data = self.payload[self._position:]
        else:
            data = self.payload[self._position:self._position + size]
        self._position += len(data)
        return data


ERROR_DOCUMENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<osm version="0.6" generator="Overpass API">\n'
    b'  <remark> runtime error: Query ran out of memory in "recurse" </remark>\n'
    b'</osm>\n'
)

GOOD_DOCUMENT = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<osm version="0.6" generator="Overpass API">\n'
    b'  <note>data 2026-11-17</note>\n'
    b'  <node id="1" lat="52.5" lon="13.4"/>\n'
    b'</osm>\n'
)


class OverpassClientTest(unittest.TestCase):
    def test_check_success(self):
        client = OverpassClient(["http://example.test"])
        payload = (
            b'{"version":0.6,"generator":"Overpass API",'
            b'"elements":[{"type":"node","id":1,"lat":52.5,"lon":13.4}]}'
        )
        with patch(
            "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ):
            self.assertTrue(client.check("http://example.test"))

    def test_check_empty_answer_is_rejected(self):
        """Пустой ответ — признак регионального зеркала, а не сервера."""
        client = OverpassClient(["http://example.test"])
        with patch(
            "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
            return_value=FakeResponse(b'{"elements":[]}'),
        ):
            with self.assertRaises(OverpassError):
                client.check("http://example.test")

    def test_check_failure(self):
        client = OverpassClient(["http://example.test"])
        with patch(
            "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ):
            with self.assertRaises(OverpassError):
                client.check("http://example.test")

    def test_check_html_rejected(self):
        client = OverpassClient(["http://example.test"])
        with patch(
            "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
            return_value=FakeResponse(b"<html>busy</html>"),
        ):
            with self.assertRaises(OverpassError):
                client.check("http://example.test")

    def test_test_query_is_outside_switzerland(self):
        """Проверка идёт по месту, где региональных зеркал нет."""
        query = OverpassClient.TEST_QUERY
        self.assertIn("52.5", query)
        self.assertIn("13.4", query)


class OverpassDocumentTest(unittest.TestCase):
    def _write(self, directory, payload, name="answer.osm"):
        path = Path(directory) / name
        path.write_bytes(payload)
        return path

    def test_error_remark_is_not_valid_osm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, ERROR_DOCUMENT)
            self.assertFalse(valid_osm(path))

    def test_note_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, GOOD_DOCUMENT)
            self.assertTrue(valid_osm(path))

    def test_empty_file_is_not_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, b"<osm version='0.6'></osm>")
            self.assertFalse(valid_osm(path))

    def test_fetch_raw_ignores_cached_file_when_cache_is_forbidden(self):
        """Изменённый запрос требует новой загрузки, а не старого файла."""
        client = OverpassClient(["http://example.test"])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "layer.osm"
            destination.write_bytes(GOOD_DOCUMENT)
            with patch(
                "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
                side_effect=urllib.error.URLError("down"),
            ):
                with self.assertRaises(OverpassError):
                    client.fetch_raw(
                        "layer", "data", destination, use_cache=False
                    )
            # Файл не тронут: недописанный ответ не публикуется.
            self.assertEqual(destination.read_bytes(), GOOD_DOCUMENT)


if __name__ == "__main__":
    unittest.main()
