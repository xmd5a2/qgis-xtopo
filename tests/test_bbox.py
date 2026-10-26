import unittest

from qgis_xtopo.core.bbox import BBox, bbox_from_location, parse_bbox


class BBoxTest(unittest.TestCase):
    def test_csv(self):
        bbox = parse_bbox("44.7,42.7,45.1,42.9")
        self.assertEqual(bbox.as_csv(), "44.7,42.7,45.1,42.9")
        self.assertEqual(bbox.as_overpass(), "42.7,44.7,42.9,45.1")
        self.assertEqual(bbox.as_osmium(), "44.7,42.7,45.1,42.9")

    def test_csv_rounds_to_four_places(self):
        bbox = parse_bbox("44.123456,42.654321,45.987654,42.987654")
        self.assertEqual(bbox.as_csv(), "44.1235,42.6543,45.9877,42.9877")

    def test_osm_link(self):
        bbox = parse_bbox("https://www.openstreetmap.org/#map=15/44.8/34.9")
        self.assertLess(bbox.lon_min, 34.9)
        self.assertGreater(bbox.lon_max, 34.9)
        self.assertLess(bbox.lat_min, 44.8)
        self.assertGreater(bbox.lat_max, 44.8)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_bbox("1,2,3")
        with self.assertRaises(ValueError):
            parse_bbox("10,20,5,30")
        with self.assertRaises(ValueError):
            BBox(0, 0, 0, 1)

    def test_location(self):
        bbox = bbox_from_location(44.8, 34.9, 15)
        self.assertTrue(bbox.lon_min < bbox.lon_max)
        self.assertTrue(bbox.lat_min < bbox.lat_max)

    def test_polygon(self):
        bbox = BBox(1, 2, 3, 4)
        polygon = bbox.polygon_geojson()
        ring = polygon["features"][0]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], [1, 4])
        self.assertEqual(ring[-1], [1, 4])


if __name__ == "__main__":
    unittest.main()
