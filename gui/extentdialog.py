from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsMapToolPan, QgsRubberBand
from qgis.PyQt.QtCore import QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QKeySequence
from qgis.PyQt.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QShortcut, QVBoxLayout,
)

from ..core.bbox import BBox, parse_bbox

OSM_TILE_URI = (
    "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0"
)
MERCATOR_EXTENT = 20037508.342789244
MAX_LATITUDE = 85.0511287798

DRAW_TOOL_STYLE = (
    "QPushButton {"
    " border: 2px solid palette(highlight);"
    " border-radius: 4px;"
    " padding: 2px;"
    " background-color: palette(base);"
    "}"
    "QPushButton:checked {"
    " background-color: palette(highlight);"
    "}"
)


def _left_button():
    try:
        return Qt.MouseButton.LeftButton
    except AttributeError:
        return Qt.LeftButton


class RectangleMapTool(QgsMapTool):
    """Drag with the left mouse button to outline a rectangular bbox."""

    rectangleDrawn = pyqtSignal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.start_point = None
        self.end_point = None
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(0, 120, 215))
        self.band.setFillColor(QColor(0, 120, 215, 40))
        self.band.setWidth(2)
        self.band.hide()

    def canvasPressEvent(self, event):
        if event.button() != _left_button():
            return
        self.start_point = self.toMapCoordinates(event.pos())
        self.end_point = self.start_point
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self._draw_band()

    def canvasMoveEvent(self, event):
        if self.start_point is None:
            return
        self.end_point = self.toMapCoordinates(event.pos())
        self._draw_band()

    def canvasReleaseEvent(self, event):
        if self.start_point is None or event.button() != _left_button():
            return
        self.end_point = self.toMapCoordinates(event.pos())
        rectangle = self._rectangle()
        self.start_point = None
        self.end_point = None
        if rectangle.width() > 0 and rectangle.height() > 0:
            self.rectangleDrawn.emit(rectangle)

    def _rectangle(self):
        rectangle = QgsRectangle(self.start_point, self.end_point)
        rectangle.normalize()
        return rectangle

    def _draw_band(self):
        self.band.setToGeometry(QgsGeometry.fromRect(self._rectangle()), None)
        self.band.show()

    def show_rectangle(self, rectangle):
        """Показать готовый прямоугольник без рисования мышью."""
        if rectangle is None or rectangle.isEmpty():
            return
        self.band.setToGeometry(QgsGeometry.fromRect(rectangle), None)
        self.band.show()

    def clear(self):
        self.start_point = None
        self.end_point = None
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.band.hide()


