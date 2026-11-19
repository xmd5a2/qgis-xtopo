import importlib.util
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_QGIS = importlib.util.find_spec("qgis") is not None


@unittest.skipUnless(HAS_QGIS, "QGIS Python bindings are not available")
class PanelVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()

    def setUp(self):
        from qgis.PyQt.QtCore import QSettings

        self.settings = QSettings("qgis-xtopo", "qgis-xtopo")
        self._saved = {
            key: self.settings.value(key)
            for key in ("hide_panels", "hidden_panels")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                self.settings.remove(key)
            else:
                self.settings.setValue(key, value)

    def _window(self):
        from qgis.PyQt.QtWidgets import QDockWidget, QMainWindow

        window = QMainWindow()
        self.addCleanup(window.deleteLater)
        self.docks = {}
        for name in ("Layers", "Browser", "QgisXtopoDockWidget"):
            dock = QDockWidget(name, window)
            dock.setObjectName(name)
            window.addDockWidget(1, dock)
            dock.show()
            self.docks[name] = dock
        window.show()
        self.app.processEvents()
        return window

    def test_pipeline_worker_does_not_shadow_qthread_finished(self):
        """Удалять PipelineWorker можно только после возврата из run()."""
        from qgis.PyQt.QtCore import QThread

        from qgis_xtopo.gui.dockwidget import PipelineWorker

        self.assertIs(PipelineWorker.finished, QThread.finished)
        self.assertTrue(hasattr(PipelineWorker, "completed"))

    def test_hide_and_show_docks(self):
        from qgis.PyQt.QtWidgets import QToolBar, QWidget

        from qgis_xtopo.core.ui import (
            MESSAGE_BAR_NAME, PLUGIN_DOCK_NAME, hide_docks, show_docks,
            visible_dock_names,
        )

        window = self._window()
        toolbar = QToolBar("Файл", window)
        toolbar.setObjectName("mFileToolBar")
        window.addToolBar(toolbar)
        toolbar.show()
        bar = QWidget(window)
        bar.show()
        self.app.processEvents()

        class Iface:
            def mainWindow(self):
                return window

            def messageBar(self):
                return bar

        iface = Iface()
        self.assertIn("Layers", visible_dock_names(iface))
        hidden = hide_docks(iface)
        self.assertEqual(
            sorted(hidden), [MESSAGE_BAR_NAME, "Browser", "Layers", "mFileToolBar"]
        )
        self.assertTrue(self.docks["Layers"].isHidden())
        self.assertTrue(self.docks["Browser"].isHidden())
        # Панель плагина остаётся на экране.
        self.assertFalse(self.docks[PLUGIN_DOCK_NAME].isHidden())
        # Панель инструментов и строка уведомлений тоже скрыты.
        self.assertTrue(toolbar.isHidden())
        self.assertTrue(bar.isHidden())

        restored = show_docks(iface, hidden)
        self.assertEqual(
            sorted(restored), [MESSAGE_BAR_NAME, "Browser", "Layers", "mFileToolBar"]
        )
        self.assertFalse(self.docks["Layers"].isHidden())
        self.assertFalse(self.docks["Browser"].isHidden())
        self.assertFalse(toolbar.isHidden())
        self.assertFalse(bar.isHidden())

    def test_hide_docks_without_iface(self):
        from qgis_xtopo.core.ui import hide_docks, show_docks

        self.assertEqual(hide_docks(None), [])
        self.assertEqual(show_docks(None, ["Layers"]), [])

    def test_dock_hides_panels_when_project_opens(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = self._window()

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        try:
            # Кнопка — переключатель, она всегда доступна.
            self.assertTrue(dock.show_panels_button.isEnabled())

            # Проект открыт: стандартные панели скрываются.
            dock._own_marker = True
            dock._refresh_project_state()
            self.assertTrue(self.docks["Layers"].isHidden())
            self.assertTrue(self.docks["Browser"].isHidden())
            self.assertFalse(self.docks["QgisXtopoDockWidget"].isHidden())

            # Проект закрыт: панели возвращаются.
            dock._project_closed()
            self.assertFalse(self.docks["Layers"].isHidden())
            self.assertFalse(self.docks["Browser"].isHidden())
        finally:
            dock.restore_panels()
            dock.deleteLater()

    def test_show_panels_button_toggles(self):
        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = self._window()

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        try:
            dock._own_marker = True
            dock._refresh_project_state()
            self.assertTrue(self.docks["Layers"].isHidden())

            # Кнопка возвращает панели, но не отключает автоматическое
            # скрытие при открытии проекта.
            dock._show_panels()
            self.assertFalse(self.docks["Layers"].isHidden())

            # Повторное нажатие скрывает панели снова.
            dock._show_panels()
            self.assertTrue(self.docks["Layers"].isHidden())

            # При следующем обновлении состояния проекта панели
            # скрываются снова.
            dock._show_panels()
            dock._refresh_project_state()
            self.assertTrue(self.docks["Layers"].isHidden())
        finally:
            dock.restore_panels()
            dock.deleteLater()

    def test_plugin_starts_catalogue_refresh(self):
        """Список тайлов Sonny обновляется при старте плагина."""
        from unittest import mock

        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.plugin import QgisXtopoPlugin

        window = QMainWindow()
        self.addCleanup(window.deleteLater)
        iface = mock.Mock()
        iface.mainWindow.return_value = window
        plugin = QgisXtopoPlugin(iface)
        with mock.patch(
            "qgis_xtopo.gui.dockwidget.XtopoDockWidget."
            "refresh_sonny_catalogue"
        ) as refresh:
            plugin.initGui()
        try:
            refresh.assert_called_once_with()
            self.assertIsNotNone(plugin.dock)
        finally:
            plugin.unload()


class ProxyIconTest(unittest.TestCase):
    """Значок прокси: глиф с точкой состояния, а не залитый силуэт."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_state_dot_only_when_enabled(self):
        from qgis.PyQt.QtCore import QSize
        from qgis.PyQt.QtGui import QColor

        from qgis_xtopo.gui.dockwidget import _proxy_state_icon

        def image(state=None):
            icon = _proxy_state_icon(
                QColor("#222222"), QColor("#ffffff"), state,
            )
            return icon.pixmap(QSize(16, 16)).toImage()

        off = image()
        ok = image("ok")
        bad = image("bad")
        # Состояние видно по точке; без прокси точка не рисуется.
        self.assertNotEqual(off, ok)
        self.assertNotEqual(ok, bad)
        # Перекрашивалась вся иконка целиком — получалось сплошное пятно.
        # Контур глобуса не доходит до углов, они обязаны быть прозрачны.
        for pixmap in (off, ok, bad):
            self.assertEqual(pixmap.pixelColor(0, 0).alpha(), 0)
            self.assertEqual(pixmap.pixelColor(15, 0).alpha(), 0)
            self.assertEqual(pixmap.pixelColor(0, 15).alpha(), 0)


class ProxyCheckOnShowTest(unittest.TestCase):
    """Значок прокси проверяется при показе панели."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _dock(self):
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        return dock

    def test_show_checks_proxy_and_marks_it_working(self):
        import time
        from unittest import mock

        from qgis_xtopo.core import proxy
        from qgis_xtopo.gui import dockwidget

        dock = self._dock()
        config = proxy.ProxyConfig(
            enabled=True, ptype="socks5", host="127.0.0.1", port=1080,
        )
        with mock.patch.object(
            dockwidget.proxy, "load", return_value=config
        ), mock.patch.object(
            dockwidget.proxy, "probe", return_value=True
        ):
            dock.show()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not dock._proxy_known:
                self.app.processEvents()
                time.sleep(0.02)
            self.assertTrue(dock._proxy_known)
            self.assertTrue(dock._proxy_ok)
            self.assertEqual(dock._proxy_icon_key, "ok")
            dock.hide()

    def test_panel_has_single_show_event(self):
        """Два showEvent перекрывали друг друга: проверка не запускалась."""
        import inspect

        from qgis_xtopo.gui import dockwidget

        source = inspect.getsource(dockwidget.XtopoDockWidget)
        self.assertEqual(source.count("def showEvent"), 1)


class SourceModeTest(unittest.TestCase):
    """Порядок источников и приглушение редких режимов."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def setUp(self):
        from qgis_xtopo.core import i18n

        self._language = i18n.language()
        i18n.set_language("ru", save=False)

    def tearDown(self):
        from qgis_xtopo.core import i18n

        i18n.set_language(self._language, save=False)

    def _dock(self):
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        return dock

    def test_slice_comes_first_and_geofabrik_second(self):
        dock = self._dock()
        self.assertEqual(dock.source_mode.itemData(0), "slice")
        self.assertEqual(dock.source_mode.itemData(1), "geofabrik")

    def test_source_mode_is_saved_on_selection(self):
        """Режим сохраняется сразу: QGIS может не вызвать closeEvent."""
        from qgis.PyQt.QtCore import QSettings

        settings = QSettings("qgis-xtopo", "qgis-xtopo")
        saved = settings.value("source_mode")
        dock = self._dock()
        try:
            dock.source_mode.setCurrentIndex(
                dock.source_mode.findData("pbf")
            )
            dock.source_mode.setCurrentIndex(
                dock.source_mode.findData("slice")
            )
            settings.sync()
            self.assertEqual(settings.value("source_mode"), "slice")
        finally:
            if saved is None:
                settings.remove("source_mode")
            else:
                settings.setValue("source_mode", saved)
            settings.sync()

    def test_rare_modes_are_greyed_out(self):
        from qgis.PyQt.QtCore import Qt

        from qgis_xtopo.gui.dockwidget import RARE_SOURCE_MODES

        dock = self._dock()
        for index in range(dock.source_mode.count()):
            key = dock.source_mode.itemData(index)
            color = dock.source_mode.itemData(
                index, Qt.ItemDataRole.ForegroundRole
            )
            if key in RARE_SOURCE_MODES:
                self.assertIsNotNone(color, key)
            else:
                self.assertIsNone(color, key)

    @staticmethod
    def _slice_estimate(**overrides):
        from qgis_xtopo.core import slice_estimate

        values = {
            "nodes": 2_973_537,
            "size_bytes": 23_883_693,
            "build_seconds": 38.0,
            "queue_seconds": 0.0,
            "download_seconds": 24.0,
            "prepare_seconds": 120.0,
            "total_seconds": 182.0,
            "nodes_limit": 100_000_000,
        }
        values.update(overrides)
        return slice_estimate.SliceEstimate(**values)

    def test_slice_refresh_button_refreshes_index(self):
        """Кнопка перекачивает карту узлов и пересчитывает оценку."""
        from unittest import mock

        from qgis_xtopo.core import net

        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        with mock.patch.object(net, "reset") as reset, \
                mock.patch.object(dock, "_start_slice_estimate") as start:
            dock._refresh_slice_estimate()
        reset.assert_called_once_with()
        start.assert_called_once_with(refresh=True)

    def test_slice_refresh_button_is_only_in_slice_mode(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        self.assertTrue(dock.slice_header.isVisibleTo(dock))
        self.assertEqual(
            dock.slice_refresh.toolTip(),
            "Обновить индекс выгрузок SliceOSM",
        )
        dock.source_mode.setCurrentIndex(
            dock.source_mode.findData("geofabrik")
        )
        self.assertFalse(dock.slice_header.isVisibleTo(dock))

    def test_slice_refreshing_state_shows_progress(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = ("refreshing",)
        dock._render_slice_estimate()
        self.assertIn("Обновление индекса", dock.pbf_info.text())

    def test_slice_mode_shows_volume_and_time(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = ("slice", self._slice_estimate())
        dock._render_slice_estimate()
        self.assertFalse(dock.pbf_info.isHidden())
        text = dock.pbf_info.text()
        self.assertIn("Объём", text)
        self.assertIn("сборка", text)
        self.assertIn("скачивание", text)
        self.assertIn("подготовка", text)

    def test_slice_cached_cutout_uses_exact_size(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = (
            "slice",
            self._slice_estimate(cached_bytes=23_883_693),
        )
        dock._render_slice_estimate()
        text = dock.pbf_info.text()
        self.assertIn("уже скачан", text)
        self.assertNotIn("сборка", text)
        self.assertNotIn("скачивание", text)

    def test_slice_limit_warning_is_visible(self):
        from qgis.PyQt.QtCore import Qt

        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = (
            "slice",
            self._slice_estimate(nodes=120_000_000, nodes_limit=100_000_000),
        )
        dock._render_slice_estimate()
        text = dock.pbf_info.text()
        self.assertIn("Предел SliceOSM", text)
        # Предупреждение о превышении предела — красное и жирное.
        self.assertIn("color:#e74c3c", text)
        self.assertIn("font-weight:bold", text)
        # Разметка должна разбираться, а не показываться тегами.
        self.assertEqual(
            dock.pbf_info.textFormat(), Qt.TextFormat.RichText
        )

    def test_slice_estimate_failure_does_not_hide_mode(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = ("error", "нет сети")
        dock._render_slice_estimate()
        self.assertFalse(dock.pbf_info.isHidden())
        self.assertIn("недоступна", dock.pbf_info.text())

    def test_slice_estimate_error_text_is_escaped(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = ("error", "нет <сети> и &")
        dock._render_slice_estimate()
        text = dock.pbf_info.text()
        self.assertIn("&lt;сети&gt;", text)
        self.assertIn("&amp;", text)

    def test_rare_modes_start_with_public_overpass(self):
        from qgis_xtopo.gui.dockwidget import RARE_SOURCE_MODES

        dock = self._dock()
        keys = [
            dock.source_mode.itemData(index)
            for index in range(dock.source_mode.count())
        ]
        first_rare = keys.index(RARE_SOURCE_MODES[0])
        self.assertEqual(keys[first_rare], "overpass")
        self.assertEqual(keys[first_rare:], list(RARE_SOURCE_MODES))


class TabCornerButtonsTest(unittest.TestCase):
    """Кнопки прокси и языка не должны быть выше полосы вкладок."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_corner_buttons_fit_tab_bar(self):
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        dock._fit_language_button()

        limit = dock.tabs.tabBar().sizeHint().height()
        self.assertLessEqual(dock.proxy_button.height(), limit)
        self.assertLessEqual(dock.language_button.height(), limit)
        corner = dock.tabs.cornerWidget()
        self.assertIsNotNone(corner)
        margins = corner.layout().contentsMargins()
        self.assertGreater(margins.right(), 0)


class LogScrollTest(unittest.TestCase):
    """Новая строка журнала должна быть видна, масштаб текста — сохранён."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_new_line_scrolls_into_view_and_keeps_zoom(self):
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        dock._open_log_window()
        view = dock.log_window_text
        self.assertIsNotNone(view)
        for index in range(200):
            view.appendPlainText(f"строка {index} " + "x" * 60)
        self.app.processEvents()
        view.zoomIn(2)
        zoom = view.font().pointSizeF()
        view.verticalScrollBar().setValue(0)

        dock._append_log("новая строка")
        self.app.processEvents()

        bar = view.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        # Последняя строка у нижнего края, а не курсор где-то выше.
        self.assertEqual(bar.value(), bar.maximum())
        self.assertEqual(view.font().pointSizeF(), zoom)


class AtlasSheetLabelsTest(unittest.TestCase):
    """Подписи размеров листа атласа должны быть рядом с листом."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _labels(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle
        from qgis.gui import QgsMapCanvas

        from qgis_xtopo.gui.atlas import SheetSizeLabels

        canvas = QgsMapCanvas()
        canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        canvas.resize(640, 480)
        canvas.setExtent(QgsRectangle(-10000, -10000, 10000, 10000))
        labels = SheetSizeLabels(canvas)
        sheet = QgsRectangle(0, 0, 5000, 5000)
        labels.set_rectangle(
            sheet, QgsCoordinateReferenceSystem("EPSG:3857")
        )
        return canvas, labels, sheet

    def test_sheet_rect_is_drawn_where_the_sheet_is(self):
        """Размеры листа считаются в координатах холста, а не элемента."""
        from qgis.core import QgsPointXY

        canvas, labels, sheet = self._labels()
        transform = canvas.mapSettings().mapToPixel()
        center = sheet.center()
        left = transform.transform(
            QgsPointXY(sheet.xMinimum(), center.y())
        )
        right = transform.transform(
            QgsPointXY(sheet.xMaximum(), center.y())
        )
        top = transform.transform(
            QgsPointXY(center.x(), sheet.yMaximum())
        )
        bottom = transform.transform(
            QgsPointXY(center.x(), sheet.yMinimum())
        )
        # Прямоугольник подписей в координатах холста совпадает с листом.
        scene = labels._size.translated(labels.pos().x(), labels.pos().y())
        self.assertAlmostEqual(scene.left(), left.x(), delta=1.0)
        self.assertAlmostEqual(scene.top(), top.y(), delta=1.0)
        self.assertAlmostEqual(scene.width(), right.x() - left.x(), delta=1.0)
        self.assertAlmostEqual(scene.height(), bottom.y() - top.y(), delta=1.0)

    def test_width_label_stays_below_the_sheet(self):
        """Нижняя подпись не уезжает за пределы холста."""
        canvas, labels, _sheet = self._labels()
        gap = 3.0
        label_bottom = labels._size.bottom() + gap
        scene_bottom = label_bottom + labels.pos().y()
        # Подпись ширины идёт под нижней стороной листа и помещается
        # в холст: раньше она уезжала на вектор центра листа.
        self.assertGreater(scene_bottom, labels.pos().y())
        self.assertLess(scene_bottom, canvas.height())
        self.assertLess(labels.sceneBoundingRect().bottom(), canvas.height())


class PanelProjectStateTest(unittest.TestCase):
    """Панель проекта не должна оставлять проект изменённым."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_refresh_does_not_mark_project_dirty(self):
        """Старый проект без переменных не должен казаться изменённым."""
        from unittest import mock

        from qgis.core import QgsExpressionContextUtils, QgsProject

        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        QgsExpressionContextUtils.removeProjectVariable(
            project, "level_of_detail"
        )
        QgsExpressionContextUtils.removeProjectVariable(
            project, "text_size_mult"
        )
        project.setDirty(False)
        try:
            panel = projectpanel.ProjectPanel(None)
            try:
                with mock.patch.object(
                    projectpanel.ProjectPanel,
                    "is_xtopo_project",
                    staticmethod(lambda *args, **kwargs: True),
                ):
                    panel.refresh()
                self.assertFalse(project.isDirty())
            finally:
                panel.deleteLater()
        finally:
            QgsExpressionContextUtils.removeProjectVariable(
                project, "level_of_detail"
            )
            QgsExpressionContextUtils.removeProjectVariable(
                project, "text_size_mult"
            )
            project.setDirty(False)

    def test_detail_change_marks_project_dirty(self):
        """Смена уровня детализации — явное действие, проект меняется."""
        from unittest import mock

        from qgis.core import QgsProject

        from qgis_xtopo.gui import projectpanel

        project = QgsProject.instance()
        project.setDirty(False)
        panel = projectpanel.ProjectPanel(None)
        try:
            combo = panel.detail_combo
            with mock.patch.object(
                projectpanel.ProjectPanel,
                "is_xtopo_project",
                staticmethod(lambda *args, **kwargs: True),
            ):
                combo.setCurrentIndex(combo.findData(5))
                self.app.processEvents()
            self.assertTrue(project.isDirty())
        finally:
            panel.deleteLater()
            project.setDirty(False)


class DockWorkerStateTest(unittest.TestCase):
    """Устаревшие результаты фоновых потоков не должны затирать свежие."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _dock(self):
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.gui.dockwidget import XtopoDockWidget

        window = QMainWindow()
        self.addCleanup(window.deleteLater)

        class Iface:
            def mainWindow(self):
                return window

        dock = XtopoDockWidget(Iface())
        self.addCleanup(dock.deleteLater)
        return dock

    def test_stale_pbf_probe_result_is_ignored(self):
        """Результат старой пробы не затирает оценку текущей."""
        from unittest import mock

        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("pbf"))
        stale = mock.Mock()
        stale.generation = 1
        current = mock.Mock()
        current.generation = 2
        dock.pbf_probe = current
        dock._pbf_probe_generation = 2
        self.assertFalse(dock._is_current_pbf_probe(stale))
        self.assertTrue(dock._is_current_pbf_probe(current))

    def test_cancelled_pbf_probe_is_not_current(self):
        """Отменённая проба больше не считается текущей."""
        from unittest import mock

        from qgis_xtopo.gui import dockwidget

        dock = self._dock()
        worker = mock.Mock()
        worker.generation = 1
        dock.pbf_probe = worker
        dock._pbf_probe_generation = 1
        self.assertTrue(dock._is_current_pbf_probe(worker))
        dock._cancel_pbf_probe()
        self.assertIsNone(dock.pbf_probe)

    def test_server_check_is_repeated_after_cancel(self):
        """Отменённая проверка серверов не блокирует повторную."""
        dock = self._dock()
        dock._servers_checked = True
        dock.server_workers = [object()]
        dock._reset_server_status()
        self.assertFalse(dock._servers_checked)
        self.assertTrue(dock.recheck_servers.isEnabled())

    def test_disk_info_is_debounced(self):
        """Свободное место пересчитывается по таймеру, а не на символ."""
        from unittest import mock

        from qgis_xtopo.core import disk_space
        from qgis_xtopo.gui import dockwidget

        dock = self._dock()
        dock._disk_info_timer.stop()
        with mock.patch.object(
            disk_space, "free_bytes", return_value=3 * disk_space.GIB
        ) as free:
            dock.projects_dir.setText("/tmp")
            # Проверки на каждый символ не было: таймер ждёт паузы.
            free.assert_not_called()
            dock._disk_info_timer.setInterval(0)
            dock._update_disk_info()
            free.assert_called()


class ProxyRestoreTest(unittest.TestCase):
    """Прокси приложения возвращается при выгрузке плагина."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_unload_restores_application_proxy(self):
        from unittest import mock

        from qgis.PyQt.QtNetwork import QNetworkProxy
        from qgis.PyQt.QtWidgets import QMainWindow

        from qgis_xtopo.plugin import QgisXtopoPlugin

        window = QMainWindow()
        self.addCleanup(window.deleteLater)
        iface = mock.Mock()
        iface.mainWindow.return_value = window
        before = QNetworkProxy.applicationProxy()
        plugin = QgisXtopoPlugin(iface)
        plugin.initGui()
        try:
            self.assertIsNotNone(plugin.dock._application_proxy)
            plugin.unload()
            after = QNetworkProxy.applicationProxy()
            self.assertEqual(after.type(), before.type())
            self.assertEqual(after.hostName(), before.hostName())
            self.assertEqual(after.port(), before.port())
        finally:
            if plugin.dock is not None:
                plugin.unload()


class ProjectPanelLanguageTest(unittest.TestCase):
    """Панель проекта переводит меню и действия."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_measure_menu_follows_interface_language(self):
        from qgis_xtopo.core import i18n
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        saved = i18n.language()
        panel = ProjectPanel(None)
        try:
            i18n.set_language("en", save=False)
            panel.retranslate()
            menu = panel.measure_action.menu()
            self.assertEqual(
                [action.text() for action in menu.actions()],
                ["Distance", "Area", "Angle", "Bearing"],
            )
            i18n.set_language("ru", save=False)
            panel.retranslate()
            self.assertEqual(
                [action.text() for action in menu.actions()],
                ["Расстояние", "Площадь", "Угол", "Азимут"],
            )
        finally:
            i18n.set_language(saved, save=False)
            panel.deleteLater()


class IsolinesRuleTest(unittest.TestCase):
    """Уровни изолиний ищутся фильтром провайдера, а не обходом слоя."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _layer(self, filters, elevations):
        from qgis.core import (
            QgsFeature, QgsGeometry, QgsRuleBasedRenderer, QgsVectorLayer,
        )

        layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326&field=ELEV:integer",
            "[TERRAIN] isolines regular",
            "memory",
        )
        root = QgsRuleBasedRenderer.Rule(None)
        for value in sorted(filters, reverse=True):
            rule = QgsRuleBasedRenderer.Rule(None)
            rule.setLabel(str(value))
            rule.setFilterExpression(filters[value])
            root.appendChild(rule)
        layer.setRenderer(QgsRuleBasedRenderer(root))
        for elevation in elevations:
            feature = QgsFeature(layer.fields())
            feature.setAttribute("ELEV", elevation)
            feature.setGeometry(QgsGeometry.fromWkt("LINESTRING (0 0, 1 1)"))
            layer.dataProvider().addFeature(feature)
        return layer

    def test_available_levels_are_detected(self):
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        filters = {
            1000: '"ELEV" % 1000 = 0 AND "ELEV" != 0',
            100: '"ELEV" % 1000 != 0 AND "ELEV" % 100 = 0',
            50: '"ELEV" % 100 != 0 AND "ELEV" % 50 = 0',
        }
        layer = self._layer(filters, (100, 200, 1000))
        panel = ProjectPanel(None)
        try:
            rules = [(value, str(value), True) for value in sorted(filters)]
            available = panel._available_rule_labels(layer, rules)
        finally:
            panel.deleteLater()
        self.assertTrue(available["1000"])
        self.assertTrue(available["100"])
        # Уровня 50 в данных нет: галка недоступна.
        self.assertFalse(available["50"])

    def test_level_search_goes_through_the_provider(self):
        """Фильтр отдаётся провайдеру: слой не читается целиком в Python."""
        from qgis_xtopo.gui.projectpanel import ProjectPanel

        filters = {1000: '"ELEV" % 1000 = 0 AND "ELEV" != 0'}
        layer = self._layer(filters, (100, 200, 1000))
        requests = []
        original = layer.getFeatures

        def getFeatures(request=None):
            requests.append(request)
            return original(request)

        layer.getFeatures = getFeatures
        panel = ProjectPanel(None)
        try:
            rules = [(1000, "1000", True)]
            available = panel._available_rule_labels(layer, rules)
        finally:
            panel.deleteLater()
        self.assertTrue(available["1000"])
        self.assertTrue(requests)
        for request in requests:
            self.assertTrue(request.filterExpression())
            # Одного объекта достаточно: уровень есть в данных или нет.
            self.assertEqual(request.limit(), 1)


class VariantMenuTest(unittest.TestCase):
    """Меню вариантов окраски пересобирается только при смене слоёв."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def test_menu_is_kept_when_layers_do_not_change(self):
        from qgis.core import QgsProject, QgsVectorLayer

        from qgis_xtopo.gui.projectpanel import ProjectPanel

        project = QgsProject.instance()
        project.clear()
        project.addMapLayer(QgsVectorLayer(
            "Point?crs=EPSG:4326", "[TERRAIN] slope (steep) var1", "memory",
        ))
        panel = ProjectPanel(None)
        try:
            panel._update_steep_variants()
            actions = [id(action)
                       for action in panel.steep_variant_menu.actions()]
            self.assertTrue(actions)
            # Переключение видимости слоя не должно пересобирать меню.
            panel._sync_from_project()
            self.assertEqual(
                [id(action)
                 for action in panel.steep_variant_menu.actions()],
                actions,
            )
            # А новый слой — уже другое дело.
            project.addMapLayer(QgsVectorLayer(
                "Point?crs=EPSG:4326", "[TERRAIN] slope (steep) var2", "memory",
            ))
            panel._sync_from_project()
            self.assertEqual(len(panel.steep_variant_menu.actions()), 2)
        finally:
            panel.deleteLater()
            project.clear()


class ExportPreviewDialogTest(unittest.TestCase):
    """Просмотрщик листов: листание, миниатюры и сигналы экспорта."""

    @classmethod
    def setUpClass(cls):
        from tests import qgis_app

        cls.app = qgis_app()

    def _dialog(self, count=3):
        from qgis.PyQt.QtGui import QColor, QImage

        from qgis_xtopo.gui.exportpreview import ExportPreviewDialog, _enum

        images = []
        for number in range(count):
            image = QImage(
                100, 140, _enum(QImage, "Format", "Format_ARGB32")
            )
            image.fill(QColor(30 + number * 40, 90, 140))
            images.append(image)
        dialog = ExportPreviewDialog(images)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_navigation_and_indicator(self):
        dialog = self._dialog()
        self.assertEqual(dialog.sheet_count, 3)
        self.assertEqual(dialog.current_index, 0)
        self.assertEqual(dialog.sheet_label.text(), "1 / 3")
        self.assertFalse(dialog.previous_button.isEnabled())
        self.assertTrue(dialog.next_button.isEnabled())

        dialog._next()
        self.assertEqual(dialog.current_index, 1)
        self.assertEqual(dialog.sheet_label.text(), "2 / 3")
        self.assertTrue(dialog.previous_button.isEnabled())
        self.assertTrue(dialog.next_button.isEnabled())

        dialog._next()
        self.assertEqual(dialog.current_index, 2)
        self.assertEqual(dialog.sheet_label.text(), "3 / 3")
        self.assertFalse(dialog.next_button.isEnabled())

        dialog._previous()
        self.assertEqual(dialog.current_index, 1)

    def test_thumbnails_panel_is_vertical_and_hidden_for_one_sheet(self):
        dialog = self._dialog()
        self.assertEqual(dialog.thumbnails.count(), 3)
        self.assertEqual(dialog.thumbnails.iconSize().width(), 200)
        self.assertTrue(dialog.thumbnails_button.isChecked())
        self.assertTrue(dialog.thumbnail_panel.isVisibleTo(dialog))

        dialog._thumbnail_clicked(dialog.thumbnails.item(2))
        self.assertEqual(dialog.current_index, 2)
        self.assertTrue(dialog.thumbnail_panel.isVisibleTo(dialog))

        dialog.thumbnails_button.setChecked(False)
        self.assertFalse(dialog.thumbnail_panel.isVisibleTo(dialog))

        single = self._dialog(count=1)
        self.assertFalse(single.thumbnails_button.isVisibleTo(single))
        self.assertFalse(single.thumbnail_panel.isVisibleTo(single))
        self.assertFalse(single.navigation.isVisibleTo(single))

    def test_export_buttons_emit_extension(self):
        dialog = self._dialog()
        requested = []
        dialog.exportRequested.connect(requested.append)
        dialog.png_button.click()
        dialog.pdf_button.click()
        self.assertEqual(requested, ["png", "pdf"])

    def test_keyboard_navigation(self):
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtTest import QTest

        from qgis_xtopo.gui.exportpreview import _enum

        dialog = self._dialog()
        dialog.show()
        dialog.canvas.setFocus()
        self.app.processEvents()

        def press(key):
            QTest.keyClick(dialog.canvas, _enum(Qt, "Key", key))
            self.app.processEvents()

        press("Key_End")
        self.assertEqual(dialog.current_index, 2)
        press("Key_Home")
        self.assertEqual(dialog.current_index, 0)
        press("Key_Right")
        self.assertEqual(dialog.current_index, 1)
        press("Key_Left")
        self.assertEqual(dialog.current_index, 0)
        press("Key_Down")
        self.assertEqual(dialog.current_index, 1)
        press("Key_Up")
        self.assertEqual(dialog.current_index, 0)
        press("Key_Space")
        self.assertEqual(dialog.current_index, 1)
        dialog.close()

    def test_zoom_is_smooth_and_fits_window(self):
        from qgis.PyQt.QtCore import QPointF
        from qgis.PyQt.QtTest import QTest

        dialog = self._dialog()
        dialog.show()
        self.app.processEvents()
        canvas = dialog.canvas
        fit = canvas.scale
        self.assertGreater(fit, 0)

        canvas.zoom_at(2.0, animate=False)
        self.assertGreater(canvas.scale, fit)
        canvas.zoom_at(100.0, animate=False)
        self.assertLessEqual(canvas.scale, fit * 8.0 + 1e-6)

        canvas._offset = QPointF(40, 25)
        canvas._target_offset = QPointF(40, 25)
        canvas.zoom_at(0.001, animate=False)
        self.assertAlmostEqual(canvas.scale, fit)
        self.assertEqual(canvas.offset, QPointF())

        canvas.zoom_at(2.0)
        self.assertTrue(canvas.is_animating)
        QTest.qWait(300)
        self.assertFalse(canvas.is_animating)
        self.assertGreater(canvas.scale, fit * 1.5)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
