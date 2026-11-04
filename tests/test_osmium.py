import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qgis_xtopo.core import osmium_prefilter, osmium_tool
from qgis_xtopo.core.bbox import BBox


OSMIUM = osmium_tool.find_osmium()

SMALL_OSM = """<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='test'>
  <node id='1' lat='42.0000' lon='43.0000'/>
  <node id='2' lat='42.0010' lon='43.0010'/>
  <node id='3' lat='50.0000' lon='10.0000'/>
  <way id='10'>
    <nd ref='1'/>
    <nd ref='2'/>
    <tag k='highway' v='track'/>
  </way>
  <way id='11'>
    <nd ref='2'/>
    <nd ref='3'/>
    <tag k='highway' v='track'/>
  </way>
</osm>
"""

# Отношение-кольцо, разорванное между двумя выгрузками: в первой есть
# один член, во второй — другой.  Так выглядит песчаная пустыня на
# границе двух стран (например, Grand Erg Oriental, relation/3073609).
RING_FIRST = """<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='test'>
  <node id='1' lat='0.0' lon='0.0'/>
  <node id='2' lat='0.0' lon='1.0'/>
  <node id='3' lat='1.0' lon='1.0'/>
  <node id='4' lat='1.0' lon='0.0'/>
  <way id='10'>
    <nd ref='1'/>
    <nd ref='2'/>
    <nd ref='3'/>
    <nd ref='4'/>
    <nd ref='1'/>
  </way>
  <relation id='100'>
    <member type='way' ref='10' role='outer'/>
    <member type='way' ref='11' role='outer'/>
    <tag k='type' v='multipolygon'/>
    <tag k='natural' v='sand'/>
    <tag k='name' v='Песчаная пустыня'/>
  </relation>
</osm>
"""

RING_SECOND = """<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' generator='test'>
  <node id='5' lat='2.0' lon='0.0'/>
  <node id='6' lat='2.0' lon='1.0'/>
  <node id='7' lat='3.0' lon='1.0'/>
  <node id='8' lat='3.0' lon='0.0'/>
  <way id='11'>
    <nd ref='5'/>
    <nd ref='6'/>
    <nd ref='7'/>
    <nd ref='8'/>
    <nd ref='5'/>
  </way>
</osm>
"""