class ExtentDialog(QDialog):
    def __init__(self, bbox_text="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор охвата карты")
        self._fitted_to_screen = False
        self._fit_to_screen()
        self._bbox = None
        self._drawn_extent = None
        self._wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        self._mercator = QgsCoordinateReferenceSystem("EPSG:3857")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        tools = QHBoxLayout()
        self.draw_button = QPushButton()
        self.draw_button.setCheckable(True)
        self.draw_button.setIcon(
            QgsApplication.getThemeIcon("mActionSelectRectangle.svg")
        )
        self.draw_button.setIconSize(QSize(22, 22))
        self.draw_button.setMinimumSize(46, 38)
        self.draw_button.setAccessibleName("Нарисовать bbox")
        self.draw_button.setStyleSheet(DRAW_TOOL_STYLE)
        if self.draw_button.icon().isNull():
            self.draw_button.setText("Нарисовать bbox")
        self.draw_button.setToolTip(
            "Нарисовать bbox: зажмите левую кнопку мыши на карте "
            "и протяните прямоугольник"
        )
        self.draw_button.toggled.connect(self._toggle_draw)
        tools.addWidget(self.draw_button)
        self.reset_button = QPushButton("Сбросить")
        self.reset_button.setEnabled(False)
        self.reset_button.setToolTip("Убрать нарисованный прямоугольник")
        self.reset_button.clicked.connect(self._reset_drawn)
        tools.addWidget(self.reset_button)
        tools.addStretch(1)
        self.coords = QLabel("")
        tools.addWidget(self.coords)
        layout.addLayout(tools)

        self.canvas = QgsMapCanvas(self)
        self.canvas.setCanvasColor(QColor("white"))
        self.canvas.enableAntiAliasing(True)
        self.canvas.setDestinationCrs(self._mercator)
        self.layer = QgsRasterLayer(OSM_TILE_URI, "OpenStreetMap", "wms")
        if self.layer.isValid():
            self.canvas.setLayers([self.layer])
        self._pan_tool = QgsMapToolPan(self.canvas)
        self._draw_tool = RectangleMapTool(self.canvas)
        self._draw_tool.rectangleDrawn.connect(self._rectangle_drawn)
        self.canvas.setMapTool(self._pan_tool)
        initial, remembered = self._initial_extent(bbox_text)
        self.canvas.setExtent(
            self._padded_extent(initial) if remembered else initial
        )
        if remembered:
            self._drawn_extent = QgsRectangle(initial)
            self._draw_tool.show_rectangle(self._drawn_extent)
            self.reset_button.setEnabled(True)
        self.canvas.extentsChanged.connect(self._update_coords)
        self.canvas.viewport().installEventFilter(self)
        layout.addWidget(self.canvas, 1)

        hint = QLabel(
            "Перетаскивайте карту мышью, колесом меняйте масштаб. Нажмите "
            "кнопку с иконкой прямоугольника и протяните рамку, чтобы задать "
            "область точнее. Затем нажмите «Использовать охват»."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        attribution = QLabel(
            '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        )
        attribution.setOpenExternalLinks(True)
        bottom.addWidget(attribution)
        bottom.addStretch(1)
        use_button = QPushButton("Использовать охват")
        use_button.setDefault(True)
        use_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        bottom.addWidget(use_button)
        bottom.addWidget(cancel_button)
        layout.addLayout(bottom)

        # Enter применяет охват и закрывает окно: канвас забирает фокус,
        # поэтому нужен явный shortcut.
        for key in (Qt.Key_Return, Qt.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(self.accept)

        self._update_coords()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        super().keyPressEvent(event)

    def _fit_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1100, 760)
            return
        available = screen.availableGeometry()
        width = min(available.width(), max(760, round(available.width() * 0.94)))
        height = min(available.height(), max(520, round(available.height() * 0.94)))
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def showEvent(self, event):
        if not self._fitted_to_screen:
            self._fitted_to_screen = True
            self._fit_to_screen()
        super().showEvent(event)

    def _toggle_draw(self, active):
        if active:
            self.canvas.setMapTool(self._draw_tool)
        else:
            self.canvas.setMapTool(self._pan_tool)

    def _rectangle_drawn(self, rectangle):
        if rectangle is None or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._drawn_extent = QgsRectangle(rectangle)
        self._drawn_extent.normalize()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(True)
        self._update_coords()

    def _reset_drawn(self):
        self._drawn_extent = None
        self._draw_tool.clear()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(False)
        self._update_coords()

    @staticmethod
    def _padded_extent(extent):
        """Отступ от краёв, чтобы границы прямоугольника не шли по краям."""
        margin = max(extent.width(), extent.height()) * 0.12
        padded = QgsRectangle(extent)
        padded.grow(margin)
        return padded

    def _initial_extent(self, bbox_text):
        """Прямоугольник карты и признак того, что bbox был задан."""
        if bbox_text:
            try:
                bbox = parse_bbox(bbox_text)
            except ValueError:
                pass
            else:
                lat_min = max(-MAX_LATITUDE, bbox.lat_min)
                lat_max = min(MAX_LATITUDE, bbox.lat_max)
                transform = QgsCoordinateTransform(
                    self._wgs84, self._mercator, QgsProject.instance()
                )
                extent = transform.transformBoundingBox(
                    QgsRectangle(bbox.lon_min, lat_min, bbox.lon_max, lat_max)
                )
                extent.normalize()
                return extent, True
        return QgsRectangle(
            -MERCATOR_EXTENT, -MERCATOR_EXTENT, MERCATOR_EXTENT, MERCATOR_EXTENT
        ), False

    def _update_coords(self, *_args):
        try:
            bbox = self._extent_as_bbox()
        except Exception:
            self.coords.setText("")
            return
        self.coords.setText(
            f"{bbox.lon_min:.4f}, {bbox.lat_min:.4f}, "
            f"{bbox.lon_max:.4f}, {bbox.lat_max:.4f}"
        )

    def _extent_as_bbox(self):
        transform = QgsCoordinateTransform(
            self.canvas.mapSettings().destinationCrs(), self._wgs84, QgsProject.instance()
        )
        source = (
            self._drawn_extent
            if self._drawn_extent is not None
            else self.canvas.extent()
        )
        extent = transform.transformBoundingBox(source)
        lon_min = max(-180.0, extent.xMinimum())
        lat_min = max(-90.0, extent.yMinimum())
        lon_max = min(180.0, extent.xMaximum())
        lat_max = min(90.0, extent.yMaximum())
        if lon_min >= lon_max or lat_min >= lat_max:
            raise ValueError("Visible extent is outside the valid coordinates")
        return BBox(lon_min, lat_min, lon_max, lat_max)

    def bbox_csv(self):
        return self._bbox.as_csv()

    def accept(self):
        try:
            self._bbox = self._extent_as_bbox()
        except Exception as exc:
            QMessageBox.warning(self, "QGIS-xtopo", f"Не удалось определить охват: {exc}")
            return
        super().accept()

    def done(self, result):
        self._draw_tool.clear()
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        super().done(result)
