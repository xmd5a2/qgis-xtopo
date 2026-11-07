import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.config import (
    Config, default_projects_dir, read_bash_config,
)


class ConfigTest(unittest.TestCase):
    def test_parse_bash_config(self):
        text = """
        # comment
        project_name="demo"
        bbox="1,2,3,4"
        array_queries=(
        water
        river
        )
        generate_terrain=true
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text(text, encoding="utf-8")
            values = read_bash_config(path)
        self.assertEqual(values["project_name"], "demo")
        self.assertEqual(values["array_queries"], ["water", "river"])
        self.assertEqual(values["generate_terrain"], "true")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="1,2,3,4",
                array_queries=["water", "river"],
                generate_terrain=True,
                isolines_step=25,
            )
            config.to_file(path)
            loaded = Config.from_file(path)
        self.assertEqual(loaded.project_name, "demo")
        self.assertEqual(loaded.array_queries, ["water", "river"])
        self.assertTrue(loaded.generate_terrain)
        self.assertEqual(loaded.isolines_step, 25)

    def test_default_overpass_endpoints(self):
        config = Config()
        self.assertEqual(
            config.overpass_endpoint,
            "https://overpass.osm.ch/api/interpreter",
        )
        self.assertIn(
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            config.overpass_endpoints,
        )
        self.assertIn(
            "https://overpass-api.de/api/interpreter",
            config.overpass_endpoints,
        )

    def test_default_projects_dir_in_home(self):
        self.assertEqual(
            default_projects_dir(), str(Path.home() / "qgis_projects")
        )
        self.assertEqual(Config().qgis_projects_dir, default_projects_dir())

    def test_terrain_source_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            config = Config(
                qgis_projects_dir=directory,
                project_name="demo",
                bbox="1,2,3,4",
                array_queries=[],
                generate_terrain=True,
                terrain_source="cop30",
                download_terrain_tiles=True,
            )
            config.to_file(path)
            loaded = Config.from_file(path)
        self.assertEqual(loaded.resolved_terrain_source(), "cop30")
        self.assertTrue(loaded.download_terrain_tiles)

    def test_old_config_resolves_manual_source(self):
        config = Config(
            project_name="demo",
            bbox="1,2,3,4",
            generate_terrain=True,
            get_terrain_tiles=True,
            terrain_src_dir="/tmp/dem",
        )
        self.assertEqual(config.resolved_terrain_source(), "manual")

    def test_manual_source_requires_directory(self):
        config = Config(
            project_name="demo",
            bbox="1,2,3,4",
            generate_terrain=True,
            terrain_source="manual",
        )
        with self.assertRaises(ValueError):
            config.validate()

    def test_validate(self):
        config = Config(project_name="demo", bbox="1,2,3,4", array_queries=["water"])
        config.validate()
        with self.assertRaises(ValueError):
            Config(project_name="bad name", bbox="1,2,3,4", array_queries=["water"]).validate()

    def test_project_name_normalized(self):
        self.assertEqual(Config(project_name=" Demo-1+2 ").project_name, "Demo-1+2")
        self.assertEqual(
            Config(project_name="Карта_01").project_name, "Карта_01"
        )

    def test_project_name_reports_invalid_chars(self):
        with self.assertRaises(ValueError) as context:
            Config(
                project_name="demo name!", bbox="1,2,3,4", array_queries=["water"]
            ).validate()
        message = str(context.exception)
        self.assertIn("пробел", message)
        self.assertIn("«!»", message)


if __name__ == "__main__":
    unittest.main()
