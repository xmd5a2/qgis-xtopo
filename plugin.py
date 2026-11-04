from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

import os

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction

from .core import i18n, map_language
from .core.expressions import load_expressions
from .core.resources import PACKAGE_DIR
from .core.transliteration import functions_available
from .gui.dockwidget import XtopoDockWidget

ACTION_TOOLTIP = "Подготовка OSM и рельефа для топографической карты"


def _right_dock_area():
    try:
        return Qt.DockWidgetArea.RightDockWidgetArea
    except AttributeError:
        return Qt.RightDockWidgetArea


class QgisXtopoPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None

    def initGui(self):
        self.action = QAction(
            QIcon(str(PACKAGE_DIR / "logo_icon.png")),
            "QGIS-xtopo",
            self.iface.mainWindow(),
        )
        self.action.setToolTip(i18n.tr(ACTION_TOOLTIP))
        self.action.triggered.connect(self.show_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&QGIS-xtopo", self.action)
        i18n.load_language()
        map_language.load_map_language()
        map_language.load_transliteration()
        map_language.load_level_of_detail()
        i18n.add_listener(self._language_changed)
        self.dock = XtopoDockWidget(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(_right_dock_area(), self.dock)
        self.dock.hide()
        if os.environ.get("QGIS_XTOPO_OPEN"):
            # Запуск из tools/run_qgis.sh: показываем панель после того, как
            # QGIS восстановит сохранённое состояние окон (иначе оно наш
            # show() перезапишет).
            self.iface.initializationCompleted.connect(self.show_dock)
        load_expressions()
        if not functions_available():
            # Плагин перезагрузили без перезапуска QGIS: функции выражений
            # регистрируются заново, иначе подписи проекта не рисуются.
            load_expressions(force=True)

    def _language_changed(self, _code=None):
        if self.action is not None:
            self.action.setToolTip(i18n.tr(ACTION_TOOLTIP))

    def unload(self):
        i18n.remove_listener(self._language_changed)
        if self.dock is not None:
            # Возвращаем панели QGIS, скрытые плагином.
            self.dock.detach_language_listener()
            self.dock.restore_panels()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("&QGIS-xtopo", self.action)
            self.action.deleteLater()
            self.action = None

    def show_dock(self):
        if self.dock is not None:
            self.dock.show()
            self.dock.raise_()
