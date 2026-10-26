from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:
    from qgis.PyQt.QtGui import QAction

from .core.expressions import load_expressions
from .core.resources import PACKAGE_DIR
from .gui.dockwidget import XtopoDockWidget


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
        self.action.setToolTip("Подготовка OSM и рельефа для топографической карты")
        self.action.triggered.connect(self.show_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&QGIS-xtopo", self.action)
        self.dock = XtopoDockWidget(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(_right_dock_area(), self.dock)
        self.dock.hide()
        load_expressions()

    def unload(self):
        if self.dock is not None:
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
