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
        from tests import qgis_app

        cls.app = qgis_app()

    @classmethod
    def tearDownClass(cls):
        from qgis.core import QgsProject

        # QGIS 3.34 can crash while destroying the default project style
        # during exitQgis() if widgets/models created by the tests are still
        # alive.  Drop the project data and pending deferred deletions first.
        cls.app.processEvents()
        QgsProject.instance().clear()
        cls.app.processEvents()

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
        self.assertEqual(dock.overpass_server.count(), 5)
        self.assertEqual(
            dock.overpass_server.itemData(1),
            "https://overpass.private.coffee/api/interpreter",
        )
        self.assertEqual(dock.source_mode.count(), 5)
        self.assertEqual(dock.source_mode.itemData(0), "geofabrik")
        self.assertEqual(
            dock.source_mode.itemText(0), "Карты с Geofabrik (предпочтительно)"
        )
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("geofabrik"))
        self.assertEqual(dock.source_label.text(), "")
        self.assertFalse(dock.source_label.isVisibleTo(dock))
        self.assertFalse(dock.source_value.isVisibleTo(dock))
        self.assertFalse(dock.source_value.isEnabled())
        self.assertFalse(dock.browse_source.isVisibleTo(dock))
        self.assertFalse(dock.download_pbf_button.isVisibleTo(dock))
        self.assertFalse(hasattr(dock, "download_maps_button"))
        self.assertTrue(dock.geofabrik_info.isVisibleTo(dock))
        self.assertFalse(dock.close_button.isEnabled())
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("pbf"))
        self.assertTrue(dock.source_value.isVisibleTo(dock))
        self.assertIn(".pbf", dock.source_value.placeholderText())
        self.assertIn("охват", dock.source_hint.text())
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
        # Строка статуса больше не фиксирована по высоте: длинный
        # прогресс с ETA переносится и не обрезается.
        self.assertGreater(
            dock.status.maximumHeight(), dock.status.minimumHeight()
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

    def test_glacier_isolines_follow_interval_checkboxes(self):
        """Галочки «Кратность» действуют и на изолинии ледников.

        Изолинии ледников рисуются теми же интервалами, что и обычные,
        поэтому включение и снятие кратности должно менять оба слоя.
        """
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsLineSymbol, QgsProject,
            QgsRuleBasedRenderer, QgsVectorLayer,
        )
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        project = QgsProject.instance()
        project.clear()
        dock = XtopoDockWidget(None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                crop = Path(directory) / "crop.geojson"
                crop.write_text(
                    '{"type":"FeatureCollection","features":[{"type":"Feature",'
                    '"properties":{},"geometry":{"type":"Polygon","coordinates":'
                    '[[[0,0],[0,1],[1,1],[1,0],[0,0]]]}}]}',
                    encoding="utf-8",
                )
                project.addMapLayer(
                    QgsVectorLayer(str(crop), "BBOX", "ogr")
                )
                layers = {}
                for name in (
                    "[TERRAIN] isolines regular",
                    "[TERRAIN] isolines glacier",
                ):
                    layer = QgsVectorLayer(
                        "LineString?crs=EPSG:4326&field=ELEV:integer",
                        name,
                        "memory",
                    )
                    root_rule = QgsRuleBasedRenderer.Rule(None)
                    for value in (50, 10, 5):
                        rule = QgsRuleBasedRenderer.Rule(QgsLineSymbol())
                        rule.setLabel(str(value))
                        rule.setFilterExpression(f'"ELEV" % {value} = 0')
                        rule.setActive(value != 5)
                        root_rule.appendChild(rule)
                    layer.setRenderer(QgsRuleBasedRenderer(root_rule))
                    feature = QgsFeature(layer.fields())
                    feature.setAttribute("ELEV", 25)
                    feature.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 1 1)"))
                    layer.dataProvider().addFeature(feature)
                    layers[name] = layer
                project.addMapLayers(list(layers.values()))

                dock._refresh_project_state()
                panel = dock.project_panel
                self.assertEqual(len(panel._rule_checks), 3)

                regular = layers["[TERRAIN] isolines regular"]
                glacier = layers["[TERRAIN] isolines glacier"]

                def active(layer):
                    return {
                        rule.label(): rule.active()
                        for rule in layer.renderer().rootRule().children()
                    }

                self.assertFalse(active(glacier)["5"])
                box5 = next(
                    box for box, label in panel._rule_checks if label == "5"
                )
                box5.setChecked(True)
                self.assertTrue(active(glacier)["5"])
                self.assertEqual(active(glacier), active(regular))

                box5.setChecked(False)
                self.assertFalse(active(glacier)["5"])
                self.assertEqual(active(glacier), active(regular))
        finally:
            dock._project_timer.stop()
            project.clear()
            dock.deleteLater()

    def test_scale_bar_length_and_label(self):
        """Шкала масштаба подбирает «круглое» расстояние."""
        from qgis_xtopo.gui.extentdialog import scale_bar

        length, text = scale_bar(10.0)
        self.assertAlmostEqual(length, 100.0)
        self.assertEqual(text, "1 км")
        length, text = scale_bar(0.5)
        self.assertEqual(text, "50 м")
        # Невозможные значения не дают полосы.
        self.assertEqual(scale_bar(0)[0], 0.0)

    def test_atlas_sheet_shows_its_size(self):
        """Вдоль границ листа атласа показывается его размер."""
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsRectangle,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.atlas import SheetSizeLabels

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        labels = SheetSizeLabels(canvas)
        # Прямоугольник примерно 10 x 10 км в метрах EPSG:3857.
        labels.set_rectangle(
            QgsRectangle(0, 0, 10000, 10000),
            QgsCoordinateReferenceSystem("EPSG:3857"),
        )
        self.assertIn("км", labels.width_text)
        self.assertIn("км", labels.height_text)
        # 10 000 метров в EPSG:3857 — это ровно 10 км на местности.
        value = float(labels.width_text.split()[0].replace(",", "."))
        self.assertGreater(value, 9.5)
        self.assertLess(value, 10.5)

    def test_project_panel_has_qgis_measure_and_identify(self):
        """В панели инструментов есть штатные инструменты QGIS."""
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        panel = ProjectPanel(None)
        try:
            names = [
                action.objectName() or action.text()
                for action in panel.toolbar.actions()
            ]
            self.assertTrue(
                any("мерить" in name.lower() for name in names),
                "в панели нет инструмента измерения",
            )
            self.assertTrue(
                any("определ" in name.lower() for name in names),
                "в панели нет инструмента определения объектов",
            )
            # Меню измерения предлагает все режимы QGIS.
            menu = panel.measure_action.menu()
            self.assertIsNotNone(menu)
            titles = [action.text() for action in menu.actions()]
            self.assertEqual(len(titles), 4)
        finally:
            panel.deleteLater()

    def test_short_status_keeps_path_tail(self):
        """Длинный путь в строке статуса обрезается, но читается."""
        from qgis_xtopo.gui.dockwidget import _short_status

        short = _short_status("Готово")
        self.assertEqual(short, "Готово")
        long = _short_status(
            "Готово: /home/user/qgis_projects/automap_test/automap_test.qgz"
        )
        self.assertLessEqual(len(long), 58)
        self.assertIn("…", long)
        self.assertTrue(long.endswith("automap_test.qgz"))

        # Прогресс не сокращается: иначе обрежется ETA в конце строки.
        progress = (
            "Загрузка карт: 18% (117/623 МБ) · 0/1 · 2.2 МБ/с · ~4 мин"
        )
        self.assertEqual(_short_status(progress), progress)

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
            dock.dem_source.setCurrentIndex(
                dock.dem_source.findData("srtm")
            )
            dock.dem_dir.clear()
            dock._set_isolines_step(10)
            self.assertEqual(dock._current_isolines_step(), 10)
            self.assertFalse(dock.isolines_steps[5].isChecked())
            self.assertTrue(dock.isolines_steps[10].isChecked())
            self.assertTrue(dock.isolines_steps[50].isChecked())

            # Отметка 5 м включает все кратные (зависимость как раньше).
            dock.isolines_steps[5].click()
            self.assertTrue(
                all(box.isChecked() for box in dock.isolines_steps.values())
            )
            self.assertEqual(dock._current_isolines_step(), 5)

            # Снятие убирает галочки по одной, в любом порядке.
            dock.isolines_steps[10].setChecked(False)
            checked = sorted(
                value for value, box in dock.isolines_steps.items()
                if box.isChecked()
            )
            self.assertEqual(checked, [5, 25, 50, 100, 200])
            dock.isolines_steps[25].setChecked(False)
            checked = sorted(
                value for value, box in dock.isolines_steps.items()
                if box.isChecked()
            )
            self.assertEqual(checked, [5, 50, 100, 200])
            self.assertEqual(dock._current_isolines_step(), 5)

            # Последнюю отметку снять нельзя.
            dock._set_isolines_step(200)
            dock.isolines_steps[200].setChecked(False)
            self.assertTrue(dock.isolines_steps[200].isChecked())

            dock.dem_source.setCurrentIndex(
                dock.dem_source.findData("manual")
            )
            self.assertFalse(dock.dem_dir_row.isHidden())
            dock.dem_dir.setText("/tmp/dem")
            self.assertIn("не найден", dock.dem_info.text())
            with tempfile.TemporaryDirectory() as dem:
                dock.dem_dir.setText(dem)
                self.assertIn("каталог выбран", dock.dem_info.text())
            dock.dem_source.setCurrentIndex(
                dock.dem_source.findData("srtm")
            )
            self.assertTrue(dock.dem_dir_row.isHidden())
            self.assertIn("NASA/USGS", dock.dem_info.text())

            # Число тайлов должно быть в короткой строке доступности,
            # а не теряться при переносе длинного текста.
            from qgis_xtopo.core import dem_sources

            dock._dem_availability = {
                "srtm": dem_sources.Availability(
                    available=True, checked=True,
                    message="весь охват, тайлов: 2",
                ),
            }
            dock._dem_check_message = ""
            dock._update_dem_info()
            self.assertIn("тайлов: 2", dock.dem_info.text())
            with tempfile.TemporaryDirectory() as directory:
                dock.projects_dir.setText(directory)
                dock.project_name.setText("demo")
                dock.bbox.setText("1,2,3,4")
                dock.source_mode.setCurrentIndex(
                    dock.source_mode.findData("geofabrik")
                )
                dock.dem_source.setCurrentIndex(
                    dock.dem_source.findData("manual")
                )
                dock.dem_dir.setText(str(Path(directory) / "missing"))
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

    def test_sheet_edit_tool_move_and_resize(self):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsPointXY, QgsRectangle,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.atlas import SheetEditMapTool

        canvas = QgsMapCanvas()
        canvas.resize(800, 600)
        crs = QgsCoordinateReferenceSystem("EPSG:3857")
        canvas.setDestinationCrs(crs)
        canvas.setExtent(QgsRectangle(0, 0, 1000, 1000))
        original = QgsRectangle(100, 100, 400, 300)
        tool = SheetEditMapTool(canvas, [(original, crs)], 1.5)
        try:
            self.assertEqual(len(tool.handle_positions()), 4)
            tool._index = 0
            tool._corner = None
            tool._original = QgsRectangle(original)
            tool._press_point = QgsPointXY(150, 150)
            tool._operation = "move"
            moved = tool._current_rectangle(QgsPointXY(200, 180))
            self.assertEqual(moved, QgsRectangle(150, 130, 450, 330))

            tool._corner = 2
            tool._operation = "resize"
            resized = tool._current_rectangle(QgsPointXY(550, 400))
            self.assertEqual(resized.xMinimum(), 100)
            self.assertEqual(resized.yMinimum(), 100)
            self.assertAlmostEqual(resized.width() / resized.height(), 1.5)
            self.assertGreater(resized.width(), original.width())
            self.assertEqual(tool._cursor_for_hit(0, 0), "SizeBDiagCursor")
            self.assertEqual(tool._cursor_for_hit(0, 2), "SizeBDiagCursor")
            self.assertEqual(tool._cursor_for_hit(0, 1), "SizeFDiagCursor")
            self.assertEqual(tool._cursor_for_hit(0, 3), "SizeFDiagCursor")
            self.assertEqual(tool._cursor_for_hit(0, None), "SizeAllCursor")
        finally:
            tool.deactivate()
            tool.dispose()
            canvas.deleteLater()
            self.app.processEvents()

    def test_atlas_active_sheet_selection_and_delete(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        class Iface:
            def __init__(self, canvas):
                self.canvas = canvas

            def mapCanvas(self):
                return self.canvas

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        panel = ProjectPanel(Iface(canvas))
        try:
            for right in (100, 200, 300):
                panel._sheet_drawn(QgsRectangle(0, 0, right, 70))
            panel.edit_sheet_button.setChecked(True)
            panel._sheet_activated(1)
            self.assertEqual(panel._active_sheet_index, 1)
            self.assertIn("выбран 2", panel.sheets_label.text())
            self.assertEqual(panel._sheet_items[1][0].strokeColor(), QColor(220, 140, 20))
            self.assertEqual(panel._sheet_items[0][0].strokeColor(), QColor(0, 120, 215))

            panel._remove_last_sheet()
            self.assertEqual(
                [rectangle.xMaximum() for rectangle, _crs in panel._sheets],
                [100, 300],
            )
            self.assertEqual(panel._active_sheet_index, 1)
            self.assertEqual(
                [
                marker.number
                for _band, marker, _labels in panel._sheet_items
            ], [1, 2]
            )
            self.assertEqual(panel._sheet_items[1][0].strokeColor(), QColor(220, 140, 20))

            panel.edit_sheet_button.setChecked(False)
            self.assertIsNone(panel._active_sheet_index)
            panel._remove_last_sheet()
            self.assertEqual(len(panel._sheets), 1)
            self.assertEqual(panel._sheets[0][0].xMaximum(), 100)
        finally:
            panel.clear_atlas()
            panel.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

    def test_clear_atlas_activates_pan_tool(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        class Iface:
            def __init__(self, canvas):
                self.canvas = canvas

            def mapCanvas(self):
                return self.canvas

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        panel = ProjectPanel(Iface(canvas))
        try:
            panel._sheet_drawn(QgsRectangle(0, 0, 100, 70))
            panel.edit_sheet_button.setChecked(True)
            self.assertIs(canvas.mapTool(), panel._edit_sheet_tool)
            panel.clear_sheets_button.click()
            self.assertEqual(len(panel._sheets), 0)
            self.assertFalse(panel.add_sheet_button.isChecked())
            self.assertFalse(panel.edit_sheet_button.isChecked())
            # После очистки карта снова таскается штатной прокруткой.
            self.assertTrue(panel.pan_action.isChecked())
            self.assertIsNotNone(panel._pan_tool)
            self.assertIs(canvas.mapTool(), panel._pan_tool)
        finally:
            panel.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

    def test_measure_dialog_close_activates_pan_tool(self):
        from unittest import mock

        from qgis.gui import QgsMapCanvas, QgsMapTool
        from qgis.PyQt.QtWidgets import QDialog
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        class Iface:
            def __init__(self, canvas):
                self.canvas = canvas

            def mapCanvas(self):
                return self.canvas

        class QgsMeasureTool(QgsMapTool):
            # Имя Python-класса попадает в metaObject(): по нему панель
            # отличает измерительный инструмент от остальных.
            pass

        canvas = QgsMapCanvas()
        panel = ProjectPanel(Iface(canvas))
        dialog = QDialog()
        tool = QgsMeasureTool(canvas)
        try:
            canvas.setMapTool(tool)
            dialog.installEventFilter(panel)
            dialog.show()
            self.app.processEvents()
            # Скрытие окна при смене инструмента — не закрытие.
            dialog.hide()
            self.app.processEvents()
            self.assertFalse(panel.pan_action.isChecked())
            self.assertIs(canvas.mapTool(), tool)
            dialog.show()
            self.app.processEvents()
            with mock.patch.object(
                ProjectPanel,
                "_is_measure_dialog",
                staticmethod(lambda widget: widget is dialog),
            ):
                dialog.close()
                self.app.processEvents()
            # После закрытия окна карта снова таскается штатной прокруткой.
            self.assertTrue(panel.pan_action.isChecked())
            self.assertIsNotNone(panel._pan_tool)
            self.assertIs(canvas.mapTool(), panel._pan_tool)
        finally:
            panel.deleteLater()
            dialog.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

    def test_measure_dialog_close_keeps_other_tool(self):
        from unittest import mock

        from qgis.gui import QgsMapCanvas, QgsMapTool
        from qgis.PyQt.QtWidgets import QDialog
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        class Iface:
            def __init__(self, canvas):
                self.canvas = canvas

            def mapCanvas(self):
                return self.canvas

        canvas = QgsMapCanvas()
        panel = ProjectPanel(Iface(canvas))
        dialog = QDialog()
        tool = QgsMapTool(canvas)
        try:
            canvas.setMapTool(tool)
            dialog.installEventFilter(panel)
            dialog.show()
            self.app.processEvents()
            with mock.patch.object(
                ProjectPanel,
                "_is_measure_dialog",
                staticmethod(lambda widget: widget is dialog),
            ):
                dialog.close()
                self.app.processEvents()
            # Другой инструмент не снимаем: переключаем только измерение.
            self.assertFalse(panel.pan_action.isChecked())
            self.assertIs(canvas.mapTool(), tool)
        finally:
            panel.deleteLater()
            dialog.deleteLater()
            canvas.deleteLater()
            self.app.processEvents()

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
            # прямоугольник сжат по тексту, а не растянут на всю ширину
            expected = copyright_item.sizeForText()
            self.assertAlmostEqual(size.width(), expected.width(), places=3)
            self.assertAlmostEqual(size.height(), expected.height(), places=3)
            self.assertLess(size.width(), 210.0)
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
            self.assertTrue(panel.edit_sheet_button.isEnabled())
            self.assertEqual(panel.atlas_group.layout().itemAt(2).count(), 4)
            panel.edit_sheet_button.setChecked(True)
            self.assertIs(canvas.mapTool(), panel._edit_sheet_tool)
            panel._sheet_edited(0, QgsRectangle(0, 0, 900, 636.3))
            self.assertEqual(panel._sheets[0][0].xMaximum(), 900)
            panel.edit_sheet_button.setChecked(False)
            messages = []
            panel.statusMessage.connect(messages.append)
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "map.png"
                panel._export_to(str(target), "png")
                names = sorted(path.name for path in Path(directory).iterdir())
                self.assertEqual(names, ["map_1.png", "map_2.png"])
                self.assertTrue(
                    any("лист 1 из 2" in message for message in messages),
                    messages,
                )
                self.assertTrue(
                    any("лист 2 из 2" in message for message in messages),
                    messages,
                )
                self.assertFalse(
                    any("лист -" in message for message in messages),
                    messages,
                )
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

    def test_export_png_without_atlas(self):
        from osgeo import gdal
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsLayoutItemMap, QgsLayoutPoint,
            QgsLayoutSize, QgsPrintLayout, QgsProject, QgsUnitTypes,
        )
        from qgis.gui import QgsMapCanvas
        from qgis.PyQt.QtGui import QImage
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
        errors = []
        gdal.PushErrorHandler(
            lambda _class, _number, message: errors.append(message)
        )
        try:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "map.png"
                panel._export_to(str(target), "png")
                self.assertTrue(target.exists())
                self.assertGreater(target.stat().st_size, 0)
                self.assertFalse(QImage(str(target)).isNull())
        finally:
            gdal.PopErrorHandler()
            panel.deleteLater()
            project.clear()
            canvas.deleteLater()
            self.app.processEvents()
        self.assertFalse(
            any("does not support update access" in error for error in errors),
            errors,
        )

    def test_dock_reprobes_pbf_on_bbox_change(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("pbf"))
        dock._pbf_probe_timer.stop()
        dock.bbox.setText("37.5,55.7,37.7,55.8")
        self.assertTrue(dock._pbf_probe_timer.isActive())
        dock._pbf_probe_timer.stop()
        dock.deleteLater()

    def test_dock_auto_selects_3dep_and_warns(self):
        from qgis_xtopo.core.bbox import BBox
        from qgis_xtopo.core.dem_sources import Availability
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        bbox = BBox(-76.2538, 35.8625, -63.1295, 36.6597)
        required = frozenset({(35, -77), (35, -76), (35, -75),
                              (36, -77), (36, -76), (36, -75)})
        land = frozenset({(35, -77), (35, -76), (36, -77), (36, -76)})
        availability = {
            "3dep": Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
                missing_tiles=required - land,
            ),
            "alos": Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
            "cop30": Availability(
                available=True, checked=True,
                required_tiles=required, covered_tiles=land,
            ),
            "srtm": Availability(available=True, checked=True),
        }
        dock = XtopoDockWidget(None)
        try:
            dock._dem_check_timer.stop()
            dock._dem_bbox = bbox
            dock._dem_availability_generation = 3
            dock._dem_availability_ready(3, availability)
            self.assertEqual(dock.dem_source.currentData(), "3dep")

            # Ручной выбор другого источника даёт предупреждение.
            dock.dem_source.setCurrentIndex(
                dock.dem_source.findData("srtm")
            )
            self.assertFalse(dock.dem_warning.isHidden())
            self.assertIn("3DEP", dock.dem_warning.text())

            # Смена охвата сбрасывает предупреждение до новой проверки.
            dock.bbox.setText("-65.0292,32.1998,-64.567,32.5081")
            dock._dem_check_timer.stop()
            self.assertTrue(dock.dem_warning.isHidden())
        finally:
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
        self.assertIn("20.10.2026", dock.geofabrik_title.text())
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

    def test_log_window_and_noise_filter(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        try:
            dock._append_log("Первый этап")
            dock._append_log("Результаты: {'OUTPUT': 'layer.gpkg'}")
            dock._append_log("Results: {'OUTPUT': 'layer.gpkg'}")
            dock._append_log("Второй этап")
            self.assertNotIn("Результаты", dock.log.toPlainText())
            self.assertNotIn("Results", dock.log.toPlainText())
            dock._open_log_window()
            self.assertTrue(dock.log_window.isVisible())
            self.assertEqual(
                dock.log_window_text.toPlainText(), dock.log.toPlainText()
            )
            dock._append_log("Третий этап")
            self.assertEqual(
                dock.log_window_text.toPlainText(), dock.log.toPlainText()
            )
        finally:
            if dock.log_window is not None:
                dock.log_window.close()
            dock.deleteLater()

    def test_extent_button_is_plain_and_disabled_while_running(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        try:
            self.assertTrue(dock.choose_extent.isEnabled())
            self.assertTrue(dock.choose_extent.styleSheet())
            dock._set_running(True)
            self.assertFalse(dock.choose_extent.isEnabled())
            self.assertEqual(dock.choose_extent.styleSheet(), "")
            dock._set_running(False)
            self.assertTrue(dock.choose_extent.isEnabled())
            self.assertTrue(dock.choose_extent.styleSheet())
        finally:
            dock.deleteLater()

    def test_close_project_button(self):
        from qgis.core import QgsProject, QgsVectorLayer
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        project = QgsProject.instance()
        project.clear()
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "BBOX", "memory")
        project.addMapLayer(layer)
        dock = XtopoDockWidget(None)
        dock._own_marker = True
        dock._refresh_project_state()
        try:
            self.assertTrue(dock._own_project_open)
            self.assertTrue(dock.close_button.isEnabled())
            self.assertFalse(dock.open_button.isEnabled())
            dock._close_project()
            self.assertEqual(len(project.mapLayers()), 0)
            dock._refresh_project_state()
            self.assertFalse(dock.close_button.isEnabled())
            self.assertTrue(dock.open_button.isEnabled())
        finally:
            project.clear()
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

    def test_project_panel_nature_reserve_and_labels(self):
        from qgis.core import (
            QgsPalLayerSettings, QgsProject, QgsVectorLayer,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        layer = QgsVectorLayer(
            "Polygon?crs=EPSG:4326&field=name:string", "nature_reserve", "memory"
        )
        layer.setLabeling(QgsVectorLayerSimpleLabeling(QgsPalLayerSettings()))
        layer.setLabelsEnabled(False)
        project.addMapLayer(layer)
        project.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(False)
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            self.assertFalse(panel.nature_reserve_check.isChecked())
            self.assertFalse(panel.nature_reserve_labels_check.isChecked())
            self.assertFalse(panel.nature_reserve_labels_check.isEnabled())

            panel.nature_reserve_check.setChecked(True)
            self.assertTrue(panel._layer_visible("nature_reserve"))
            self.assertTrue(panel.nature_reserve_labels_check.isEnabled())
            self.assertFalse(layer.labelsEnabled())

            panel.nature_reserve_labels_check.setChecked(True)
            self.assertTrue(layer.labelsEnabled())
            panel.nature_reserve_check.setChecked(False)
            self.assertFalse(panel.nature_reserve_labels_check.isEnabled())
            self.assertFalse(panel._layer_visible("nature_reserve"))
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
            # Старый шаблон: единственный слой без вариантов
            self.assertFalse(hasattr(panel, "hillshade_1_check"))
            self.assertFalse(hasattr(panel, "hillshade_2_check"))
            self.assertFalse(hasattr(panel, "hillshade_container"))
            panel._sync_from_project()
            self.assertEqual(len(panel.hillshade_variant_menu.actions()), 1)
            self.assertEqual(
                panel._active_hillshade_layer(), "[TERRAIN] hillshade"
            )
            self.assertTrue(panel.hillshade_check.isEnabled())
            panel.hillshade_check.setChecked(True)
            self.assertTrue(panel._layer_visible("[TERRAIN] hillshade"))
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_hillshade_variants(self):
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import (
            HILLSHADE_VARIANTS, ProjectPanel,
        )

        project = QgsProject.instance()
        project.clear()
        layers = {}
        for name, _label in HILLSHADE_VARIANTS:
            layer = QgsRasterLayer(
                "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0",
                name,
                "wms",
            )
            project.addMapLayer(layer)
            layers[name] = layer
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            labels = [
                action.text()
                for action in panel.hillshade_variant_menu.actions()
            ]
            self.assertEqual(
                labels, ["Вариант 1 (равнины)", "Вариант 2 (горы)"]
            )
            # По умолчанию выбран светлый вариант для равнин
            self.assertEqual(
                panel._active_hillshade_layer(), HILLSHADE_VARIANTS[0][0]
            )
            self.assertEqual(panel.hillshade_variant_button.text(), "(1) ▼")
            self.assertTrue(panel.hillshade_check.isChecked())
            self.assertTrue(panel._layer_visible(HILLSHADE_VARIANTS[0][0]))
            self.assertFalse(panel._layer_visible(HILLSHADE_VARIANTS[1][0]))
            # Смена варианта переносит видимость
            panel.hillshade_variant_menu.actions()[1].trigger()
            self.assertTrue(panel._layer_visible(HILLSHADE_VARIANTS[1][0]))
            self.assertFalse(panel._layer_visible(HILLSHADE_VARIANTS[0][0]))
            self.assertEqual(panel.hillshade_variant_button.text(), "(2) ▼")
            # Снятие галочки прячет все варианты, включение возвращает выбранный
            panel.hillshade_check.setChecked(False)
            for name, _label in HILLSHADE_VARIANTS:
                self.assertFalse(panel._layer_visible(name))
            panel.hillshade_check.setChecked(True)
            self.assertTrue(panel._layer_visible(HILLSHADE_VARIANTS[1][0]))
            self.assertFalse(panel._layer_visible(HILLSHADE_VARIANTS[0][0]))
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
            # У склонов шкала задаёт прозрачность слоя
            for name in (STEEP_LAYER, SLIGHT_LAYER):
                widget = panel._opacity_widgets[name]
                slider = widget.property("slider")
                self.assertEqual(slider.value(), 40, name)
                self.assertTrue(slider.isEnabled(), name)
            widget = panel._opacity_widgets[STEEP_LAYER]
            widget.property("slider").setValue(75)
            self.assertAlmostEqual(layers[STEEP_LAYER].opacity(), 0.75, places=6)
            self.assertEqual(widget.property("value_label").text(), "75%")
            # Кнопка сброса возвращает исходное значение из шаблона
            reset = widget.property("reset_button")
            self.assertIsNotNone(reset)
            self.assertTrue(reset.isEnabled())
            reset.click()
            self.assertAlmostEqual(layers[STEEP_LAYER].opacity(), 0.4, places=6)
            self.assertEqual(widget.property("value_label").text(), "40%")
            # У затенения рельефа шкала задаёт прозрачность теней: 50% —
            # исходный вид, меньше — темнее
            widget = panel._opacity_widgets[HILLSHADE_LAYER]
            self.assertEqual(widget.property("slider").value(), 50)
            self.assertTrue(widget.property("slider").isEnabled())
            # У выключенного слоя шкала недоступна
            panel.hillshade_check.setChecked(False)
            self.assertFalse(widget.property("slider").isEnabled())
            panel.hillshade_check.setChecked(True)
            self.assertTrue(widget.property("slider").isEnabled())
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_slope_variants(self):
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import (
            STEEP_LAYER, STEEP_VARIANTS, ProjectPanel,
        )

        project = QgsProject.instance()
        project.clear()
        layers = {}
        for name, _label in STEEP_VARIANTS:
            layer = QgsRasterLayer(
                "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0",
                name,
                "wms",
            )
            layer.setOpacity(0.4)
            project.addMapLayer(layer)
            layers[name] = layer
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            menu = panel.steep_variant_menu
            labels = [action.text() for action in menu.actions()]
            self.assertEqual(labels, ["Горы", "Предгорья", "Равнины"])
            # По умолчанию выбран первый вариант
            self.assertEqual(panel._active_steep_layer(), STEEP_VARIANTS[0][0])
            self.assertEqual(panel.steep_variant_button.text(), "(Г) \u25bc")
            self.assertTrue(panel.steep_check.isEnabled())
            # Пока опция выключена, выбор варианта заблокирован
            panel.steep_check.setChecked(False)
            self.assertFalse(panel.steep_variant_button.isEnabled())
            # Галочка показывает только выбранный вариант
            panel.steep_check.setChecked(True)
            self.assertTrue(panel.steep_variant_button.isEnabled())
            self.assertTrue(panel._layer_visible(STEEP_VARIANTS[0][0]))
            self.assertFalse(panel._layer_visible(STEEP_VARIANTS[1][0]))
            self.assertFalse(panel._layer_visible(STEEP_VARIANTS[2][0]))
            # Смена варианта переносит видимость и прозрачность
            menu.actions()[1].trigger()
            self.assertTrue(panel._layer_visible(STEEP_VARIANTS[1][0]))
            self.assertFalse(panel._layer_visible(STEEP_VARIANTS[0][0]))
            self.assertEqual(panel.steep_variant_button.text(), "(П) \u25bc")
            slider = panel._opacity_widgets[STEEP_LAYER].property("slider")
            self.assertEqual(slider.value(), 40)
            slider.setValue(75)
            self.assertAlmostEqual(
                layers[STEEP_VARIANTS[1][0]].opacity(), 0.75, places=6
            )
            menu.actions()[2].trigger()
            self.assertTrue(panel._layer_visible(STEEP_VARIANTS[2][0]))
            self.assertFalse(panel._layer_visible(STEEP_VARIANTS[1][0]))
            self.assertAlmostEqual(
                layers[STEEP_VARIANTS[2][0]].opacity(), 0.75, places=6
            )
            # Снятие галочки прячет все варианты
            panel.steep_check.setChecked(False)
            for name, _label in STEEP_VARIANTS:
                self.assertFalse(panel._layer_visible(name))
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_slope_variant_fallback(self):
        """Старый шаблон: единственный слой «slope (steep)» без вариантов."""
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import STEEP_LAYER, ProjectPanel

        project = QgsProject.instance()
        project.clear()
        layer = QgsRasterLayer(
            "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
            "&zmax=19&zmin=0",
            STEEP_LAYER,
            "wms",
        )
        project.addMapLayer(layer)
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            self.assertEqual(len(panel.steep_variant_menu.actions()), 1)
            self.assertEqual(panel._active_steep_layer(), STEEP_LAYER)
            panel.steep_check.setChecked(True)
            self.assertTrue(panel._layer_visible(STEEP_LAYER))
        finally:
            panel.deleteLater()
            project.clear()

    def test_project_panel_slope_variant_by_exact_name(self):
        """Вариант уклона определяется полным именем слоя, а не суффиксом."""
        from qgis.core import QgsProject, QgsRasterLayer
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        # Похожие имена не должны попадать в список вариантов.
        names = (
            "[TERRAIN] slope (steep) var1",
            "slope (steep) var2",
            "[TERRAIN] slope (slight)",
        )
        for name in names:
            layer = QgsRasterLayer(
                "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0",
                name,
                "wms",
            )
            project.addMapLayer(layer)
        panel = ProjectPanel(None)
        try:
            panel._sync_from_project()
            actions = panel.steep_variant_menu.actions()
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].text(), "Горы")
            self.assertEqual(
                panel._active_steep_layer(), "[TERRAIN] slope (steep) var1"
            )
            panel.steep_check.setChecked(True)
            self.assertTrue(panel._layer_visible("[TERRAIN] slope (steep) var1"))
        finally:
            panel.deleteLater()
            project.clear()

    def test_ramp_items_scale(self):
        from qgis.core import QgsColorRampShader
        from qgis.PyQt.QtGui import QColor
        from qgis_xtopo.gui.projectpanel import _ramp_items

        items = [
            QgsColorRampShader.ColorRampItem(0, QColor(0, 0, 0, 115), "0"),
            QgsColorRampShader.ColorRampItem(255, QColor(255, 255, 255, 0), "255"),
        ]
        baseline = [item.color.alpha() for item in items]
        # 50% — исходный вид
        self.assertEqual(
            [item.color.alpha() for item in _ramp_items(items, baseline, 50)],
            [115, 0],
        )
        # меньше — темнее
        self.assertEqual(
            [item.color.alpha() for item in _ramp_items(items, baseline, 0)],
            [230, 0],
        )
        # больше — светлее, предельно светлый вариант без теней
        self.assertEqual(
            [item.color.alpha() for item in _ramp_items(items, baseline, 100)],
            [0, 0],
        )

    def test_hillshade_shadows_can_be_darker(self):
        """Шкала затенения рельефа меняет плотность теней в проекте.

        Сценарий идёт в отдельном процессе: провайдер растров QGIS при
        выгрузке слоя сбрасывает менеджер драйверов GDAL, и следующие
        тесты в этом же процессе не смогли бы открыть векторные слои.
        """
        import subprocess
        import sys
        import textwrap

        root = Path(__file__).resolve().parent.parent
        script = textwrap.dedent(f"""
            import os, sys, tempfile
            from pathlib import Path
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            sys.path.insert(0, {str(root)!r})
            import tests
            from qgis.core import QgsApplication
            app = QgsApplication([], False)
            app.initQgis()
            from qgis.core import (QgsColorRampShader, QgsProject,
                QgsRasterLayer, QgsRasterShader, QgsSingleBandPseudoColorRenderer)
            from qgis.PyQt.QtGui import QColor
            from qgis_xtopo.gui.projectpanel import HILLSHADE_LAYER, ProjectPanel
            project = QgsProject.instance()
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "hillshade.tif"
                from osgeo import gdal
                gdal.UseExceptions()
                dataset = gdal.GetDriverByName("GTiff").Create(
                    str(path), 4, 4, 1, gdal.GDT_Byte)
                dataset.SetGeoTransform((0, 1, 0, 10, 0, -1))
                dataset.GetRasterBand(1).WriteRaster(0, 0, 4, 4, bytes(16))
                dataset = None
                layer = QgsRasterLayer(str(path), HILLSHADE_LAYER)
                assert layer.isValid()
                shader = QgsRasterShader()
                function = QgsColorRampShader()
                function.setColorRampType(QgsColorRampShader.Interpolated)
                function.setColorRampItemList([
                    QgsColorRampShader.ColorRampItem(
                        0, QColor(0, 0, 0, 115), "0"),
                    QgsColorRampShader.ColorRampItem(
                        255, QColor(255, 255, 255, 0), "255"),
                ])
                shader.setRasterShaderFunction(function)
                layer.setRenderer(QgsSingleBandPseudoColorRenderer(
                    layer.dataProvider(), 1, shader))
                project.addMapLayer(layer)
                panel = ProjectPanel(None)
                panel._sync_from_project()
                widget = panel._opacity_widgets[HILLSHADE_LAYER]
                slider = widget.property("slider")
                assert slider.value() == 50, slider.value()
                # Меньше прозрачности — темнее тени
                slider.setValue(0)
                items = layer.renderer().shader().rasterShaderFunction(
                    ).colorRampItemList()
                assert items[0].color.alpha() == 230, items[0].color.alpha()
                assert items[1].color.alpha() == 0
                # Сброс возвращает исходный вид
                widget.property("reset_button").click()
                items = layer.renderer().shader().rasterShaderFunction(
                    ).colorRampItemList()
                assert items[0].color.alpha() == 115
                assert slider.value() == 50
            print("OK")
            sys.stdout.flush()
            os._exit(0)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

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

    def test_fit_project_extent_preserves_extent_at_150_percent(self):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
            QgsProject, QgsVectorLayer,
        )
        from qgis.gui import QgsMapCanvas
        from qgis_xtopo.core import project as project_module

        project = QgsProject.instance()
        project.clear()
        layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "BBOX", "memory")
        feature = QgsFeature(layer.fields())
        feature.setGeometry(
            QgsGeometry.fromWkt("POLYGON ((0 0, 0 10, 10 10, 10 0, 0 0))")
        )
        layer.dataProvider().addFeature(feature)
        project.addMapLayer(layer)

        class Iface:
            def __init__(self, canvas):
                self.canvas = canvas

            def mapCanvas(self):
                return self.canvas

        canvas = QgsMapCanvas()
        canvas.resize(800, 600)
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        iface = Iface(canvas)
        try:
            canvas.setMagnificationFactor(1.0)
            self.assertTrue(project_module.fit_project_extent(iface))
            extent_100 = canvas.extent()
            canvas.setMagnificationFactor(1.5)
            self.assertTrue(project_module.fit_project_extent(iface))
            extent_150 = canvas.extent()
            self.assertAlmostEqual(extent_150.width(), extent_100.width(), places=6)
            self.assertAlmostEqual(extent_150.height(), extent_100.height(), places=6)
            self.assertAlmostEqual(
                extent_150.center().x(), extent_100.center().x(), places=6
            )
            self.assertAlmostEqual(
                extent_150.center().y(), extent_100.center().y(), places=6
            )
        finally:
            project.clear()
            canvas.deleteLater()
            self.app.processEvents()

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

    def test_dock_geofabrik_downloaded_highlight(self):
        from qgis.PyQt.QtCore import Qt

        from qgis_xtopo.core import geofabrik
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        chosen = geofabrik.GeofabrikMap(
            "corse", "Corse", "france",
            "https://download.geofabrik.de/europe/france/corse-latest.osm.pbf",
        )
        state = geofabrik.IndexState(
            geofabrik.GeofabrikIndex(), 0, "Tue, 20 Oct 2026 04:04:13 GMT"
        )
        plan = geofabrik.GeofabrikPlan(
            state,
            [chosen],
            [geofabrik.MapStatus(chosen, True, 34506434, local=True)],
        )
        dock._geofabrik_plan = plan
        dock._apply_geofabrik_plan(plan)
        entry = dock.geofabrik_maps.item(0)
        self.assertIn("скачана", entry.text())
        self.assertNotEqual(entry.background().style(), Qt.NoBrush)
        self.assertGreater(entry.background().color().green(),
                           entry.background().color().red())
        dock.deleteLater()

    def test_dock_geofabrik_list_fits_width(self):
        """Длинные названия не дают горизонтальную прокрутку списка."""
        from qgis.PyQt.QtWidgets import QListView, QStyleOptionViewItem

        from qgis_xtopo.core import geofabrik
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        dock = XtopoDockWidget(None)
        self.assertEqual(
            dock.geofabrik_maps.resizeMode(), QListView.Adjust
        )
        chosen = geofabrik.GeofabrikMap(
            "russia", "Russian Federation", "europe",
            "https://download.geofabrik.de/russia-latest.osm.pbf",
        )
        state = geofabrik.IndexState(
            geofabrik.GeofabrikIndex(), 0, "Tue, 20 Oct 2026 04:04:13 GMT"
        )
        plan = geofabrik.GeofabrikPlan(
            state,
            [chosen],
            [geofabrik.MapStatus(chosen, True, 5103585179)],
        )
        dock._geofabrik_plan = plan
        dock._apply_geofabrik_plan(plan)
        dock.geofabrik_maps.resize(260, 150)
        dock.geofabrik_maps.show()
        self.app.processEvents()
        # Ширина элемента по мнению делегата не превышает ширину окна —
        # прокрутка не нужна, а текст обрезается многоточием посередине.
        delegate = dock.geofabrik_maps.itemDelegate()
        index = dock.geofabrik_maps.model().index(0, 0)
        hint = delegate.sizeHint(QStyleOptionViewItem(), index)
        self.assertLessEqual(
            hint.width(), dock.geofabrik_maps.viewport().width()
        )
        self.assertFalse(
            dock.geofabrik_maps.horizontalScrollBar().isVisible()
        )
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
        self.assertIn("охват", dialog.draw_button.toolTip())
        self.assertNotIn("bbox", dialog.draw_button.toolTip())
        # Запомненный охват рисуется при открытии окна
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

    def test_extent_dialog_escape_and_scale_bar(self):
        """Esc отменяет выбор, шкала масштаба рисуется на холсте."""
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtGui import QKeySequence
        from qgis.PyQt.QtWidgets import QShortcut
        from qgis_xtopo.gui.extentdialog import ExtentDialog

        dialog = ExtentDialog("37.5,55.7,37.7,55.8")
        sequences = [
            shortcut.key().toString()
            for shortcut in dialog.findChildren(QShortcut)
        ]
        self.assertIn(
            QKeySequence(Qt.Key_Escape).toString(), sequences,
            "Esc должен закрывать окно выбора охвата",
        )
        # Подписи размеров сторон идут за рамкой во время рисования.
        dialog._rectangle_changed(dialog.canvas.extent())
        self.assertTrue(dialog._frame_labels[0])
        self.assertTrue(dialog._frame_labels[1])
        # Подписи берутся из поставщика: они следуют за рамкой при любой
        # перерисовке, даже если карта перемещается.
        rectangle, width_label, height_label = dialog.canvas._current_frame()
        self.assertIsNotNone(rectangle)
        self.assertEqual(width_label, dialog._frame_labels[0])
        self.assertEqual(height_label, dialog._frame_labels[1])
        # Шкала масштаба: длина полоски и подпись вычисляются без ошибок.
        painter_check = dialog.canvas._draw_scale_bar
        self.assertTrue(callable(painter_check))
        dialog.done(0)

    def test_extent_dialog_basemaps(self):
        """Подложки переключаются, подпись под ними меняется."""
        from qgis_xtopo.gui.extentdialog import BASEMAPS, ExtentDialog

        dialog = ExtentDialog("")
        try:
            self.assertEqual(dialog.basemap_combo.count(), len(BASEMAPS))
            names = [
                dialog.basemap_combo.itemText(index)
                for index in range(dialog.basemap_combo.count())
            ]
            for expected in (
                "OpenStreetMap", "OpenTopoMap", "ArcGIS Topo",
                "Google Satellite", "Google Hybrid",
            ):
                self.assertIn(expected, names)
            self.assertTrue(dialog.layer.isValid())
            self.assertIn("openstreetmap.org", dialog.attribution.text())
            index = dialog.basemap_combo.findData("opentopo")
            dialog.basemap_combo.setCurrentIndex(index)
            self.assertIn("opentopomap.org", dialog.attribution.text())
            self.assertIn("opentopomap.org", dialog.layer.source())
            self.assertTrue(dialog.layer.isValid())
            index = dialog.basemap_combo.findData("google_satellite")
            dialog.basemap_combo.setCurrentIndex(index)
            self.assertIn("mt1.google.com", dialog.layer.source())
            self.assertTrue(dialog.layer.isValid())
        finally:
            dialog.done(0)

    def test_extent_dialog_search_moves_map(self):
        """Найденное место: карта переходит к нему, рамка сбрасывается."""
        import time
        from unittest import mock

        from qgis_xtopo.core import nominatim
        from qgis_xtopo.gui.extentdialog import ExtentDialog

        place = nominatim.Place(
            name="Кисловодск, Россия",
            lat=43.9055311,
            lon=42.7157283,
            south=43.8472332,
            north=43.9599627,
            west=42.6598370,
            east=42.7922094,
        )
        dialog = ExtentDialog("")
        try:
            with mock.patch.object(
                nominatim, "search", return_value=[place]
            ) as search:
                dialog.search_input.setText("Кисловодск")
                dialog._start_search()
                deadline = time.time() + 20
                while (
                    dialog._search_worker is not None
                    and time.time() < deadline
                ):
                    self.app.processEvents()
                    time.sleep(0.02)
                self.app.processEvents()
            search.assert_called()
            self.assertEqual(dialog.search_results.count(), 1)
            self.assertIn("1", dialog.search_status.text())
            dialog.search_results.setCurrentRow(0)
            self.app.processEvents()
            bbox = dialog._extent_as_bbox()
            middle_lon = (bbox.lon_min + bbox.lon_max) / 2
            middle_lat = (bbox.lat_min + bbox.lat_max) / 2
            self.assertAlmostEqual(middle_lon, 42.7157283, delta=0.2)
            self.assertAlmostEqual(middle_lat, 43.9055311, delta=0.2)
            # Нарисованная рамка больше не задаёт охват.
            self.assertIsNone(dialog._drawn_extent)
            self.assertFalse(dialog.reset_button.isEnabled())
        finally:
            dialog.done(0)

    def test_extent_dialog_enter_in_search_does_not_close(self):
        """Enter в строке поиска запускает поиск, а не закрывает окно."""
        from unittest import mock

        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtTest import QTest
        from qgis_xtopo.core import nominatim
        from qgis_xtopo.gui.extentdialog import ExtentDialog

        dialog = ExtentDialog("")
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        try:
            with mock.patch.object(nominatim, "search", return_value=[]):
                dialog.show()
                self.app.processEvents()
                dialog.search_input.setFocus()
                self.app.processEvents()
                dialog.search_input.setText("Кисловодск")
                QTest.keyClick(dialog.search_input, Qt.Key_Return)
                self.app.processEvents()
            self.assertEqual(accepted, [])
        finally:
            dialog.done(0)
            self.app.processEvents()

    def test_geofabrik_refresh_button(self):
        """Кнопка обновления индекса помечает кэш индекса устаревшим."""
        from unittest import mock

        from qgis_xtopo.core import geofabrik
        from qgis_xtopo.core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            config = Config(qgis_projects_dir=directory, project_name="refresh")
            config.ensure_directories()
            geofabrik_dir = config.geofabrik_dir
            geofabrik_dir.mkdir(parents=True, exist_ok=True)
            (geofabrik_dir / "index-meta.json").write_text(
                '{"fetched_at": 9999999999, "etag": "x"}', encoding="utf-8"
            )
            with mock.patch.object(
                geofabrik, "invalidate_index"
            ) as invalidate:
                invalidate(config)
            invalidate.assert_called_once()

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
            # Прежние слои проекта должны быть удалены до подготовки.
            config.ensure_directories()
            stale_vector = config.vector_dir / "stale.sqlite"
            stale_raster = config.raster_dir / "stale.tif"
            stale_vector.write_text("old", encoding="utf-8")
            stale_raster.write_text("old", encoding="utf-8")

            project_path = run_project(config)
            self.assertTrue(project_path.exists())
            self.assertFalse(stale_vector.exists())
            self.assertFalse(stale_raster.exists())
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

    def test_poi_icons_service_layer_is_built(self):
        """Служебный слой значков собирается при подготовке проекта."""
        from qgis_xtopo.core import poi_icons
        from qgis_xtopo.core.config import Config
        from qgis_xtopo.core.pipeline import run_project

        osm = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6">
 <node id="1" lat="42.90" lon="44.98"><tag k="place" v="city"/></node>
 <node id="2" lat="42.86" lon="44.96"><tag k="natural" v="peak"/></node>
 <node id="3" lat="42.86" lon="44.96"/>
 <node id="4" lat="42.86" lon="44.98"/>
</osm>
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.osm"
            source.write_text(osm, encoding="utf-8")
            config = Config(
                qgis_projects_dir=str(root / "projects"),
                project_name="poi",
                bbox="44.95,42.85,45.05,42.95",
                array_queries=["peak", "places_main"],
                source_mode="pbf",
                pbf_path=str(source),
            )
            project_path = run_project(config)
            aux = config.vector_dir / poi_icons.AUX_FILE_NAME
            self.assertTrue(aux.exists(), "нет служебного слоя значков")

            from osgeo import ogr

            dataset = ogr.Open(str(aux))
            try:
                self.assertGreater(dataset.GetLayerCount(), 0)
                total = 0
                names = set()
                for index in range(dataset.GetLayerCount()):
                    layer = dataset.GetLayer(index)
                    total += layer.GetFeatureCount()
                    definition = layer.GetLayerDefn()
                    names.update(
                        definition.GetFieldDefn(position).GetName()
                        for position in range(definition.GetFieldCount())
                    )
            finally:
                dataset = None
            self.assertGreater(total, 0)
            for field in ("rank", "lod"):
                self.assertIn(field, names)

            with zipfile.ZipFile(project_path) as archive:
                name = next(
                    item for item in archive.namelist() if item.endswith(".qgs")
                )
                text = archive.read(name).decode("utf-8")
            # Служебные слои есть в проекте, но не показываются в дереве.
            self.assertIn("<layername>poi icons ", text)
            tree = re.findall(r'<layer-tree-layer[^>]*name="([^"]+)"', text)
            self.assertFalse([item for item in tree if item.startswith("poi icons")])
            # Правила значков ссылаются на служебные слои.
            self.assertIn("overlay_nearest('poi icons ", html.unescape(text))

            # Слой доступен по имени: на нём строится выборка соседей.
            from qgis.core import (
                QgsExpression, QgsExpressionContext,
                QgsExpressionContextUtils, QgsProject,
            )

            project = QgsProject.instance()
            project.clear()
            project.read(str(project_path))
            service = [
                layer for layer in project.mapLayers().values()
                if layer.name().startswith('poi icons ')
            ]
            self.assertTrue(service, "нет служебных слоёв в проекте")
            self.assertTrue(all(layer.isValid() for layer in service))
            name = service[0].name()
            expression = QgsExpression(
                f"overlay_nearest('{name}', expression:=\"rank\", limit:=-1, "
                "max_distance:=0.1)"
            )
            context = QgsExpressionContext()
            context.appendScope(QgsExpressionContextUtils.projectScope(project))
            context.appendScope(
                QgsExpressionContextUtils.layerScope(service[0]))
            feature = next(service[0].getFeatures())
            context.setFeature(feature)
            value = expression.evaluate(context)
            self.assertFalse(expression.hasEvalError(), expression.evalErrorString())
            self.assertIsInstance(value, list)

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
