import unittest
import urllib.error
from unittest.mock import patch

from qgis_xtopo.core.overpass_client import OverpassClient, OverpassError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.payload


class OverpassClientTest(unittest.TestCase):
    def test_check_success(self):
        client = OverpassClient(["http://example.test"])
        with patch(
            "qgis_xtopo.core.overpass_client.urllib.request.urlopen",
            return_value=FakeResponse(b'{"elements":[]}'),
        ):
            self.assertTrue(client.check("http://example.test"))

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


if __name__ == "__main__":
    unittest.main()
