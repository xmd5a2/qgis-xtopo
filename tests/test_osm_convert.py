import tempfile
import unittest
from pathlib import Path

from qgis_xtopo.core.osm_convert import convert_osm_to_features
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

    def test_negation_matches_missing_tag(self):
        query = 'way["natural"="water"]["intermittent"!="yes"];'
        features = [
            {"properties": {"@type": "way", "natural": "water"}, "geometry": {"type": "Polygon"}},
            {"properties": {"@type": "way", "natural": "water", "intermittent": "yes"},
             "geometry": {"type": "Polygon"}},
        ]
        selected = filter_features(features, query)
        self.assertEqual(len(selected), 1)


if __name__ == "__main__":
    unittest.main()
