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


if __name__ == "__main__":
    unittest.main()
