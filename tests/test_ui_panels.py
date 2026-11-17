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
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = (
            "slice",
            self._slice_estimate(nodes=120_000_000, nodes_limit=100_000_000),
        )
        dock._render_slice_estimate()
        self.assertIn("Предел SliceOSM", dock.pbf_info.text())

    def test_slice_estimate_failure_does_not_hide_mode(self):
        dock = self._dock()
        dock.source_mode.setCurrentIndex(dock.source_mode.findData("slice"))
        dock._slice_estimate_timer.stop()
        dock._slice_estimate_state = ("error", "нет сети")
        dock._render_slice_estimate()
        self.assertFalse(dock.pbf_info.isHidden())
        self.assertIn("недоступна", dock.pbf_info.text())

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


if __name__ == "__main__":
    unittest.main()
