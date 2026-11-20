import math
import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.osm_convert import (
    FieldNames, _assemble_rings, _is_area, convert_osm_to_features,
)
from qgis_xtopo.core.pbf_source import filter_features, parse_query


OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="1.0" lon="1.0"><tag k="place" v="city"/></node>
  <node id="2" lat="1.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="2.0"/>
  <node id="4" lat="2.0" lon="2.0"/>
  <node id="5" lat="2.0" lon="1.0"/>
  <way id="10">
    <nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="2"/>
    <tag k="natural" v="water"/>
  </way>
  <relation id="20">
    <member type="way" ref="10" role="outer"/>
    <tag k="type" v="multipolygon"/>
    <tag k="natural" v="water"/>
  </relation>
</osm>
"""

ISLAND = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="2" lat="1.0" lon="1.0"/>
  <node id="3" lat="1.0" lon="2.0"/>
  <node id="4" lat="2.0" lon="2.0"/>
  <node id="5" lat="2.0" lon="1.0"/>
  <way id="10">
    <nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="2"/>
    <tag k="place" v="island"/>
  </way>
</osm>
"""


class AreaTagsTest(unittest.TestCase):
    """Площадные way — по списку GDAL (osmconf.ini)."""

    def test_gdal_keys_are_areas(self):
        for tags in (
            {"place": "island"},
            {"place": "islet"},
            {"boundary": "administrative"},
            {"sport": "soccer"},
            {"craft": "sculptor"},
            {"geological": "outcrop"},
            {"military": "naval_base"},
            {"office": "company"},
            {"shop": "mall"},
            {"highway": "platform"},
            {"public_transport": "platform"},
            {"building": "yes"},
            {"natural": "water"},
        ):
            self.assertTrue(_is_area(tags), tags)

    def test_plain_lines_are_not_areas(self):
        for tags in (
            {},
            {"highway": "residential"},
            {"barrier": "fence"},
            {"waterway": "stream"},
            {"man_made": "cutline"},
            {"man_made": "pipeline"},
            {"man_made": "embankment"},
        ):
            self.assertFalse(_is_area(tags), tags)

    def test_area_man_made_values(self):
        """Резервуары и силосы площадные в обоих режимах чтения."""
        for value in ("storage_tank", "bunker_silo", "monitoring_station"):
            self.assertTrue(_is_area({"man_made": value}), value)

    def test_area_tag_wins(self):
        self.assertFalse(_is_area({"place": "island", "area": "no"}))
        self.assertTrue(_is_area({"highway": "residential", "area": "yes"}))

    def test_island_way_is_polygon(self):
        """Остров должен приходить площадью, как в режиме PBF."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "island.osm"
            path.write_text(ISLAND, encoding="utf-8")
            features = convert_osm_to_features(path)
        way = next(
            feature for feature in features
            if feature["properties"]["@id"] == "way/10"
        )
        self.assertEqual(way["geometry"]["type"], "Polygon")


class AssembleRingsTest(unittest.TestCase):
    """Кольца мультиполигона собираются из отдельных way."""

    def _ring_parts(self, count):
        """Кольцо из count отрезков: номера узлов по кругу."""
        nodes = {}
        ways = {}
        total = count
        for index in range(total):
            angle = 2 * math.pi * index / total
            nodes[index + 1] = (round(math.cos(angle), 6),
                                round(math.sin(angle), 6))
        for index in range(total):
            first = index + 1
            second = (index + 1) % total + 1
            ways[index + 1] = {"refs": [first, second], "tags": {}}
        return ways, nodes

    def test_ring_is_closed(self):
        ways, nodes = self._ring_parts(6)
        rings = _assemble_rings(sorted(ways), ways, nodes)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])
        self.assertEqual(len(rings[0]), 7)

    def test_many_parts_are_assembled_quickly(self):
        """Сборка не должна быть квадратичной по числу way."""
        ways, nodes = self._ring_parts(2000)
        rings = _assemble_rings(sorted(ways), ways, nodes)
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][0], rings[0][-1])
        self.assertEqual(len(rings[0]), 2001)


class OsmConvertTest(unittest.TestCase):
    def test_features(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.osm"
            path.write_text(OSM, encoding="utf-8")
            features = convert_osm_to_features(path)
        ids = {feature["properties"]["@id"] for feature in features}
        self.assertIn("node/1", ids)
        self.assertIn("way/10", ids)
        self.assertIn("relation/20", ids)
        way = next(feature for feature in features if feature["properties"]["@id"] == "way/10")
        self.assertEqual(way["geometry"]["type"], "Polygon")
        relation = next(feature for feature in features if feature["properties"]["@id"] == "relation/20")
        self.assertIn(relation["geometry"]["type"], ("Polygon", "MultiPolygon"))

    def test_query_filter(self):
        query = '(\nway["natural"="water"];\nnode["place"="city"];\n);'
        filters = parse_query(query)
        self.assertEqual(len(filters), 2)
        features = [
            {"properties": {"@type": "way", "natural": "water"}, "geometry": {"type": "Polygon"}},
            {"properties": {"@type": "node", "place": "city"}, "geometry": {"type": "Point"}},
            {"properties": {"@type": "way", "natural": "wood"}, "geometry": {"type": "Polygon"}},
        ]
        selected = filter_features(features, query)
        self.assertEqual(len(selected), 2)

    def test_island_query_includes_archipelago(self):
        """Острова, сгруппированные в relation, тоже должны попадать в слой.

        Драйвер GDAL не отдаёт coastline-way, входящие в мультиполигон,
        отдельными объектами: у группы островов остаётся только relation
        с тегом place=archipelago.
        """
        query = (
            Path(__file__).resolve().parents[1]
            / "resources/queries/island.txt"
        ).read_text(encoding="utf-8")
        features = [
            {"properties": {"@type": "relation", "place": "archipelago"},
             "geometry": {"type": "MultiPolygon"}},
            {"properties": {"@type": "way", "place": "islet"},
             "geometry": {"type": "Polygon"}},
            {"properties": {"@type": "relation", "place": "bay"},
             "geometry": {"type": "Polygon"}},
        ]
        selected = filter_features(features, query)
        self.assertEqual(
            [feature["properties"]["place"] for feature in selected],
            ["archipelago", "islet"],
        )

    def test_negation_matches_missing_tag(self):
        query = 'way["natural"="water"]["intermittent"!="yes"];'
        features = [
            {"properties": {"@type": "way", "natural": "water"}, "geometry": {"type": "Polygon"}},
            {"properties": {"@type": "way", "natural": "water", "intermittent": "yes"},
             "geometry": {"type": "Polygon"}},
        ]
        selected = filter_features(features, query)
        self.assertEqual(len(selected), 1)

    def test_missing_tag_filter(self):
        """[!"building"] отбирает объекты без тега building."""
        query = 'way["admin_level"="2"][!"building"];'
        features = [
            {"properties": {"@type": "way", "admin_level": "2",
                            "boundary": "administrative"},
             "geometry": {"type": "LineString"}},
            {"properties": {"@type": "way", "admin_level": "2",
                            "building": "yes"},
             "geometry": {"type": "Polygon"}},
        ]
        selected = filter_features(features, query)
        self.assertEqual(len(selected), 1)
        self.assertNotIn("building", selected[0]["properties"])

    def test_field_names_are_unique_ignoring_case(self):
        """SQLite не различает регистр имён колонок."""
        names = FieldNames()
        first = names.apply({"communication:5G": "yes", "name": "tower"})
        second = names.apply({"communication:5g": "no", "name": "tower"})
        keys = set(first) | set(second)
        self.assertEqual(len(keys), len({key.lower() for key in keys}))

    def test_rename_is_the_same_for_all_features(self):
        names = FieldNames()
        first = names.apply({"communication:5G": "yes"})
        second = names.apply({"communication:5g": "no"})
        self.assertIn("communication:5G", first)
        self.assertNotIn("communication:5g", second)
        self.assertEqual(len(second), 1)

    def test_name_keeps_its_spelling(self):
        names = FieldNames()
        resolved = names.apply({"Name": "x", "name": "y"})
        self.assertIn("name", resolved)
        self.assertEqual(resolved["name"], "y")


if __name__ == "__main__":
    unittest.main()
