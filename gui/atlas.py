from qgis.core import (
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvasItem, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPen


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


class SheetMarker(QgsMapCanvasItem):
    """Number drawn at the centre of an atlas sheet."""

    def __init__(self, canvas, number, parent=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.number = number
        self.point = None
        self.setZValue(1000)
        self.setVisible(False)

    def set_number(self, number):
        self.number = number
        self.update()

    def set_map_point(self, point):
        self.point = QgsPointXY(point)
        self.updatePosition()
        self.setVisible(True)

    def updatePosition(self):
        if self.point is not None:
            self.setPos(self.toCanvasCoordinates(self.point))

    def boundingRect(self):
        return QRectF(-14, -14, 28, 28)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(
            _enum(QPainter, "RenderHint", "Antialiasing"), True
        )
        painter.setPen(QPen(QColor(0, 120, 215), 2))
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawEllipse(QPointF(0, 0), 12, 12)
        painter.setPen(QColor(0, 75, 140))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(14)
        painter.setFont(font)
        painter.drawText(
            QRectF(-12, -12, 24, 24),
            _enum(Qt, "AlignmentFlag", "AlignCenter"),
            str(self.number),
        )


class SheetMapTool(QgsMapTool):
    """Draw atlas sheets with the aspect ratio of the selected page."""

    sheetDrawn = pyqtSignal(object)

    def __init__(self, canvas, aspect=210.0 / 297.0):
        super().__init__(canvas)
        self.canvas = canvas
        self.aspect = max(0.01, float(aspect))
        self.start_point = None
        self.number = 1
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(0, 120, 215))
        self.band.setFillColor(QColor(0, 120, 215, 35))
        self.band.setWidth(2)
        self.band.hide()
        self.marker = SheetMarker(canvas, self.number)
        self.marker.hide()

    def set_aspect(self, aspect):
        self.aspect = max(0.01, float(aspect))

    def set_number(self, number):
        self.number = max(1, int(number))
        self.marker.set_number(self.number)

    def canvasPressEvent(self, event):
        if event.button() != _enum(Qt, "MouseButton", "LeftButton"):
            return
        self.start_point = self.toMapCoordinates(event.pos())
        self.marker.set_number(self.number)

    def canvasMoveEvent(self, event):
        if self.start_point is None:
            return
        rectangle = self._rectangle(self.toMapCoordinates(event.pos()))
        if rectangle.isEmpty():
            return
        self.band.setToGeometry(
            QgsGeometry.fromRect(rectangle),
            self.canvas.mapSettings().destinationCrs(),
        )
        self.band.show()
        self.marker.set_map_point(rectangle.center())

    def canvasReleaseEvent(self, event):
        if (
            self.start_point is None
            or event.button() != _enum(Qt, "MouseButton", "LeftButton")
        ):
            return
        rectangle = self._rectangle(self.toMapCoordinates(event.pos()))
        self.start_point = None
        self.band.hide()
        self.marker.hide()
        if rectangle.isEmpty():
            return
        if rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self.sheetDrawn.emit(rectangle)

    def _rectangle(self, current):
        dx = current.x() - self.start_point.x()
        dy = current.y() - self.start_point.y()
        sign_x = 1.0 if dx >= 0 else -1.0
        sign_y = 1.0 if dy >= 0 else -1.0
        width = abs(dx)
        height = abs(dy)
        if width <= 0 or height <= 0:
            return QgsRectangle()
        if width / self.aspect >= height:
            height = width / self.aspect
        else:
            width = height * self.aspect
        rectangle = QgsRectangle(
            self.start_point.x(),
            self.start_point.y(),
            self.start_point.x() + sign_x * width,
            self.start_point.y() + sign_y * height,
        )
        rectangle.normalize()
        return rectangle

    def _clear_preview(self):
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.band.hide()
        self.marker.hide()

    def dispose(self):
        self.start_point = None
        self._clear_preview()
        for item in (self.marker, self.band):
            try:
                scene = item.scene()
                if scene is not None:
                    scene.removeItem(item)
            except Exception:
                pass

    def deactivate(self):
        self.start_point = None
        self._clear_preview()
        super().deactivate()
