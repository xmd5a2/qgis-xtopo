import html
import importlib.util
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class PipelineIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qgis.core import QgsApplication

        cls.app = QgsApplication([], False)
        cls.app.initQgis()

    @classmethod
    def tearDownClass(cls):
        from qgis.core import QgsProject

        # QGIS 3.34 can crash while destroying the default project style
        # during exitQgis() if widgets/models created by the tests are still
        # alive.  Drop the project data and pending deferred deletions first.
        cls.app.processEvents()
        QgsProject.instance().clear()
        cls.app.processEvents()
        cls.app.exitQgis()

    @unittest.skipUnless(
        os.environ.get("QGIS_XTOPO_TEST_PBF")
        and Path(os.environ["QGIS_XTOPO_TEST_PBF"]).exists(),
        "Set QGIS_XTOPO_TEST_PBF to a local .pbf file to run this test",
    )
    def test_pbf_extract(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pbf_source import prepare_dataset
        from qgis_xtopo.core.resources import read_query

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="pbf",
                bbox="1.4,42.4,1.8,42.7",
                array_queries=["water", "places_main", "highway_main"],
                source_mode="pbf",
                pbf_path=os.environ["QGIS_XTOPO_TEST_PBF"],
            )
            config.ensure_directories()
            dataset = prepare_dataset(
                config,
                BBox(1.4, 42.4, 1.8, 42.7),
                config.array_queries,
                {name: read_query(name) for name in config.array_queries},
            )
            try:
                self.assertIn("water", dataset.feature_files)
                self.assertIn("places_main", dataset.feature_files)
                self.assertGreater(dataset.feature_files["water"].stat().st_size, 0)
            finally:
                dataset.close()

    @unittest.skipUnless(
        os.environ.get("QGIS_XTOPO_TEST_PBF")
        and Path(os.environ["QGIS_XTOPO_TEST_PBF"]).exists(),
        "Set QGIS_XTOPO_TEST_PBF to a local .pbf file to run this test",
    )
    def test_pbf_probe(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pbf_source import probe_dataset
        from qgis_xtopo.core.resources import read_query

        names = ["water", "places_main", "highway_main"]
        texts = {name: read_query(name) for name in names}
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="probe",
                bbox="1.4,42.4,1.8,42.7",
                array_queries=names,
                source_mode="pbf",
                pbf_path=os.environ["QGIS_XTOPO_TEST_PBF"],
            )
            config.ensure_directories()
            probe = probe_dataset(
                config, BBox(1.4, 42.4, 1.8, 42.7), names, texts
            )
        self.assertGreater(probe.total, 0)
        self.assertGreater(probe.in_bbox, 0)
        self.assertGreater(probe.matched, 0)
        self.assertGreater(probe.elapsed, 0)

    @unittest.skipUnless(
        os.environ.get("QGIS_XTOPO_TEST_PBF")
        and Path(os.environ["QGIS_XTOPO_TEST_PBF"]).exists(),
        "Set QGIS_XTOPO_TEST_PBF to a local .pbf file to run this test",
    )
    def test_pbf_fast_probe(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pbf_fast import probe_fast
        from qgis_xtopo.core.resources import read_query

        names = ["water", "places_main", "highway_main"]
        texts = {name: read_query(name) for name in names}
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="probe",
                bbox="1.4,42.4,1.8,42.7",
                array_queries=names,
                source_mode="pbf",
                pbf_path=os.environ["QGIS_XTOPO_TEST_PBF"],
            )
            probe = probe_fast(
                config, BBox(1.4, 42.4, 1.8, 42.7), names, texts
            )
        self.assertGreater(probe.total, 0)
        self.assertGreater(probe.in_bbox, 0)
        self.assertGreater(probe.matched, 0)
        self.assertGreater(probe.elapsed, 0)
        self.assertLessEqual(probe.sampled_blobs, probe.total_blobs)

    @unittest.skipUnless(
        os.environ.get("QGIS_XTOPO_TEST_PBF")
        and Path(os.environ["QGIS_XTOPO_TEST_PBF"]).exists(),
        "Set QGIS_XTOPO_TEST_PBF to a local .pbf file to run this test",
    )
    def test_multi_source_deduplicates(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pbf_source import prepare_dataset
        from qgis_xtopo.core.resources import read_query

        names = ["water", "places_main", "highway_main"]
        texts = {name: read_query(name) for name in names}
        source = os.environ["QGIS_XTOPO_TEST_PBF"]
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="multi",
                bbox="1.4,42.4,1.8,42.7",
                array_queries=names,
                source_mode="pbf",
                pbf_path=source,
            )
            config.ensure_directories()
            one = prepare_dataset(
                config, BBox(1.4, 42.4, 1.8, 42.7), names, texts,
                sources=[source],
            )
            try:
                single = {
                    name: path.read_text(encoding="utf-8").count("\n")
                    for name, path in one.feature_files.items()
                }
            finally:
                one.close()
            two = prepare_dataset(
                config, BBox(1.4, 42.4, 1.8, 42.7), names, texts,
                sources=[source, source],
            )
            try:
                doubled = {
                    name: path.read_text(encoding="utf-8").count("\n")
                    for name, path in two.feature_files.items()
                }
            finally:
                two.close()
        self.assertTrue(single)
        self.assertEqual(single, doubled)

    def test_dock_is_compact(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        self.assertEqual(dock.tabs.count(), 4)
        self.assertEqual(dock.tabs.tabText(dock.project_tab_index), "Проект")
        self.assertFalse(dock.tabs.isTabEnabled(dock.project_tab_index))
        self.assertEqual(dock.tabs.currentIndex(), 0)
        self.assertLessEqual(dock.minimumSizeHint().height(), 600)
        self.assertFalse(dock.log_toggle.isChecked())
        self.assertEqual(dock.overpass_server.count(), 4)
        self.assertEqual(
            dock.overpass_server.itemData(1),
            "https://overpass-api.de/api/interpreter",
        )
        self.assertEqual(dock.source_mode.count(), 5)
        self.assertEqual(dock.source_mode.itemData(0), "geofabrik")
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("geofabrik"))
        self.assertEqual(dock.source_label.text(), "Карты:")
        self.assertFalse(dock.source_value.isEnabled())
        self.assertFalse(dock.browse_source.isVisibleTo(dock))
        self.assertFalse(dock.download_pbf_button.isVisibleTo(dock))
        self.assertFalse(hasattr(dock, "download_maps_button"))
        self.assertTrue(dock.geofabrik_info.isVisibleTo(dock))
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("pbf"))
        self.assertIn(".pbf", dock.source_value.placeholderText())
        self.assertIn("bbox", dock.source_hint.text())
        self.assertNotIn("osmium", dock.source_hint.text())
        self.assertEqual(dock.source_label.text(), "Файл PBF:")
        self.assertFalse(dock.source_value.text().startswith("http"))
        self.assertTrue(dock.browse_source.isEnabled())
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("local"))
        self.assertEqual(dock.source_label.text(), "Команда:")
        self.assertIn("osm3s_query", dock.source_value.text())
        self.assertIn("--db-dir", dock.source_value.text())
        self.assertFalse(dock.browse_source.isEnabled())
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("ssh"))
        self.assertEqual(dock.source_label.text(), "SSH-команда:")
        self.assertIn("ssh user@server", dock.source_value.text())
        self.assertFalse(dock.browse_source.isEnabled())
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("overpass"))
        self.assertEqual(dock.source_label.text(), "Сервер:")
        self.assertEqual(
            dock.source_value.text(), dock.overpass_server.currentData()
        )
        self.assertFalse(dock.browse_source.isEnabled())
        self.assertEqual(dock.area_label.text(), "Охват:")
        from qgis.PyQt.QtWidgets import QPushButton

        button_texts = [button.text() for button in dock.findChildren(QPushButton)]
        self.assertNotIn("Текущий вид", button_texts)
        self.assertNotIn("Загрузить карты", button_texts)
        self.assertEqual(
            dock.status.minimumHeight(), dock.status.maximumHeight()
        )
        self.assertTrue(dock.prepare_button.styleSheet())
        dock.deleteLater()

    def test_dock_project_panel(self):
        import tempfile

        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLineSymbol, QgsProject,
            QgsRuleBasedRenderer, QgsVectorLayer,
        )
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        project = QgsProject.instance()
        project.clear()
        with tempfile.TemporaryDirectory() as directory:
            crop = Path(directory) / "crop.geojson"
            crop.write_text(
                '{"type":"FeatureCollection","features":[{"type":"Feature",'
                '"properties":{},"geometry":{"type":"Polygon","coordinates":'
                '[[[0,0],[0,1],[1,1],[1,0],[0,0]]]}}]}',
                encoding="utf-8",
            )
            dock = XtopoDockWidget(None)
            try:
                bbox = QgsVectorLayer(str(crop), "BBOX", "ogr")
                isolines = QgsVectorLayer(
                    "LineString?crs=EPSG:4326&field=ELEV:integer",
                    "[TERRAIN] isolines regular",
                    "memory",
                )
                root_rule = QgsRuleBasedRenderer.Rule(None)
                for value, active in ((50, True), (10, True), (5, False)):
                    rule = QgsRuleBasedRenderer.Rule(QgsLineSymbol())
                    rule.setLabel(str(value))
                    rule.setFilterExpression(f'"ELEV" % {value} = 0')
                    rule.setActive(active)
                    root_rule.appendChild(rule)
                isolines.setRenderer(QgsRuleBasedRenderer(root_rule))
                feature = QgsFeature(isolines.fields())
                feature.setAttribute("ELEV", 25)
                feature.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 1 1)"))
                isolines.dataProvider().addFeature(feature)
                steep = QgsVectorLayer(
                    "Point?crs=EPSG:4326&field=id:integer",
                    "[TERRAIN] slope (steep)",
                    "memory",
                )
                project.addMapLayers([bbox, isolines, steep])
                root = project.layerTreeRoot()
                root.findLayer(isolines.id()).setItemVisibilityChecked(False)
                root.findLayer(steep.id()).setItemVisibilityChecked(False)

                dock.log_toggle.setChecked(True)
                dock._refresh_project_state()
                panel = dock.project_panel
                self.assertTrue(dock.tabs.isTabEnabled(dock.project_tab_index))
                for index in range(3):
                    self.assertFalse(dock.tabs.isTabEnabled(index))
                self.assertIs(dock.tabs.currentWidget(), panel)
                self.assertFalse(dock.log_toggle.isChecked())
                self.assertFalse(dock.prepare_button.isEnabled())
                self.assertFalse(dock.open_button.isEnabled())
                self.assertEqual(dock.prepare_button.styleSheet(), "")
                self.assertFalse(panel.isolines_check.isChecked())
                self.assertEqual(
                    panel.isolines_check.text(), "Контуры высот (изогипсы)"
                )
                self.assertFalse(panel.isolines_check.icon().isNull())
                self.assertEqual(panel.rules_title.text(), "Кратность:")
                self.assertFalse(panel.rules_title.isHidden())
                self.assertEqual(len(panel._rule_checks), 3)
                states = {
                    label: box.isChecked() for box, label in panel._rule_checks
                }
                self.assertTrue(states["50"])
                self.assertTrue(states["10"])
                self.assertFalse(states["5"])

                panel.isolines_check.setChecked(True)
                node = root.findLayer(isolines.id())
                self.assertTrue(node.itemVisibilityChecked())
                box5 = next(
                    box for box, label in panel._rule_checks if label == "5"
                )
                box10 = next(
                    box for box, label in panel._rule_checks if label == "10"
                )
                box50 = next(
                    box for box, label in panel._rule_checks if label == "50"
                )
                self.assertTrue(box5.isEnabled())
                self.assertFalse(box10.isEnabled())
                self.assertFalse(box50.isEnabled())
                box5.setChecked(True)
                renderer = isolines.renderer()
                active = {
                    rule.label(): rule.active()
                    for rule in renderer.rootRule().children()
                }
                self.assertTrue(active["5"])

                node.setItemVisibilityChecked(False)
                self.assertFalse(panel.isolines_check.isChecked())

                panel.steep_check.setChecked(True)
                steep_node = root.findLayer(steep.id())
                self.assertTrue(steep_node.itemVisibilityChecked())

                project.clear()
                dock._refresh_project_state()
                self.assertFalse(dock.tabs.isTabEnabled(dock.project_tab_index))
                for index in range(3):
                    self.assertTrue(dock.tabs.isTabEnabled(index))
                self.assertEqual(dock.tabs.currentIndex(), 0)
                self.assertTrue(dock.prepare_button.isEnabled())
                self.assertTrue(dock.prepare_button.styleSheet())
            finally:
                dock._project_timer.stop()
                project.clear()
                dock.deleteLater()

    def test_isolines_availability_respects_step(self):
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLineSymbol, QgsRuleBasedRenderer,
            QgsVectorLayer,
        )
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        filters = {
            1000: '"ELEV" % 1000 = 0 AND "ELEV" != 0',
            200: '"ELEV" % 1000 != 0 AND "ELEV" % 200 = 0',
            100: '"ELEV" % 1000 != 0 AND "ELEV" % 100 = 0',
            50: (
                '"ELEV" % 1000 != 0 AND "ELEV" % 100 != 0 '
                'AND "ELEV" % 50 = 0'
            ),
            25: (
                '"ELEV" % 1000 != 0 AND "ELEV" % 100 != 0 '
                'AND "ELEV" % 50 != 0 AND "ELEV" % 25 = 0'
            ),
            10: (
                '"ELEV" % 1000 != 0 AND "ELEV" % 100 != 0 '
                'AND "ELEV" % 50 != 0 AND "ELEV" % 10 = 0'
            ),
            5: (
                '"ELEV" % 1000 != 0 AND "ELEV" % 100 != 0 '
                'AND "ELEV" % 50 != 0 AND "ELEV" % 10 != 0 '
                'AND "ELEV" % 5 = 0'
            ),
        }
        layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326&field=ELEV:integer",
            "[TERRAIN] isolines regular",
            "memory",
        )
        root_rule = QgsRuleBasedRenderer.Rule(None)
        for value in sorted(filters, reverse=True):
            rule = QgsRuleBasedRenderer.Rule(QgsLineSymbol())
            rule.setLabel(str(value))
            rule.setFilterExpression(filters[value])
            root_rule.appendChild(rule)
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        for elevation in (100, 200, 300, 1000):
            feature = QgsFeature(layer.fields())
            feature.setAttribute("ELEV", elevation)
            feature.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 1 1)"))
            layer.dataProvider().addFeature(feature)

        panel = ProjectPanel(None)
        try:
            rules = [
                (value, str(value), True) for value in sorted(filters)
            ]
            available = panel._available_rule_labels(layer, rules)
            for label in ("5", "10", "25", "50"):
                self.assertFalse(available[label], label)
            for label in ("100", "200", "1000"):
                self.assertTrue(available[label], label)
        finally:
            panel.deleteLater()

    def test_dock_terrain_options(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        try:
            self.assertEqual(
                dock.hillshade_slope.text(), "Затенение рельефа и уклоны"
            )
            self.assertEqual(dock.isolines.text(), "Контуры высот")
            self.assertEqual(dock.smooth_isolines.text(), "Сглаживать контуры")
            self.assertEqual(
                [
                    dock.resample_method.itemData(index)
                    for index in range(dock.resample_method.count())
                ],
                ["cubicspline", "lanczos"],
            )
            dock.generate_terrain.setChecked(True)
            dock.dem_dir.clear()
            dock.download_terrain.setChecked(False)
            dock._set_isolines_step(10)
            self.assertEqual(dock._current_isolines_step(), 10)
            self.assertFalse(dock.isolines_steps[5].isChecked())
            self.assertTrue(dock.isolines_steps[10].isChecked())
            self.assertTrue(dock.isolines_steps[50].isChecked())

            dock.isolines_steps[5].click()
            self.assertTrue(
                all(box.isChecked() for box in dock.isolines_steps.values())
            )
            self.assertEqual(dock._current_isolines_step(), 5)

            dock.isolines_steps[100].click()
            checked = [
                value for value, box in dock.isolines_steps.items()
                if box.isChecked()
            ]
            self.assertEqual(checked, [100, 200])
            self.assertEqual(dock._current_isolines_step(), 100)

            dock.dem_dir.setText("/tmp/dem")
            self.assertFalse(dock.download_terrain.isChecked())
            self.assertFalse(dock.download_terrain.isEnabled())
            self.assertIn("не найден", dock.dem_hint.text())
            with tempfile.TemporaryDirectory() as dem:
                dock.dem_dir.setText(dem)
                self.assertFalse(dock.download_terrain.isChecked())
                self.assertFalse(dock.download_terrain.isEnabled())
                self.assertIn("Используется рельеф", dock.dem_hint.text())
            dock.dem_dir.clear()
            self.assertTrue(dock.download_terrain.isEnabled())
            dock.download_terrain.setChecked(True)
            dock.dem_dir.setText("/tmp/dem")
            self.assertFalse(dock.download_terrain.isChecked())
            with tempfile.TemporaryDirectory() as directory:
                dock.projects_dir.setText(directory)
                dock.project_name.setText("demo")
                dock.bbox.setText("1,2,3,4")
                dock.source_mode.setCurrentIndex(
                    dock.source_mode.findData("geofabrik")
                )
                with self.assertRaises(ValueError):
                    dock._build_config()
        finally:
            dock.deleteLater()

    def test_sheet_tool_fixed_aspect(self):
        from qgis.core import QgsPointXY
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.atlas import SheetMapTool

        canvas = QgsMapCanvas()
        tool = SheetMapTool(canvas, 297.0 / 210.0)
        tool.start_point = QgsPointXY(0, 0)
        rectangle = tool._rectangle(QgsPointXY(100, 20))
        self.assertAlmostEqual(
            rectangle.width() / rectangle.height(), 297.0 / 210.0, places=3
        )
        tool.deactivate()
        tool.dispose()
        canvas.deleteLater()

    def test_configure_map_layers_keeps_project_visibility(self):
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLayoutItemMap, QgsPointXY,
            QgsPrintLayout, QgsProject, QgsRectangle, QgsVectorLayer,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        class Iface:
            def __init__(self, canvas):
                self._canvas = canvas

            def mapCanvas(self):
                return self._canvas

        project = QgsProject.instance()
        project.clear()
        canvas = QgsMapCanvas()
        panel = ProjectPanel(Iface(canvas))
        try:
            inside = QgsVectorLayer(
                "Point?crs=EPSG:3857&field=id:integer", "inside", "memory"
            )
            outside = QgsVectorLayer(
                "Point?crs=EPSG:3857&field=id:integer", "outside", "memory"
            )
            feature = QgsFeature()
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
            inside.dataProvider().addFeature(feature)
            inside.updateExtents()
            project.addMapLayer(inside)
            project.addMapLayer(outside)

            layout = QgsPrintLayout(project)
            map_item = QgsLayoutItemMap(layout)
            panel._configure_map_layers(
                map_item, QgsRectangle(-100, -100, 100, 100)
            )
            root = project.layerTreeRoot()
            self.assertTrue(root.findLayer(inside.id()).itemVisibilityChecked())
            self.assertTrue(root.findLayer(outside.id()).itemVisibilityChecked())
            self.assertEqual(map_item.layers(), [inside])

            panel._restore_map_layers()
            self.assertEqual(map_item.layers(), [])
            self.assertTrue(root.findLayer(outside.id()).itemVisibilityChecked())
        finally:
            project.clear()
            panel.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

    def test_layers_for_extent_keeps_legend_order(self):
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLayoutItemMap, QgsPointXY,
            QgsPrintLayout, QgsProject, QgsRectangle, QgsVectorLayer,
        )
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        panel = ProjectPanel(None)
        try:
            bottom = QgsVectorLayer(
                "Point?crs=EPSG:3857&field=id:integer", "bottom", "memory"
            )
            top = QgsVectorLayer(
                "Point?crs=EPSG:3857&field=id:integer", "top", "memory"
            )
            for layer in (bottom, top):
                feature = QgsFeature()
                feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
                layer.dataProvider().addFeature(feature)
                layer.updateExtents()
            project.addMapLayer(bottom)
            project.addMapLayer(top)

            layout = QgsPrintLayout(project)
            map_item = QgsLayoutItemMap(layout)
            panel._configure_map_layers(
                map_item, QgsRectangle(-100, -100, 100, 100)
            )
            self.assertEqual(
                [layer.name() for layer in map_item.layers()],
                ["top", "bottom"],
            )
            panel._restore_map_layers()
        finally:
            project.clear()
            panel.deleteLater()
            self.app.processEvents()

    def test_atlas_layout_label_placement(self):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsLayoutItem, QgsLayoutItemLabel,
            QgsLayoutItemMap, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout,
            QgsProject, QgsUnitTypes,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.projectpanel import ProjectPanel, _enum

        project = QgsProject.instance()
        project.clear()
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName("Detailed")
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(
            QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters)
        )
        map_item.attemptResize(
            QgsLayoutSize(297, 210, QgsUnitTypes.LayoutMillimeters)
        )
        layout.addLayoutItem(map_item)
        number_item = QgsLayoutItemLabel(layout)
        number_item.setText("[% @atlas_featurenumber %]")
        layout.addLayoutItem(number_item)
        copyright_item = QgsLayoutItemLabel(layout)
        copyright_item.setText("Map data OpenStreetMap contributors")
        layout.addLayoutItem(copyright_item)
        lower_right = _enum(QgsLayoutItem, "ReferencePoint", "LowerRight")
        copyright_item.setReferencePoint(lower_right)
        copyright_item.attemptMove(
            QgsLayoutPoint(297, 210, QgsUnitTypes.LayoutMillimeters)
        )
        project.layoutManager().addLayout(layout)

        class Iface:
            def __init__(self, canvas):
                self._canvas = canvas

            def mapCanvas(self):
                return self._canvas

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(
            QgsCoordinateReferenceSystem("EPSG:3857")
        )
        panel = ProjectPanel(Iface(canvas))
        try:
            panel._configure_layout("test")
            # копирайт прижат к правому нижнему углу страницы
            size = copyright_item.sizeWithUnits()
            self.assertAlmostEqual(
                copyright_item.pos().x() + size.width(), 297.0, places=3
            )
            self.assertAlmostEqual(
                copyright_item.pos().y() + size.height(), 210.0, places=3
            )
            # номер листа — в левом верхнем углу, как в макете
            self.assertAlmostEqual(number_item.pos().x(), 0.0, places=3)
            self.assertAlmostEqual(number_item.pos().y(), 0.0, places=3)
        finally:
            panel.deleteLater()
            project.clear()

    def test_atlas_export_from_panel(self):
        import tempfile

        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsLayoutItemMap, QgsLayoutPoint,
            QgsLayoutSize, QgsPrintLayout, QgsProject, QgsRectangle,
            QgsUnitTypes,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName("Detailed")
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(
            QgsLayoutPoint(0, 0, QgsUnitTypes.LayoutMillimeters)
        )
        map_item.attemptResize(
            QgsLayoutSize(297, 210, QgsUnitTypes.LayoutMillimeters)
        )
        layout.addLayoutItem(map_item)
        project.layoutManager().addLayout(layout)

        class Iface:
            def __init__(self, canvas):
                self._canvas = canvas

            def mapCanvas(self):
                return self._canvas

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(
            QgsCoordinateReferenceSystem("EPSG:3857")
        )
        panel = ProjectPanel(Iface(canvas))
        try:
            panel.add_sheet_button.setChecked(True)
            self.assertIs(canvas.mapTool(), panel._sheet_tool)
            panel.add_sheet_button.setChecked(False)
            panel.page_format.setCurrentIndex(
                panel.page_format.findData("A3")
            )
            self.assertEqual(panel._page_size_mm(), (420.0, 297.0))
            panel._sheet_drawn(QgsRectangle(0, 0, 1000, 707.0))
            panel._sheet_drawn(QgsRectangle(1000, 0, 2000, 707.0))
            self.assertEqual(len(panel._sheets), 2)
            self.assertIn("2", panel.sheets_label.text())
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "map.png"
                panel._export_to(str(target), "png")
                names = sorted(path.name for path in Path(directory).iterdir())
                self.assertEqual(names, ["map_1.png", "map_2.png"])
                pdf = Path(directory) / "map.pdf"
                panel._export_to(str(pdf), "pdf")
                self.assertTrue(pdf.exists())
                self.assertGreater(pdf.stat().st_size, 0)
        finally:
            panel.clear_atlas()
            project.clear()
            panel.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

    def test_dock_reprobes_pbf_on_bbox_change(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("pbf"))
        dock._pbf_probe_timer.stop()
        dock.bbox.setText("37.5,55.7,37.7,55.8")
        self.assertTrue(dock._pbf_probe_timer.isActive())
        dock._pbf_probe_timer.stop()
        dock.deleteLater()

    def test_dock_project_name(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.project_name.setText("My Project")
        self.assertEqual(dock.project_name.text(), "My Project")
        self.assertTrue(dock.project_name_hint.isVisibleTo(dock))
        self.assertIn("пробел", dock.project_name_hint.text())
        dock.project_name.setText("my-project_1+2")
        self.assertFalse(dock.project_name_hint.isVisibleTo(dock))
        dock.project_name.setText("Карта_Кавказа-01")
        self.assertFalse(dock.project_name_hint.isVisibleTo(dock))
        dock.project_name.setText("карта!№")
        self.assertTrue(dock.project_name_hint.isVisibleTo(dock))
        self.assertIn("«!»", dock.project_name_hint.text())
        dock.deleteLater()

    def test_dock_geofabrik_timer(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("geofabrik"))
        dock._geofabrik_timer.stop()
        dock.bbox.setText("37.5,55.7,37.7,55.8")
        self.assertTrue(dock._geofabrik_timer.isActive())
        self.assertIsNone(dock._geofabrik_plan)
        dock._geofabrik_timer.stop()
        dock.deleteLater()

    def test_dock_geofabrik_plan_format(self):
        from qgis_xtopo.core import geofabrik
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("geofabrik"))
        item = geofabrik.GeofabrikMap(
            "bayern", "Bayern", "germany",
            "https://download.geofabrik.de/europe/germany/bayern-latest.osm.pbf",
        )
        status = geofabrik.MapStatus(item, True, 100 * 1048576)
        state = geofabrik.IndexState(
            geofabrik.GeofabrikIndex(), 0, "Tue, 20 Oct 2026 04:04:13 GMT"
        )
        plan = geofabrik.GeofabrikPlan(state, [item], [status])
        dock._geofabrik_plan = plan
        dock._apply_geofabrik_plan(plan)
        self.assertEqual(dock.source_value.text(), "bayern")
        self.assertIn("Bayern", dock.geofabrik_maps.item(0).text())
        self.assertIn("100.0 МБ", dock.geofabrik_maps.item(0).text())
        self.assertTrue(dock.geofabrik_maps.item(0).checkState())
        self.assertEqual(dock._geofabrik_selected_ids(), ["bayern"])
        self.assertIn("20.10.2026", dock.geofabrik_group.title())
        self.assertNotIn("Индекс Geofabrik", dock.geofabrik_info.text())
        self.assertIn("100.0 МБ", dock.geofabrik_summary.text())
        estimate = dock._geofabrik_estimate(50 * 1048576, 100 * 1048576)
        self.assertIn("Скачивание", estimate)
        self.assertIn("подготовка", estimate)
        local = dock._geofabrik_estimate(0, 100 * 1048576)
        self.assertNotIn("Скачивание", local)
        dock.deleteLater()

    def test_dock_extent_moved_to_project_group(self):
        from qgis.PyQt.QtWidgets import QGroupBox
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        groups = dock.findChildren(QGroupBox)
        titles = [group.title() for group in groups]
        self.assertNotIn("Область", titles)
        project_group = next(
            group for group in groups if group.title() == "Проект"
        )
        self.assertEqual(dock.area_label.text(), "Охват:")
        # Строка охвата находится внутри группы «Проект»
        self.assertIs(dock.area_label.parentWidget(), project_group)
        dock.deleteLater()

    def test_prepare_without_extent_asks_for_it(self):
        from unittest import mock

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.bbox.setText("")
        calls = []

        def fake_picker():
            calls.append(True)
            return False

        dock._bbox_from_picker = fake_picker
        try:
            dock._prepare()
            # Охват пуст: сначала открывается окно выбора, подготовка не идёт.
            self.assertEqual(calls, [True])
            self.assertIsNone(dock.worker)
            self.assertIn("Охват не выбран", dock.status.text())
        finally:
            dock.deleteLater()

    def test_prepare_with_extent_does_not_ask(self):
        from unittest import mock

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.bbox.setText("13.4,50.6,13.5,50.7")
        calls = []
        dock._bbox_from_picker = lambda: calls.append(True)

        def broken_config():
            raise ValueError("остановка теста")

        dock._build_config = broken_config
        try:
            with mock.patch("qgis_xtopo.gui.dockwidget.QMessageBox.warning"):
                dock._prepare()
            # Охват задан: окно выбора не открывается.
            self.assertEqual(calls, [])
            self.assertIsNone(dock.worker)
        finally:
            dock.deleteLater()

    def test_project_panel_isolines_toggle_glacier(self):
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        regular = QgsVectorLayer(
            "LineString?crs=EPSG:4326", "[TERRAIN] isolines regular", "memory"
        )
        glacier = QgsVectorLayer(
            "LineString?crs=EPSG:4326", "[TERRAIN] isolines glacier", "memory"
        )
        project.addMapLayer(regular)
        project.addMapLayer(glacier)
        panel = ProjectPanel(None)
        try:
            panel.isolines_check.setChecked(True)
            self.assertTrue(panel._layer_visible("[TERRAIN] isolines regular"))
            self.assertTrue(panel._layer_visible("[TERRAIN] isolines glacier"))
            panel.isolines_check.setChecked(False)
            self.assertFalse(panel._layer_visible("[TERRAIN] isolines regular"))
            self.assertFalse(panel._layer_visible("[TERRAIN] isolines glacier"))
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_single_hillshade(self):
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        layer = QgsRasterLayer(
            "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
            "[TERRAIN] hillshade",
            "wms",
        )
        project.addMapLayer(layer)
        panel = ProjectPanel(None)
        try:
            # Отдельного выбора вариантов больше нет
            self.assertFalse(hasattr(panel, "hillshade_1_check"))
            self.assertFalse(hasattr(panel, "hillshade_2_check"))
            self.assertFalse(hasattr(panel, "hillshade_container"))
            self.assertTrue(panel.hillshade_check.isEnabled())
            panel.hillshade_check.setChecked(True)
            self.assertTrue(panel._layer_visible("[TERRAIN] hillshade"))
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_opacity_sliders(self):
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import (
            HILLSHADE_LAYER, SLIGHT_LAYER, STEEP_LAYER, ProjectPanel,
        )

        project = QgsProject.instance()
        project.clear()
        layers = {}
        for name in (STEEP_LAYER, SLIGHT_LAYER, HILLSHADE_LAYER):
            layer = QgsRasterLayer(
                "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
                name,
                "wms",
            )
            layer.setOpacity(0.4)
            project.addMapLayer(layer)
            layers[name] = layer
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            for name, widget in panel._opacity_widgets.items():
                slider = widget.property("slider")
                self.assertEqual(slider.value(), 40, name)
                self.assertTrue(slider.isEnabled(), name)
            # Движение шкалы меняет прозрачность слоя
            widget = panel._opacity_widgets[HILLSHADE_LAYER]
            widget.property("slider").setValue(75)
            self.assertAlmostEqual(
                layers[HILLSHADE_LAYER].opacity(), 0.75, places=6
            )
            self.assertEqual(widget.property("value_label").text(), "75%")
            # Кнопка сброса возвращает исходное значение из шаблона
            reset = widget.property("reset_button")
            self.assertIsNotNone(reset)
            self.assertTrue(reset.isEnabled())
            reset.click()
            self.assertAlmostEqual(
                layers[HILLSHADE_LAYER].opacity(), 0.4, places=6
            )
            self.assertEqual(widget.property("value_label").text(), "40%")
            # У выключенного слоя шкала недоступна
            panel.hillshade_check.setChecked(False)
            self.assertFalse(widget.property("slider").isEnabled())
            panel.hillshade_check.setChecked(True)
            self.assertTrue(widget.property("slider").isEnabled())
        finally:
            panel.deleteLater()
            project.clear()

    def test_set_canvas_magnification(self):
        from qgis_xtopo.core import project as project_module

        class FakeCanvas:
            def __init__(self):
                self.factor = None

            def setMagnificationFactor(self, value):
                self.factor = value

        class FakeIface:
            def __init__(self):
                self.canvas = FakeCanvas()

            def mapCanvas(self):
                return self.canvas

        iface = FakeIface()
        self.assertTrue(project_module.set_canvas_magnification(iface))
        self.assertAlmostEqual(iface.canvas.factor, 1.5, places=6)

        class OldCanvas:
            def setExtent(self, extent):
                pass

        class OldIface:
            def mapCanvas(self):
                return OldCanvas()

        self.assertFalse(project_module.set_canvas_magnification(OldIface()))

    def test_fit_project_extent_default_zoom(self):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
            QgsProject, QgsVectorLayer,
        )
        from qgis_xtopo.core import project as project_module

        self.assertEqual(project_module.DEFAULT_ZOOM, 1.0)
        self.assertEqual(project_module.MAGNIFICATION_FACTOR, 1.5)

        project = QgsProject.instance()
        project.clear()
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "BBOX", "memory")
        feature = QgsFeature(layer.fields())
        feature.setGeometry(
            QgsGeometry.fromWkt("POLYGON ((0 0, 0 10, 10 10, 10 0, 0 0))")
        )
        layer.dataProvider().addFeature(feature)
        project.addMapLayer(layer)

        class FakeSettings:
            def destinationCrs(self):
                return QgsCoordinateReferenceSystem("EPSG:4326")

        class FakeCanvas:
            def __init__(self):
                self.extent = None

            def mapSettings(self):
                return FakeSettings()

            def setExtent(self, extent):
                self.extent = extent

            def refresh(self):
                pass

        class FakeIface:
            def __init__(self):
                self.canvas = FakeCanvas()

            def mapCanvas(self):
                return self.canvas

        iface = FakeIface()
        self.assertTrue(project_module.fit_project_extent(iface))
        # Запас 5% по краям
        self.assertAlmostEqual(
            iface.canvas.extent.width(), 10 * 1.05, places=6
        )
        project.clear()

    def test_project_panel_rule_exclusivity(self):
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        panel = ProjectPanel(None)
        values = {
            5: "5", 10: "10", 25: "25", 50: "50",
            100: "100", 200: "200", 1000: "1000",
        }
        # Зависимости взяты из фильтров правил исходного проекта.
        deps = {
            5: {10, 50, 100, 1000},
            10: {50, 100, 1000},
            25: {50, 100, 1000},
            50: {100, 1000},
            100: {1000},
            200: {1000},
            1000: set(),
        }
        # Общие линии есть только у 5 и 25 (25 не исключён фильтром 5) и
        # у 100 и 200. Кратности 200 и 1000 совместны: правило 200 не
        # рисует линии, кратные 1000, поэтому дубликатов нет.
        conflicts = [(5, 25), (100, 200)]
        available = set(values)
        current = {10, 50, 100, 1000}

        # Базовая кратность задаёт набор кратных, как на вкладке «Рельеф».
        # Недостающая кратность добавляется, иначе в линиях будут пропуски.
        self.assertEqual(
            panel._rule_target(
                True, 10, values, deps, available, current, conflicts
            ),
            {10, 50, 100, 1000},
        )
        self.assertEqual(
            panel._rule_target(
                True, 5, values, deps, available, current, conflicts
            ),
            {5, 10, 50, 100, 1000},
        )
        # 200 нельзя включать вместе со 100: линии совпадают
        self.assertEqual(
            panel._rule_target(
                True, 100, values, deps, available, current, conflicts
            ),
            {100, 1000},
        )
        # 200 и 1000 совместимы: фильтр 200 исключает кратные 1000
        self.assertEqual(
            panel._rule_target(
                True, 200, values, deps, available, current, conflicts
            ),
            {200, 1000},
        )
        self.assertEqual(
            panel._rule_target(
                True, 1000, values, deps, available, current, conflicts
            ),
            {1000},
        )
        # Снятие 50 убирает и 10: его фильтр исключает линии кратности 50
        self.assertEqual(
            panel._rule_target(
                False, 50, values, deps, available, current, conflicts
            ),
            {100, 1000},
        )
        # Снятие 10 оставляет более крупные кратности
        self.assertEqual(
            panel._rule_target(
                False, 10, values, deps, available, current, conflicts
            ),
            {50, 100, 1000},
        )
        # Снятие 1000 убирает все кратности, которые его исключают
        self.assertEqual(
            panel._rule_target(
                False, 1000, values, deps, available, current, conflicts
            ),
            set(),
        )

    def test_project_panel_export_dpi_default(self):
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        panel = ProjectPanel(None)
        self.assertEqual(panel.export_dpi.currentData(), 300)

    def test_project_panel_rule_toggle_applies_exclusivity(self):
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLineSymbol, QgsProject,
            QgsRuleBasedRenderer, QgsVectorLayer,
        )
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326&field=ELEV:integer",
            "[TERRAIN] isolines regular",
            "memory",
        )
        # Фильтры как в исходном проекте: каждое правило не рисует линии
        # более крупных кратностей. Правило «5 м» не исключает кратные 25,
        # поэтому 5 и 25 рисуют общие линии, а 200 не исключает 1000 —
        # значит, 200 и 1000 совместимы.
        filters = {
            1000: '("ELEV" % 1000) = 0 AND "ELEV" != 0',
            200: '("ELEV" % 1000) != 0 AND ("ELEV" % 200) = 0',
            100: '("ELEV" % 1000) != 0 AND ("ELEV" % 100) = 0',
            50: (
                '("ELEV" % 1000) != 0 AND ("ELEV" % 100) != 0 '
                'AND ("ELEV" % 50) = 0'
            ),
            25: (
                '("ELEV" % 1000) != 0 AND ("ELEV" % 100) != 0 '
                'AND ("ELEV" % 50) != 0 AND ("ELEV" % 25) = 0'
            ),
            10: (
                '("ELEV" % 1000) != 0 AND ("ELEV" % 100) != 0 '
                'AND ("ELEV" % 50) != 0 AND ("ELEV" % 10) = 0'
            ),
            5: (
                '("ELEV" % 1000) != 0 AND ("ELEV" % 100) != 0 '
                'AND ("ELEV" % 50) != 0 AND ("ELEV" % 10) != 0 '
                'AND ("ELEV" % 5) = 0'
            ),
        }
        root_rule = QgsRuleBasedRenderer.Rule(None)
        for value in (1000, 200, 100, 50, 25, 10, 5):
            rule = QgsRuleBasedRenderer.Rule(QgsLineSymbol())
            rule.setLabel(str(value))
            rule.setFilterExpression(filters[value])
            rule.setActive(value in (10, 50, 100, 1000))
            root_rule.appendChild(rule)
        layer.setRenderer(QgsRuleBasedRenderer(root_rule))
        for elevation in (5, 10, 25, 50, 100, 200, 1000):
            item = QgsFeature(layer.fields())
            item.setAttribute("ELEV", elevation)
            item.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 1 1)"))
            layer.dataProvider().addFeature(item)
        project.addMapLayer(layer)
        panel = ProjectPanel(None)
        try:
            panel._rebuild_rules()
            self.assertEqual(
                sorted(box.text() for box, _label in panel._rule_checks),
                ["10 м", "100 м", "1000 м", "200 м", "25 м", "5 м", "50 м"],
            )

            def toggle(label, checked):
                for box, name in panel._rule_checks:
                    if name == label:
                        box.setChecked(checked)
                        return
                raise AssertionError(label)

            def active():
                return {
                    label for label, state in panel._rule_states().items() if state
                }

            # Снятие 50 м убирает и 10 м: фильтр правила «10 м»
            # исключает линии кратности 50, иначе в изолиниях пропуск.
            self.assertEqual(active(), {"10", "50", "100", "1000"})
            toggle("50", False)
            self.assertEqual(active(), {"100", "1000"})
            # Обратное включение возвращает набор от базовой кратности
            toggle("10", True)
            self.assertEqual(active(), {"10", "50", "100", "1000"})
            # 200 несовместима со 100: линии совпадают, остаётся мелкая
            toggle("200", True)
            self.assertEqual(active(), {"200", "1000"})
            # Конфликты вычисляются по фильтрам: 200 и 1000 совместимы,
            # общие линии есть только у 5 и 25, а также у 100 и 200.
            self.assertEqual(panel._rule_conflicts(), [(5, 25), (100, 200)])
        finally:
            panel.deleteLater()
            project.clear()

    def test_dock_geofabrik_map_selection(self):
        from qgis.PyQt.QtCore import Qt

        from qgis_xtopo.core import geofabrik
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("geofabrik"))
        chosen = geofabrik.GeofabrikMap(
            "corse", "Corse", "france",
            "https://download.geofabrik.de/europe/france/corse-latest.osm.pbf",
        )
        extra = geofabrik.GeofabrikMap(
            "france", "France", "europe",
            "https://download.geofabrik.de/europe/france-latest.osm.pbf",
        )
        state = geofabrik.IndexState(
            geofabrik.GeofabrikIndex(), 0, "Tue, 20 Oct 2026 04:04:13 GMT"
        )
        plan = geofabrik.GeofabrikPlan(
            state,
            [chosen],
            [geofabrik.MapStatus(chosen, True, 34506434)],
            extra=[extra],
            extra_statuses=[geofabrik.MapStatus(extra, True, 5103585179)],
            parent_names={"corse": "France", "france": "Europe"},
        )
        dock._geofabrik_plan = plan
        dock._apply_geofabrik_plan(plan)
        self.assertEqual(dock.geofabrik_maps.count(), 3)
        self.assertTrue(dock.geofabrik_maps.item(0).checkState())
        self.assertFalse(dock.geofabrik_maps.item(2).checkState())
        self.assertEqual(dock._geofabrik_selected_ids(), ["corse"])
        self.assertIn("32.9 МБ", dock.geofabrik_maps.item(0).text())
        self.assertIn("France", dock.geofabrik_maps.item(0).text())
        self.assertNotIn("corse", dock.geofabrik_maps.item(0).text())
        self.assertNotIn("доступна", dock.geofabrik_maps.item(0).text())
        self.assertIn("Выбрано карт: 1", dock.geofabrik_summary.text())
        dock.geofabrik_maps.item(0).setCheckState(Qt.Unchecked)
        self.assertEqual(dock._geofabrik_selected_ids(), [])
        with self.assertRaises(ValueError):
            dock._build_config()
        dock.deleteLater()

    def test_dock_build_config_geofabrik(self):
        import tempfile

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        with tempfile.TemporaryDirectory() as directory:
            dock = XtopoDockWidget(None)
            dock.projects_dir.setText(directory)
            dock.project_name.setText("demo")
            dock.bbox.setText("1,2,3,4")
            dock.source_mode.setCurrentIndex(
                dock.source_mode.findData("geofabrik")
            )
            config = dock._build_config()
            self.assertEqual(config.source_mode, "geofabrik")
            self.assertEqual(config.pbf_path, "")
            dock.deleteLater()

    def test_coastline_line_geometries(self):
        from osgeo import ogr
        from qgis_xtopo.core.coastline import _line_geometries

        polygon = ogr.CreateGeometryFromWkt(
            "POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))"
        )
        lines = _line_geometries(polygon)
        self.assertTrue(lines)
        for geometry in lines:
            self.assertIn(geometry.GetGeometryName(), ("LINESTRING", "LINEARRING"))
        self.assertEqual(_line_geometries(None), [])

    def test_extent_dialog(self):
        from qgis_xtopo.gui.extentdialog import ExtentDialog

        dialog = ExtentDialog("37.5,55.7,37.7,55.8")
        from qgis.PyQt.QtWidgets import QApplication

        available = QApplication.primaryScreen().availableGeometry()
        self.assertGreaterEqual(dialog.width(), int(available.width() * 0.9))
        self.assertGreaterEqual(dialog.height(), int(available.height() * 0.9))
        self.assertLessEqual(dialog.width(), available.width())
        self.assertLessEqual(dialog.height(), available.height())
        self.assertTrue(dialog.layer.isValid())
        self.assertFalse(hasattr(dialog, "pan_button"))
        self.assertFalse(hasattr(dialog, "zoom_in_button"))
        self.assertFalse(hasattr(dialog, "zoom_out_button"))
        self.assertFalse(dialog.draw_button.icon().isNull())
        self.assertEqual(dialog.draw_button.text(), "")
        self.assertIn("bbox", dialog.draw_button.toolTip())
        # Запомненный bbox рисуется при открытии окна
        self.assertIsNotNone(dialog._drawn_extent)
        self.assertTrue(dialog.reset_button.isEnabled())
        bbox = dialog._extent_as_bbox()
        self.assertAlmostEqual(bbox.lon_min, 37.5, places=3)
        self.assertAlmostEqual(bbox.lon_max, 37.7, places=3)
        self.assertAlmostEqual(bbox.lat_min, 55.7, places=3)
        self.assertAlmostEqual(bbox.lat_max, 55.8, places=3)
        canvas_extent = dialog.canvas.extent()
        dialog._rectangle_drawn(canvas_extent)
        self.assertIsNotNone(dialog._drawn_extent)
        self.assertTrue(dialog.reset_button.isEnabled())
        self.assertAlmostEqual(
            dialog._drawn_extent.xMinimum(), canvas_extent.xMinimum(), places=1
        )
        dialog._reset_drawn()
        self.assertIsNone(dialog._drawn_extent)
        self.assertFalse(dialog.reset_button.isEnabled())
        dialog.done(0)

    def test_synthetic_osm(self):
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pipeline import run_project

        osm = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
 <node id="1" lat="42.90" lon="44.98"><tag k="place" v="city"/></node>
 <node id="2" lat="42.86" lon="44.96"><tag k="natural" v="peak"/></node>
 <node id="3" lat="42.86" lon="44.96"/>
 <node id="4" lat="42.86" lon="44.98"/>
 <node id="5" lat="42.88" lon="44.98"/>
 <node id="6" lat="42.88" lon="44.96"/>
 <way id="10"><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="6"/><nd ref="3"/>
   <tag k="natural" v="water"/></way>
 <way id="11"><nd ref="4"/><nd ref="5"/><tag k="waterway" v="river"/></way>
