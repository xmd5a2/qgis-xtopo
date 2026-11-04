import unittest

from qgis_xtopo.core.gdal_log import GdalLog


class GdalLogTest(unittest.TestCase):
    def setUp(self):
        self.log = GdalLog()

    def test_warning_is_counted_by_category(self):
        self.log._handle(
            2, 1,
            "A geometry of type GEOMETRYCOLLECTION is inserted into layer "
            "t of geometry type POINT, which is not normally allowed",
        )
        self.assertEqual(
            self.log.counts(),
            {"тип геометрии объекта не совпадает с типом слоя": 1},
        )

    def test_unclosed_ring_is_known_category(self):
        self.log._handle(
            2, 1,
            "Non closed ring detected. To avoid accepting it, set the "
            "OGR_GEOMETRY_ACCEPT_UNCLOSED_RING configuration option to NO",
        )
        self.assertEqual(
            self.log.counts(),
            {"незамкнутые кольца в данных OSM (GDAL закрывает их сам)": 1},
        )

    def test_unknown_warning_keeps_message_start(self):
        self.log._handle(2, 1, "Something entirely new happened")
        self.assertEqual(self.log.counts(), {"Something entirely new happened": 1})

    def test_failure_is_counted_as_error(self):
        from osgeo import gdal

        self.log._handle(gdal.CE_Failure, 1, "A geometry of type POINT is broken")
        self.assertEqual(
            self.log.error_counts(),
            {"тип геометрии объекта не совпадает с типом слоя": 1},
        )
        self.assertEqual(self.log.counts(), {})

    def test_summary_lists_counts(self):
        self.log._handle(2, 1, "Non closed ring detected. blah")
        self.log._handle(2, 1, "Non closed ring detected. blah")
        summary = self.log.summary()
        self.assertEqual(len(summary), 1)
        self.assertIn("— 2", summary[0])

    def test_reset_clears_counters(self):
        self.log._handle(2, 1, "Non closed ring detected. blah")
        self.log.reset()
        self.assertEqual(self.log.counts(), {})
        self.assertEqual(self.log.error_counts(), {})

    def test_install_and_restore_capture_gdal_messages(self):
        import os

        from osgeo import gdal, ogr

        gdal.UseExceptions()
        source = "/tmp/gdallog_test_src.gpkg"
        output = "/tmp/gdallog_test_out.gpkg"
        for path in (source, output):
            if os.path.exists(path):
                os.remove(path)
        dataset = ogr.GetDriverByName("GPKG").CreateDataSource(source)
        layer = dataset.CreateLayer("t", geom_type=ogr.wkbUnknown)
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetGeometry(
            ogr.CreateGeometryFromWkt(
                "GEOMETRYCOLLECTION(POINT(7.4 46.9),POINT(7.41 46.91))"
            )
        )
        layer.CreateFeature(feature)
        dataset = None

        self.log.install()
        try:
            gdal.VectorTranslate(
                output, source, options=["-nln", "t", "-nlt", "POINT"]
            )
        finally:
            self.log.restore()
        self.assertEqual(sum(self.log.counts().values()), 1)

    def test_warning_stops_handler_chain(self):
        """GDAL вызывает все обработчики в стеке: нужен CE_Fatal."""
        from osgeo import gdal

        self.log._handle(gdal.CE_Warning, 1, "Non closed ring detected.")
        self.assertEqual(
            self.log._last_return, gdal.CE_Fatal
        )

    def test_failure_is_not_suppressed(self):
        from osgeo import gdal

        self.log._handle(gdal.CE_Failure, 1, "broken")
        self.assertEqual(self.log._last_return, gdal.CE_None)


if __name__ == "__main__":
    unittest.main()
