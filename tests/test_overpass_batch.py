import unittest

from qgis_xtopo.core import overpass_batch


class OverpassBatchTest(unittest.TestCase):
    def test_parse_simple_query(self):
        selectors = overpass_batch.parse_selectors(
            '(\nnode["railway"="station"]["subway"!="yes"];\n'
            'way["railway"="station"];\n);'
        )
        self.assertIsNotNone(selectors)
        self.assertEqual(len(selectors), 2)
        self.assertEqual(selectors[0].kind, "node")
        self.assertEqual(selectors[0].conditions, [
            ("railway", "=", "station"),
            ("subway", "!=", "yes"),
        ])

    def test_parse_unsupported_query(self):
        # ->.relations нельзя повторить на клиенте
        self.assertIsNone(overpass_batch.parse_selectors(
            '(\nrel["admin_level"="2"]->.relations;\n'
            'node(r.relations)["place"="city"];\n);'
        ))

    def test_matching(self):
        selector = overpass_batch.Selector("way", [("aerialway", "=", "chair_lift")])
        self.assertTrue(selector.matches("way", {"aerialway": "chair_lift"}))
        self.assertFalse(selector.matches("way", {"aerialway": "cable_car"}))
        self.assertFalse(selector.matches("node", {"aerialway": "chair_lift"}))
        negate = overpass_batch.Selector("node", [("oneway", "!=", "no")])
        self.assertTrue(negate.matches("node", {}))
        self.assertFalse(negate.matches("node", {"oneway": "no"}))

    def test_split_batch_writes_layers(self):
        import tempfile
        from pathlib import Path

        combined = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
  <node id="1" lat="45.0" lon="9.0"><tag k="railway" v="station"/><tag k="name" v="A"/></node>
  <node id="2" lat="45.1" lon="9.1"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><tag k="aerialway" v="chair_lift"/></way>
  <node id="1" lat="45.0" lon="9.0"/>
  <way id="10"/>
</osm>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.osm"
            path.write_text(combined, encoding="utf-8")
            layers = {
                "stations": [overpass_batch.Selector(
                    "node", [("railway", "=", "station")])],
                "lifts": [overpass_batch.Selector(
                    "way", [("aerialway", "=", "chair_lift")])],
            }
            result = overpass_batch.split_batch(path, layers)
            stations = Path(result["stations"][0]).read_text(encoding="utf-8")
            lifts = Path(result["lifts"][0]).read_text(encoding="utf-8")
        # Узел станции попадает в свой слой с тегами
        self.assertIn('<tag k="name" v="A"/>', stations)
        self.assertIn('id="1"', stations)
        # В линию попадает сама линия и её узлы без тегов
        self.assertIn('<way id="10">', lifts)
        self.assertIn('<nd ref="1"/>', lifts)
        self.assertNotIn('v="A"', lifts)
        self.assertIn('<node id="1" lat="45.0" lon="9.0"/>', lifts)

    def test_build_batch_query(self):
        class FakeBbox:
            def as_overpass(self):
                return "9.0,45.0,9.5,45.5"

        selectors = [
            overpass_batch.Selector("node", [("aerialway", "=", "station")]),
            overpass_batch.Selector("way", [("aerialway", "=", "chair_lift")]),
        ]
        query = overpass_batch.build_batch_query(selectors, FakeBbox())
        self.assertIn("[bbox:9.0,45.0,9.5,45.5]", query)
        self.assertIn('node["aerialway"="station"];', query)
        self.assertIn('way["aerialway"="chair_lift"];', query)
        self.assertTrue(query.rstrip().endswith("out body;>;out skel qt;"))


if __name__ == "__main__":
    unittest.main()
