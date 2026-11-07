import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.osm_convert import FieldNames, convert_osm_to_features
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
