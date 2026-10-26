import unittest

from qgis_xtopo.core.pbf_source import QueryIndex, parse_tags


class PbfSourceTest(unittest.TestCase):
    def test_json_tags(self):
        tags = parse_tags('{"natural":"water","name:ru":"Озеро"}')
        self.assertEqual(tags["natural"], "water")
        self.assertEqual(tags["name:ru"], "Озеро")

    def test_hstore_tags(self):
        tags = parse_tags('"natural"=>"water","name:ru"=>"Озеро","surface"=>"asphalt"')
        self.assertEqual(tags["natural"], "water")
        self.assertEqual(tags["name:ru"], "Озеро")
        self.assertEqual(tags["surface"], "asphalt")

    def test_hstore_escapes(self):
        text = r'''"name"=>"D\"Or","int_name"=>"Principat d\'Andorra"'''
        tags = parse_tags(text)
        self.assertEqual(tags["name"], 'D"Or')
        self.assertEqual(tags["int_name"], "Principat d'Andorra")

    def test_query_index(self):
        index = QueryIndex({
            "water": 'way["natural"="water"]["intermittent"!="yes"];',
            "peak": 'node["natural"="peak"];',
        })
        self.assertEqual(index.matching_names("way", {"natural": "water"}), {"water"})
        self.assertEqual(index.matching_names("node", {"natural": "peak"}), {"peak"})
        self.assertEqual(
            index.matching_names("way", {"natural": "water", "intermittent": "yes"}),
            set(),
        )


    def test_filter_extent_expands_bbox(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.pbf_source import _filter_extent

        bbox = BBox(9.0, 46.0, 9.1, 46.1)
        min_x, max_x, min_y, max_y = _filter_extent(bbox)
        self.assertLess(min_x, bbox.lon_min)
        self.assertGreater(max_x, bbox.lon_max)
        self.assertLess(min_y, bbox.lat_min)
        self.assertGreater(max_y, bbox.lat_max)
        # Запас одинаков со всех сторон
        self.assertAlmostEqual(bbox.lon_min - min_x, bbox.lat_min - min_y, 6)
        self.assertAlmostEqual(bbox.lon_min - min_x, max_y - bbox.lat_max, 6)

    def test_filter_extent_margin_grows_with_bbox(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.pbf_source import _filter_extent

        small = _filter_extent(BBox(9.0, 46.0, 9.01, 46.01))
        large = _filter_extent(BBox(9.0, 46.0, 10.0, 47.0))
        self.assertGreater(
            large[1] - large[0], small[1] - small[0]
        )


if __name__ == "__main__":
    unittest.main()