class OsmiumToolTest(unittest.TestCase):
    def test_platform_tag(self):
        tag = osmium_tool.platform_tag()
        self.assertIn("-", tag)
        self.assertTrue(tag.endswith(("x86_64", "aarch64", "armv7l")))

    def test_bundled_binary_exists(self):
        # Поставленный бинарник обязан быть в пакете плагина, иначе
        # пользователю придётся ставить osmium самостоятельно.
        bundled = osmium_tool.vendor_root()
        self.assertTrue(bundled.is_dir(), f"нет каталога поставки {bundled}")
        self.assertTrue((bundled / "LICENSE").exists())
        self.assertTrue((bundled / "PROVENANCE.md").exists())

    def test_windows_bundle_complete(self):
        # Поставка для Windows: osmium.exe и его зависимые DLL.  Без них
        # бинарник на Windows не запустится, поэтому комплектность
        # проверяется тестом.
        bundle = osmium_tool.vendor_root() / "win-x86_64"
        if not bundle.is_dir():
            self.skipTest("поставка для Windows не собрана")
        for name in (
            "osmium.exe",
            "boost_program_options.dll",
            "zlib.dll",
            "libexpat.dll",
            "libbz2.dll",
            "lz4.dll",
        ):
            self.assertTrue((bundle / name).exists(), f"нет файла {name}")

    def test_windows_bundle_matches_pin(self):
        # Поставленные файлы должны совпадать с зафиксированными хешами:
        # иначе в архив попадёт не то, что проверено.
        root = osmium_tool.vendor_root()
        bundle = root / "win-x86_64"
        pin = root / "windows-build.json"
        if not bundle.is_dir() or not pin.exists():
            self.skipTest("поставка для Windows не собрана")
        record = json.loads(pin.read_text(encoding="utf-8"))
        for name, digest in record["sha256"].items():
            path = bundle / name
            self.assertTrue(path.exists(), f"нет файла {name}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, digest, f"файл {name} не совпадает с записью")

    def test_ensure_executable_sets_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "osmium"
            target.write_bytes(b"#!/bin/sh\n")
            target.chmod(0o644)
            result = osmium_tool.ensure_executable(target, directory)
            mode = os.stat(result).st_mode
            self.assertTrue(mode & stat.S_IXUSR)

    def test_version_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "osmium"
            fake.write_text("#!/bin/sh\necho 'osmium version 1.19.0'\n")
            fake.chmod(0o755)
            self.assertEqual(osmium_tool.version_of(fake), (1, 19, 0))

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_find_osmium_returns_working_binary(self):
        found = osmium_tool.find_osmium()
        self.assertTrue(found)
        result = subprocess.run(
            [found, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"osmium version", result.stdout)

    def test_find_osmium_without_binaries(self):
        osmium_tool._cache.clear()
        with mock.patch.object(osmium_tool, "bundled_binary", return_value=None), \
                mock.patch.object(osmium_tool.shutil, "which", return_value=None):
            self.assertIsNone(osmium_tool.find_osmium())
        osmium_tool._cache.clear()


class OsmiumPrefilterTest(unittest.TestCase):
    def _make_pbf(self, directory):
        if not OSMIUM:
            self.skipTest("osmium недоступен")
        source = Path(directory) / "small.osm"
        source.write_text(SMALL_OSM, encoding="utf-8")
        result = Path(directory) / "small.osm.pbf"
        subprocess.run(
            [OSMIUM, "cat", "-o", str(result), str(source)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return result

    def test_padded_bbox(self):
        bbox = BBox(43.0, 42.0, 43.1, 42.1)
        lon_min, lat_min, lon_max, lat_max = osmium_prefilter.padded_bbox(bbox)
        self.assertLess(lon_min, bbox.lon_min)
        self.assertGreater(lon_max, bbox.lon_max)
        self.assertLess(lat_min, bbox.lat_min)
        self.assertGreater(lat_max, bbox.lat_max)

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_prefilter_extracts_area(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._make_pbf(directory)
            bbox = BBox(42.9, 41.9, 43.1, 42.1)
            paths, temporary, covering = (
                osmium_prefilter.prefilter_sources(
                    [str(source)], bbox, _config(directory)
                )
            )
            self.assertTrue(paths)
            self.assertEqual(len(temporary), len(paths))
            # Файлы горных массивов читаются без фильтра драйвера.
            self.assertEqual(len(covering), 1)
            self.assertIn("covering", covering[0])
            for path in paths:
                self.assertTrue(Path(path).exists())
                self.assertTrue(Path(path).stat().st_size > 0)

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_prefilter_falls_back_without_osmium(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._make_pbf(directory)
            bbox = BBox(42.9, 41.9, 43.1, 42.1)
            with mock.patch.object(osmium_prefilter.osmium_tool, "find_osmium",
                                   return_value=None):
                paths, temporary, covering = (
                    osmium_prefilter.prefilter_sources(
                        [str(source)], bbox, _config(directory)
                    )
                )
            self.assertEqual(paths, [str(source)])
            self.assertEqual(temporary, [])
            self.assertEqual(covering, [])

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_prefilter_falls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._make_pbf(directory)
            bbox = BBox(42.9, 41.9, 43.1, 42.1)
            with mock.patch.object(osmium_prefilter.osmium_tool, "run",
                                   side_effect=osmium_tool.OsmiumError("сломалось")):
                paths, temporary, covering = (
                    osmium_prefilter.prefilter_sources(
                        [str(source)], bbox, _config(directory)
                    )
                )
            self.assertEqual(paths, [str(source)])
            self.assertEqual(temporary, [])
            self.assertEqual(covering, [])

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_prefilter_keeps_non_pbf_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "map.osm.xml"
            source.write_text(SMALL_OSM, encoding="utf-8")
            bbox = BBox(42.9, 41.9, 43.1, 42.1)
            paths, temporary, covering = osmium_prefilter.prefilter_sources(
                [str(source)], bbox, _config(directory)
            )
            self.assertEqual(paths, [str(source)])
            self.assertEqual(temporary, [])
            self.assertEqual(covering, [])

    def _make_pbf_from(self, directory, name, text):
        source = Path(directory) / f"{name}.osm"
        source.write_text(text, encoding="utf-8")
        result = Path(directory) / f"{name}.osm.pbf"
        subprocess.run(
            [OSMIUM, "cat", "-o", str(result), str(source)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return result

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_relation_split_between_sources_is_joined(self):
        """Отношение, разорванное границей стран, должно собраться.

        В каждой выгрузке relation обрезан, и драйвер GDAL его теряет
        («Relation ... has missing ways»).  Области склеиваются — и
        отношение снова целое.
        """
        from osgeo import ogr

        with tempfile.TemporaryDirectory() as directory:
            first = self._make_pbf_from(directory, "first", RING_FIRST)
            second = self._make_pbf_from(directory, "second", RING_SECOND)
            bbox = BBox(-0.5, -0.5, 3.5, 3.5)
            paths, temporary, covering = osmium_prefilter.prefilter_sources(
                [str(first), str(second)], bbox, _config(directory)
            )
            extracts = [
                path for path in paths
                if "extract" in path and "covering" not in path
            ]
            self.assertEqual(len(extracts), 1, "области должны быть склеены")
            dataset = ogr.Open(extracts[0])
            layer = dataset.GetLayerByName("multipolygons")
            ids = [str(feature.GetField("osm_id")) for feature in layer]
            self.assertIn("100", ids)
            dataset = None

    @unittest.skipUnless(OSMIUM, "osmium недоступен")
    def test_single_source_is_not_merged(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._make_pbf(directory)
            bbox = BBox(42.9, 41.9, 43.1, 42.1)
            paths, temporary, covering = osmium_prefilter.prefilter_sources(
                [str(source)], bbox, _config(directory)
            )
            extracts = [
                path for path in paths
                if "extract" in path and "covering" not in path
            ]
            self.assertEqual(len(extracts), 1)
            self.assertNotIn("merged", extracts[0])


def _config(directory):
    from qgis_xtopo.core.config import Config

    return Config(qgis_projects_dir=directory, project_name="test")


if __name__ == "__main__":
    unittest.main()