</osm>
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.osm"
            source.write_text(osm, encoding="utf-8")
            config = Config(
                qgis_projects_dir=str(root / "projects"),
                project_name="test",
                bbox="44.95,42.85,45.05,42.95",
                array_queries=["peak", "water", "river", "places_main"],
                source_mode="pbf",
                pbf_path=str(source),
            )
            project_path = run_project(config)
            self.assertTrue(project_path.exists())
            for name in ("peak", "water", "water_dissolved", "river", "places_main"):
                self.assertTrue((config.vector_dir / f"{name}.sqlite").exists(), name)

            with zipfile.ZipFile(project_path) as archive:
                name = next(
                    item for item in archive.namelist() if item.endswith(".qgs")
                )
                text = archive.read(name).decode("utf-8")
            datasources = re.findall(r"<datasource>(.*?)</datasource>", text, re.S)
            self.assertTrue(datasources)
            for datasource in datasources:
                value = html.unescape(datasource).split("|", 1)[0]
                if "=" in value:
                    continue
                self.assertTrue(
                    (project_path.parent / value).exists(),
                    f"unavailable layer source: {value}",
                )

    def test_project_is_regenerated_from_template(self):
        from qgis_xtopo.core import project as project_module
        from qgis_xtopo.core.config import Config

        sentinel = "glacier_glacier_cf870278"

        def read_project(path):
            with zipfile.ZipFile(path) as archive:
                name = next(
                    item for item in archive.namelist() if item.endswith(".qgs")
                )
                return archive.read(name).decode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                qgis_projects_dir=directory,
                project_name="regen",
                bbox="44.95,42.85,45.05,42.95",
                array_queries=["peak"],
                source_mode="pbf",
                pbf_path="/nonexistent.osm",
            )
            path = project_module.prepare_project(config)
            self.assertIn(sentinel, read_project(path))

            # Simulate a stale project: with no vector data every local layer
            # is dropped from the file.
            self.assertTrue(project_module.clean_project_layers(path))
            self.assertNotIn(sentinel, read_project(path))

            # The next preparation must start from the template again and
            # bring back layers that were pruned on an earlier run.
            project_module.prepare_project(config)
            self.assertIn(sentinel, read_project(path))


if __name__ == "__main__":
    unittest.main()
