from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvasItem, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from ..core.measure import distance_area, format_length


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


class SheetSizeLabels(QgsMapCanvasItem):
    """Размеры страницы атласа вдоль границ прямоугольника.

    Подписи показывают, сколько местности покрывает лист: ширину —
    под нижней стороной, высоту — сбоку от левой.  Рисуются так же,
    как размеры рамки в окне выбора охвата: тёмные буквы с белой
    обводкой, без подложки.
    """

    def __init__(self, canvas, parent=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.rectangle = None
        self.crs = None
        self.width_text = ""
        self.height_text = ""
        self._size = QRectF()
        self.setZValue(999)
        self.setVisible(False)

    def set_rectangle(self, rectangle, crs):
        """Задаёт прямоугольник листа и подписи его сторон."""
        self.rectangle = QgsRectangle(rectangle)
        self.crs = crs
        self._update_text()
        self.updatePosition()
        self.setVisible(True)

    def _update_text(self):
        if self.rectangle is None:
            return
        # Расстояния считаются в системе координат самого листа.
        distance = distance_area(self.crs)
        bottom = self.rectangle.yMinimum()
        left = self.rectangle.xMinimum()
        width = distance.measureLine(
            QgsPointXY(left, bottom), QgsPointXY(self.rectangle.xMaximum(), bottom)
        ) / 1000.0
        height = distance.measureLine(
            QgsPointXY(left, bottom), QgsPointXY(left, self.rectangle.yMaximum())
        ) / 1000.0
        self.width_text = format_length(width)
        self.height_text = format_length(height)

    def updatePosition(self):
        if self.rectangle is None:
            return
        center = self.rectangle.center()
        self.setPos(self.toCanvasCoordinates(center))
        transform = self.canvas.mapSettings().mapToPixel()
        left = transform.transform(QgsPointXY(self.rectangle.xMinimum(), center.y()))
        right = transform.transform(
            QgsPointXY(self.rectangle.xMaximum(), center.y())
        )
        top = transform.transform(QgsPointXY(center.x(), self.rectangle.yMaximum()))
        bottom = transform.transform(QgsPointXY(center.x(), self.rectangle.yMinimum()))
        self._size = QRectF(
            left.x(), top.y(), right.x() - left.x(), bottom.y() - top.y()
        ).normalized()

    def boundingRect(self):
        return self._size.adjusted(-90, -60, 90, 60)

    def paint(self, painter, option, widget=None):
        if not self.width_text or not self.height_text:
            return
        painter.setRenderHint(
            _enum(QPainter, "RenderHint", "Antialiasing"), True
        )
        font = QFont()
        font.setBold(True)
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        size = self._size
        gap = 3.0
        height = metrics.height()

        def draw(rect, text, rotate=False):
            painter.save()
            if rotate:
                painter.translate(rect.center())
                painter.rotate(-90)
                rect = QRectF(
                    -rect.height() / 2, -rect.width() / 2,
                    rect.height(), rect.width(),
                )
            x = rect.left() + (rect.width() - metrics.horizontalAdvance(text)) / 2
            y = rect.top() + (rect.height() - metrics.height()) / 2 + metrics.ascent()
            path = QPainterPath()
            path.addText(x, y, font, text)
            pen = QPen(QColor(255, 255, 255), 3)
            pen.setJoinStyle(_enum(Qt, "PenJoinStyle", "RoundJoin"))
            painter.setPen(pen)
            painter.setBrush(_enum(Qt, "BrushStyle", "NoBrush"))
            painter.drawPath(path)
            painter.setPen(_enum(Qt, "PenStyle", "NoPen"))
            painter.setBrush(QColor(25, 25, 25))
            painter.drawPath(path)
            painter.restore()

        # Ширина — под нижней стороной, высота — сбоку от левой.
        draw(
            QRectF(
                size.left(), size.bottom() + gap,
                size.width(), height,
            ),
            self.width_text,
        )
        band = height + 2
        length = metrics.horizontalAdvance(self.height_text) + 6
        middle = size.center().y()
        draw(
            QRectF(
                size.left() - gap - band, middle - length / 2, band, length
            ),
            self.height_text,
            rotate=True,
        )


class SheetHandles(QgsMapCanvasItem):
    """Square corner handles for editable atlas sheets."""

    def __init__(self, canvas, tool):
        super().__init__(canvas)
        self.canvas = canvas
        self.tool = tool
        self.setZValue(1001)

    def boundingRect(self):
        return QRectF(-12, -12, self.canvas.width() + 24, self.canvas.height() + 24)

    def updatePosition(self):
        self.setPos(0, 0)
        self.update()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(
            _enum(QPainter, "RenderHint", "Antialiasing"), True
        )
        painter.setPen(QPen(QColor(0, 75, 140), 1))
        painter.setBrush(QColor(255, 255, 255, 245))
        for point in self.tool.handle_positions():
            painter.drawRect(QRectF(point.x() - 4, point.y() - 4, 8, 8))


class SheetEditMapTool(QgsMapTool):
    """Move atlas sheets and resize them by their corners."""

    sheetEdited = pyqtSignal(int, object)
    sheetActivated = pyqtSignal(int)

    def __init__(self, canvas, sheets, aspect=210.0 / 297.0):
        super().__init__(canvas)
        self.canvas = canvas
        self.sheets = sheets
        self.aspect = max(0.01, float(aspect))
        self._operation = None
        self._index = None
        self._corner = None
        self._original = None
        self._press_point = None
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(220, 140, 20))
        self.band.setFillColor(QColor(220, 140, 20, 45))
        self.band.setWidth(2)
        self.band.hide()
        self.handles = SheetHandles(canvas, self)
        self.handles.show()

    def set_aspect(self, aspect):
        self.aspect = max(0.01, float(aspect))

    def _canvas_crs(self):
        return self.canvas.mapSettings().destinationCrs()

    def _to_sheet(self, point, crs):
        canvas_crs = self._canvas_crs()
        if crs == canvas_crs:
            return QgsPointXY(point)
        return QgsCoordinateTransform(
            canvas_crs, crs, QgsProject.instance()
        ).transform(point)

    def _to_canvas(self, point, crs):
        canvas_crs = self._canvas_crs()
        if crs == canvas_crs:
            return QgsPointXY(point)
        return QgsCoordinateTransform(
            crs, canvas_crs, QgsProject.instance()
        ).transform(point)

    @staticmethod
    def _corners(rectangle):
        return (
            QgsPointXY(rectangle.xMinimum(), rectangle.yMinimum()),
            QgsPointXY(rectangle.xMaximum(), rectangle.yMinimum()),
            QgsPointXY(rectangle.xMaximum(), rectangle.yMaximum()),
            QgsPointXY(rectangle.xMinimum(), rectangle.yMaximum()),
        )

    def handle_positions(self):
        points = []
        for rectangle, crs in self.sheets:
            for corner in self._corners(rectangle):
                map_point = self._to_canvas(corner, crs)
                points.append(self.toCanvasCoordinates(map_point))
        return points

    def _hit(self, position, map_point):
        for index in range(len(self.sheets) - 1, -1, -1):
            rectangle, crs = self.sheets[index]
            for corner, point in enumerate(self._corners(rectangle)):
                screen = self.toCanvasCoordinates(self._to_canvas(point, crs))
                if abs(screen.x() - position.x()) <= 8 and abs(
                    screen.y() - position.y()
                ) <= 8:
                    return index, corner
        for index in range(len(self.sheets) - 1, -1, -1):
            rectangle, crs = self.sheets[index]
            if rectangle.contains(self._to_sheet(map_point, crs)):
                return index, None
        return None, None

    def canvasPressEvent(self, event):
        if event.button() != _enum(Qt, "MouseButton", "LeftButton"):
            return
        map_point = self.toMapCoordinates(event.pos())
        self._index, self._corner = self._hit(event.pos(), map_point)
        if self._index is None:
            return
        self.sheetActivated.emit(self._index)
        rectangle, crs = self.sheets[self._index]
        self._original = QgsRectangle(rectangle)
        self._press_point = self._to_sheet(map_point, crs)
        self._operation = "move" if self._corner is None else "resize"

    def _current_rectangle(self, map_point):
        if self._index is None or self._original is None:
            return QgsRectangle()
        rectangle, crs = self.sheets[self._index]
        current = self._to_sheet(map_point, crs)
        if self._operation == "move":
            dx = current.x() - self._press_point.x()
            dy = current.y() - self._press_point.y()
            return QgsRectangle(
                self._original.xMinimum() + dx,
                self._original.yMinimum() + dy,
                self._original.xMaximum() + dx,
                self._original.yMaximum() + dy,
            )
        corners = self._corners(self._original)
        anchor = corners[(self._corner + 2) % 4]
        selected = corners[self._corner]
        sign_x = 1.0 if selected.x() >= anchor.x() else -1.0
        sign_y = 1.0 if selected.y() >= anchor.y() else -1.0
        width = abs(current.x() - anchor.x())
        height = abs(current.y() - anchor.y())
        if width / self.aspect >= height:
            height = width / self.aspect
        else:
            width = height * self.aspect
        result = QgsRectangle(
            anchor.x(),
            anchor.y(),
            anchor.x() + sign_x * width,
            anchor.y() + sign_y * height,
        )
        result.normalize()
        return result

    def _show_preview(self, rectangle):
        if rectangle.isEmpty() or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        _original, crs = self.sheets[self._index]
        self.band.setToGeometry(QgsGeometry.fromRect(rectangle), crs)
        self.band.show()

    @staticmethod
    def _cursor_for_hit(index, corner):
        if index is None:
            return "ArrowCursor"
        if corner is None:
            return "SizeAllCursor"
        if corner in (0, 2):
            return "SizeBDiagCursor"
        return "SizeFDiagCursor"

    def canvasMoveEvent(self, event):
        map_point = self.toMapCoordinates(event.pos())
        if self._operation is None:
            index, corner = self._hit(event.pos(), map_point)
            cursor = self._cursor_for_hit(index, corner)
            self.canvas.setCursor(_enum(Qt, "CursorShape", cursor))
            return
        self._show_preview(self._current_rectangle(map_point))

    def canvasReleaseEvent(self, event):
        if (
            self._operation is None
            or event.button() != _enum(Qt, "MouseButton", "LeftButton")
        ):
            return
        rectangle = self._current_rectangle(self.toMapCoordinates(event.pos()))
        index = self._index
        self._clear_operation()
        if rectangle.isEmpty() or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self.sheetEdited.emit(index, rectangle)

    def _clear_operation(self):
        self._operation = None
        self._index = None
        self._corner = None
        self._original = None
        self._press_point = None
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.band.hide()
        self.handles.update()

    def deactivate(self):
        self._clear_operation()
        self.canvas.setCursor(_enum(Qt, "CursorShape", "ArrowCursor"))
        super().deactivate()

    def dispose(self):
        self._clear_operation()
        for item in (self.handles, self.band):
            try:
                scene = item.scene()
                if scene is not None:
                    scene.removeItem(item)
            except Exception:
                pass


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
