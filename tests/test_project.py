import re
import tempfile
import unittest
import zipfile
from pathlib import Path

from qgis_xtopo.core.project import rewrite_project_archive
from qgis_xtopo.core.resources import PACKAGE_DIR


class ProjectTemplateTest(unittest.TestCase):
    def test_rewrite(self):
        source = PACKAGE_DIR / "resources" / "automap.qgz"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "demo.qgz"
            rewrite_project_archive(source, destination, "demo")
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
                self.assertIn("demo.qgs", names)
                text = archive.read("demo.qgs").decode("utf-8")
        self.assertIn('projectname="demo"', text)
        self.assertIn("<title>demo</title>", text)
        self.assertIn("<qgis_xtopo", text)

    def test_template_has_aerialway_station(self):
        source = PACKAGE_DIR / "resources" / "automap.qgz"
        with zipfile.ZipFile(source) as archive:
            text = archive.read("automap.qgs").decode("utf-8")
        self.assertIn("aerialway station icons", text)
        self.assertIn(
            "aerialway_station_buffered_dissolved_centroids.sqlite",
            text,
        )
        self.assertIn("../icons/aerialway_station.svg", text)
        self.assertIn('labelsEnabled="1"', text)
        # станции видны только до 1:100000 (minScale — предел отдаления)
        block = re.search(
            r"<maplayer[^>]*>(?:(?!</maplayer>).)*"
            r"aerialway station icons.*?</maplayer>",
            text, re.S,
        )
        self.assertIsNotNone(block)
        self.assertIn('minScale="100000"', block.group(0))
        self.assertIn('hasScaleBasedVisibilityFlag="1"', block.group(0))
        self.assertNotIn("100000 - 300000", block.group(0))
        # стрелки направления с учётом oneway, между точками пунктира
        self.assertGreaterEqual(text.count("filled_arrowhead"), 16)
        self.assertIn("offset_along_line", text)
        self.assertIn("&quot;oneway&quot;", text)

    def test_default_queries_include_aerialway_station(self):
        from qgis_xtopo.core.config import default_queries

        self.assertIn("aerialway_station", default_queries())
        self.assertLess(
            default_queries().index("aerialway_station"),
            default_queries().index("aerialway_cable_car_gondola"),
        )


if __name__ == "__main__":
    unittest.main()
