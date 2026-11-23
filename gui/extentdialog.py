import re
from dataclasses import dataclass

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsMapCanvas, QgsMapCanvasItem, QgsMapTool, QgsMapToolPan,
    QgsRubberBand,
)
from qgis.PyQt.QtCore import QRectF, QSize, Qt, QThread, QTimer, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor, QKeySequence, QPainter, QPainterPath, QPen, QStandardItem,
    QStandardItemModel,
)
from qgis.PyQt.QtWidgets import (
    QApplication, QComboBox, QCompleter, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QShortcut, QSizePolicy,
    QVBoxLayout,
)

from ..core import i18n, nominatim
from ..core.bbox import BBox, parse_bbox
from ..core.measure import mercator_ground_factor


@dataclass(frozen=True)
class Basemap:
    """Подложка карты в окне выбора охвата."""

    key: str
    name: str
    template: str
    zmax: int
    attribution: str

    @property
    def attribution_text(self):
        """Атрибуция без ссылок: рисуется поверх карты как обычный текст."""
        return re.sub(r"<[^>]+>", "", self.attribution).strip()

    @property
    def uri(self):
        """URI слоя тайлов.

        Внутренние ``&`` адреса кодируются: иначе разбор URI разорвал бы
        шаблон Google на отдельные параметры.
        """
        url = self.template.replace("&", "%26")
        return f"type=xyz&url={url}&zmax={self.zmax}&zmin=0"


# Подложки предпросмотра.  В готовую карту они не попадают: там только
# данные OSM и рельеф.
BASEMAPS = (
    Basemap(
        "osm",
        "OpenStreetMap",
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        19,
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        " contributors",
    ),
    Basemap(
        "opentopo",
        "OpenTopoMap",
        "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        17,
        '© <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA), '
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        " contributors",
    ),
    Basemap(
        "arcgis",
        "ArcGIS Satellite",
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        19,
        '© <a href="https://www.esri.com">Esri</a>, Maxar, Earthstar '
        "Geographics, and the GIS User Community",
    ),
    Basemap(
        "google_satellite",
        "Google Satellite",
        "https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
        20,
        '© <a href="https://www.google.com">Google</a>',
    ),
    Basemap(
        "google_hybrid",
        "Google Hybrid",
        "https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}",
        20,
        '© <a href="https://www.google.com">Google</a>',
    ),
)

BASEMAP_BY_KEY = {basemap.key: basemap for basemap in BASEMAPS}

MERCATOR_EXTENT = 20037508.342789244
MAX_LATITUDE = 85.0511287798

# Размер окна вокруг точки, если у найденного места нет границ.
PLACE_SPAN = 0.02
# Поиск начинается по мере ввода: пауза после последнего нажатия и
# минимальная длина запроса, иначе Nominatim дёргался бы на каждую букву.
SEARCH_DELAY_MS = 450
MIN_SEARCH_LENGTH = 2
SEARCH_FIELD_WIDTH = 380
# Выпадающий список не уже поля.
SEARCH_POPUP_WIDTH = 380

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

# Цвет рамки охвата и её подписей размеров.
FRAME_COLOR = QColor(0, 120, 215)
FRAME_FILL_COLOR = QColor(0, 120, 215, 40)


def _left_button():
    try:
        return Qt.MouseButton.LeftButton
    except AttributeError:
        return Qt.LeftButton


def _right_button():
    try:
        return Qt.MouseButton.RightButton
    except AttributeError:
        return Qt.RightButton


# Подписи размеров — общие для окна выбора охвата и разметки листов
# атласа, поэтому живут в core/measure.py.
from ..core.measure import (  # noqa: E402,F401
    format_length, frame_sizes_km, scale_bar,
)


class OneLineLabel(QLabel):
    """Однострочная подпись, которая не переносится и не расширяет окно.

    Оценка подготовки бывает длинной; если дать QLabel её полную ширину,
    кнопки уезжают или текст переносится на вторую строку.  Здесь
    подпись занимает доступное место, а хвост сокращается многоточием;
    полный текст остаётся в подсказке.
    """

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(0)
        try:
            self.setTextFormat(Qt.TextFormat.PlainText)
        except AttributeError:
            self.setTextFormat(Qt.PlainText)
        self.setText(text)

    def setText(self, text):
        self._full_text = text or ""
        super().setToolTip(self._full_text)
        self.updateGeometry()
        self._apply_elide()

    def full_text(self):
        return self._full_text

    def sizeHint(self):
        hint = super().sizeHint()
        width = self.fontMetrics().horizontalAdvance(self._full_text) + 2
        return QSize(width, hint.height())

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self):
        width = max(0, self.width())
        if width <= 0:
            # Ширина ещё не выделена: показываем полный текст, чтобы
            # sizeHint и компоновка дали подписи место.
            super().setText(self._full_text)
            return
        text = self.fontMetrics().elidedText(
            self._full_text, Qt.ElideRight, width
        )
        super().setText(text)


class MeasuredCanvas(QgsMapCanvas):
    """Холст, подписывающий размеры сторон рамки охвата.

    Рядом с каждой осью рамки показывается длина стороны: по горизонтали —
    ширина, по вертикали — высота. Подписи рисуются поверх карты, поэтому
    отдельный слой или элемент управления не нужны. В левом нижнем углу
    рисуется шкала масштаба, в правом — атрибуция подложки.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = None
        self._width_label = ""
        self._height_label = ""
        self._frame_provider = None
        self._attribution = ""
        self._scale_bar_rect = None
        self._attribution_rect = None

    def set_attribution(self, text):
        """Текст атрибуции поверх карты (без ссылок и разметки)."""
        self._attribution = " ".join(str(text or "").split())
        self.update()

    def attribution_text(self):
        return self._attribution

    def set_frame(self, rectangle, width_label="", height_label=""):
        """Задаёт рамку (в координатах холста) и подписи её сторон."""
        self._frame = QgsRectangle(rectangle) if rectangle is not None else None
        self._width_label = width_label or ""
        self._height_label = height_label or ""
        self.update()

    def set_frame_provider(self, provider):
        """Источник рамки и подписей.

        Поставщик вызывается при каждой отрисовке: подписи размеров
        сторон идут за рамкой даже при перемещении карты, когда сама
        рамка в координатах не меняется.
        """
        self._frame_provider = provider
        self.update()

    def _current_frame(self):
        if self._frame_provider is not None:
            try:
                rectangle, width_label, height_label = self._frame_provider()
            except Exception:
                return None, "", ""
            if rectangle is None:
                return None, "", ""
            return QgsRectangle(rectangle), width_label, height_label
        return self._frame, self._width_label, self._height_label

    def _scene_shift(self):
        """Сдвиг сцены при перетаскивании карты в координатах viewport."""
        origin = self.sceneRect().topLeft()
        return -origin.x(), -origin.y()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._draw_scale_bar()
        self._draw_attribution()
        frame, self._width_label, self._height_label = self._current_frame()
        if frame is None:
            return
        transform = self.getCoordinateTransform()

        def device(x, y):
            point = transform.transform(QgsPointXY(x, y))
            return point.x(), point.y()

        left, top = device(frame.xMinimum(), frame.yMaximum())
        right, bottom = device(frame.xMaximum(), frame.yMinimum())
        # Во время перетаскивания карты QgsMapCanvas сдвигает сцену,
        # а не пересчитывает преобразование координат. Подписи рисуются
        # поверх viewport, поэтому их нужно сдвинуть вместе со сценой,
        # иначе цифры остаются на старом месте до отпускания мыши.
        shift_x, shift_y = self._scene_shift()
        left += shift_x
        right += shift_x
        top += shift_y
        bottom += shift_y
        if not self._width_label or not self._height_label:
            return
        # QgsMapCanvas рисует карту во viewport(): сам виджет — только рамка,
        # и на нём QPainter не активируется.
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            font.setBold(True)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            height = metrics.height()
            gap = 3.0
            view = self.viewport()
            # Горизонтальная ось: ширина — одна подпись под нижней стороной
            # рамки (если не помещается — над верхней).
            if bottom + gap + height <= view.height():
                width_rect = QRectF(left, bottom + gap, right - left, height)
            else:
                width_rect = QRectF(left, top - height - gap, right - left, height)
            self._draw_label(painter, width_rect, self._width_label, font, metrics)
            # Вертикальная ось: высота — одна подпись вплотную к левой
            # стороне рамки, текст повёрнут (если не помещается — к правой).
            band = height + 2
            length = metrics.horizontalAdvance(self._height_label) + 6
            middle = (top + bottom) / 2
            if left - gap - band >= 0:
                side_rect = QRectF(left - gap - band, middle - length / 2, band, length)
            else:
                side_rect = QRectF(right + gap, middle - length / 2, band, length)
            self._draw_label(
                painter, side_rect, self._height_label, font, metrics, rotate=True
            )
        finally:
            painter.end()

    def _draw_scale_bar(self):
        """Шкала масштаба в левом нижнем углу карты.

        Длина полоски подбирается так, чтобы она соответствовала
        «круглому» расстоянию (1, 2 или 5 × 10ⁿ метров).
        """
        self._scale_bar_rect = None
        painter = QPainter(self.viewport())
        try:
            view = self.viewport()
            pixels = view.width()
            if pixels <= 0:
                return
            # Метров в пикселе: ширина видимой области (в CRS холста —
            # EPSG:3857, то есть в метрах) делим на ширину окна.
            # Прежде использовался знаменатель масштаба, а это другое
            # число, и полоса выходила в тысячи раз короче.
            extent = self.extent()
            span = extent.width()
            if not span or span <= 0:
                return
            meters_per_pixel = span / pixels
            # Холст в EPSG:3857: его метры растянуты с широтой, поэтому
            # для подписи шкалы переводим их в метры на местности.
            meters_per_pixel *= mercator_ground_factor(extent.center().y())
            metrics = painter.fontMetrics()
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            painter.setFont(font)
            length, text = scale_bar(meters_per_pixel)
            if not length:
                return
            margin = 10
            left = margin
            bottom = view.height() - margin
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Классическая шкала: чередующиеся чёрные и белые четверти,
            # подпись — над полосой.  Раньше полоса сливалась с рамкой.
            bar_height = 6.0
            bar_top = bottom - bar_height
            segment = length / 4.0
            for index in range(4):
                rect = QRectF(
                    left + index * segment, bar_top, segment, bar_height
                )
                painter.setPen(QPen(QColor(25, 25, 25), 1))
                painter.setBrush(
                    QColor(25, 25, 25) if index % 2 == 0
                    else QColor(255, 255, 255)
                )
                painter.drawRect(rect)
            painter.setPen(QPen(QColor(25, 25, 25), 1))
            painter.setBrush(Qt.NoBrush)
            text_rect = QRectF(
                left, bar_top - metrics.height() - 2,
                length, metrics.height(),
            )
            painter.drawText(text_rect, Qt.AlignCenter, text)
            self._scale_bar_rect = QRectF(
                left, text_rect.top(), length, text_rect.height() + bar_height + 2
            )
        finally:
            painter.end()

    def _draw_attribution(self):
        """Атрибуция подложки поверх карты в правом нижнем углу."""
        self._attribution_rect = None
        if not self._attribution:
            return
        painter = QPainter(self.viewport())
        try:
            view = self.viewport()
            if view.width() <= 0 or view.height() <= 0:
                return
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            painter.setFont(font)
            metrics = painter.fontMetrics()
            margin = 10
            # Слева оставляем место шкале масштаба, чтобы подписи не
            # наезжали друг на друга на узком окне.
            available = max(0, view.width() - 180 - 2 * margin)
            text = metrics.elidedText(self._attribution, Qt.ElideRight, available)
            width = metrics.horizontalAdvance(text)
            rect = QRectF(
                view.width() - margin - width,
                view.height() - margin - metrics.height(),
                width,
                metrics.height(),
            )
            self._draw_label(
                painter, rect, text, font, metrics, color=QColor(25, 25, 25)
            )
            self._attribution_rect = QRectF(rect)
        finally:
            painter.end()

    @staticmethod
    def _draw_label(painter, rect, text, font, metrics, rotate=False,
                    color=FRAME_COLOR):
        """Подпись без подложки: тёмные буквы с белой обводкой.

        Обводка нужна, чтобы подпись читалась на любом фоне карты.
        """
        painter.save()
        if rotate:
            # Текст вдоль вертикальной оси: поворот на 90° против часовой
            # стрелки, подпись читается снизу вверх.
            painter.translate(rect.center())
            painter.rotate(-90)
            rect = QRectF(
                -rect.height() / 2, -rect.width() / 2, rect.height(), rect.width()
            )
        x = rect.left() + (rect.width() - metrics.horizontalAdvance(text)) / 2
        y = rect.top() + (rect.height() - metrics.height()) / 2 + metrics.ascent()
        path = QPainterPath()
        path.addText(x, y, font, text)
        pen = QPen(QColor(255, 255, 255), 3)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPath(path)
        painter.restore()


class RectangleMapTool(QgsMapTool):
    """Drag with the left mouse button to outline a rectangular extent.

    The right mouse button pans the map, so the extent can be positioned
    without leaving the drawing tool.
    """

    rectangleDrawn = pyqtSignal(object)
    rectangleChanged = pyqtSignal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.start_point = None
        self.end_point = None
        self.pan_point = None
        self.band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.band.setColor(QColor(FRAME_COLOR))
        self.band.setFillColor(QColor(FRAME_FILL_COLOR))
        self.band.setWidth(2)
        self.band.hide()

    def canvasPressEvent(self, event):
        if event.button() == _right_button():
            if self.start_point is None:
                self._start_pan(event.pos())
            return
        if event.button() != _left_button() or self.pan_point is not None:
            return
        self.start_point = self.toMapCoordinates(event.pos())
        self.end_point = self.start_point
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self._draw_band()

    def canvasMoveEvent(self, event):
        if self.pan_point is not None:
            if event.buttons() & _right_button():
                self._pan(event.pos())
            else:
                self._stop_pan()
            return
        if self.start_point is None:
            return
        self.end_point = self.toMapCoordinates(event.pos())
        self._draw_band()
        # Подписи размеров сторон идут за рамкой, пока её рисуют.
        rectangle = self._rectangle()
        if rectangle.width() > 0 and rectangle.height() > 0:
            self.rectangleChanged.emit(rectangle)

    def canvasReleaseEvent(self, event):
        if event.button() == _right_button() and self.pan_point is not None:
            self._stop_pan()
            return
        if self.start_point is None or event.button() != _left_button():
            return
        self.end_point = self.toMapCoordinates(event.pos())
        rectangle = self._rectangle()
        self.start_point = None
        self.end_point = None
        if rectangle.width() > 0 and rectangle.height() > 0:
            self.rectangleDrawn.emit(rectangle)

    def _start_pan(self, position):
        self.pan_point = position
        self._pan_origin = position
        self._pan_extent = QgsRectangle(self.canvas.extent())
        self._pan_units = self.canvas.mapUnitsPerPixel()
        self._pan_cursor = self.canvas.cursor()
        self.canvas.setCursor(Qt.ClosedHandCursor)

    def _pan(self, position):
        delta = position - self._pan_origin
        if delta.isNull():
            return
        # Карта следует за курсором: тянем вправо — область уходит влево.
        dx = -delta.x() * self._pan_units
        dy = delta.y() * self._pan_units
        extent = self._pan_extent
        self.canvas.setExtent(
            QgsRectangle(
                extent.xMinimum() + dx,
                extent.yMinimum() + dy,
                extent.xMaximum() + dx,
                extent.yMaximum() + dy,
            )
        )
        self.canvas.refresh()

    def _stop_pan(self):
        if self.pan_point is None:
            return
        self.pan_point = None
        self.canvas.setCursor(getattr(self, "_pan_cursor", Qt.ArrowCursor))

    def deactivate(self):
        self._stop_pan()
        super().deactivate()

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
        self._stop_pan()
        self.start_point = None
        self.end_point = None
        self.band.reset(QgsWkbTypes.PolygonGeometry)
        self.band.hide()


class ExtentHandles(QgsMapCanvasItem):
    """Квадратные маркеры углов нарисованной рамки охвата."""

    def __init__(self, canvas, tool):
        super().__init__(canvas)
        self.canvas = canvas
        self.tool = tool
        self.setZValue(1001)

    def boundingRect(self):
        return QRectF(
            -12, -12, self.canvas.width() + 24, self.canvas.height() + 24
        )

    def updatePosition(self):
        self.setPos(0, 0)
        self.update()

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(0, 75, 140), 1))
        painter.setBrush(QColor(255, 255, 255, 245))
        for point in self.tool.handle_positions():
            painter.drawRect(
                QRectF(point.x() - 4, point.y() - 4, 8, 8)
            )


class ExtentPanTool(QgsMapToolPan):
    """Перетаскивание карты; рамка двигается и меняет размер за углы."""

    def __init__(self, canvas, extent_provider, extent_shower=None,
                 extent_changed=None, extent_finished=None):
        super().__init__(canvas)
        self.canvas = canvas
        self._extent_provider = extent_provider
        self._show_extent = extent_shower
        self._extent_changed = extent_changed
        self._extent_finished = extent_finished
        self._operation = None
        self._corner = None
        self._start_extent = None
        self._extent = None
        self._pan_point = None
        self._pan_origin = None
        self._pan_extent = None
        self._pan_units = None
        self.handles = ExtentHandles(canvas, self)
        self.handles.hide()

    @staticmethod
    def _corners(rectangle):
        return (
            QgsPointXY(rectangle.xMinimum(), rectangle.yMinimum()),
            QgsPointXY(rectangle.xMaximum(), rectangle.yMinimum()),
            QgsPointXY(rectangle.xMaximum(), rectangle.yMaximum()),
            QgsPointXY(rectangle.xMinimum(), rectangle.yMaximum()),
        )

    def _normalized_extent(self):
        rectangle = self._extent_provider()
        if rectangle is None or rectangle.isEmpty():
            return None
        rectangle = QgsRectangle(rectangle)
        rectangle.normalize()
        return rectangle

    def handle_positions(self):
        rectangle = self._normalized_extent()
        if rectangle is None:
            return []
        return [
            self.toCanvasCoordinates(corner)
            for corner in self._corners(rectangle)
        ]

    def _corner_at(self, position):
        for index, point in enumerate(self.handle_positions()):
            if abs(point.x() - position.x()) <= 8 and abs(
                point.y() - position.y()
            ) <= 8:
                return index
        return None

    def _extent_at(self, position):
        rectangle = self._normalized_extent()
        if rectangle is None:
            return None
        if not rectangle.contains(self.toMapCoordinates(position)):
            return None
        return rectangle

    def refresh_handles(self):
        if self.handles is None:
            return
        active = self.canvas.mapTool() is self
        self.handles.setVisible(
            active and self._extent_provider() is not None
        )
        self.handles.update()

    def activate(self):
        super().activate()
        if self.handles is not None:
            self.handles.setVisible(
                self._extent_provider() is not None
            )
            self.handles.update()

    def canvasPressEvent(self, event):
        if event.button() == _right_button():
            # Правая кнопка панорамирует и в режиме без инструмента выбора:
            # левая уже занята перетаскиванием рамки и её углов.
            self._start_pan(event.pos())
            return
        if event.button() == _left_button():
            corner = self._corner_at(event.pos())
            rectangle = self._normalized_extent()
            if corner is not None and rectangle is not None:
                self._start_extent = rectangle
                self._extent = QgsRectangle(rectangle)
                self._press_point = self.toMapCoordinates(event.pos())
                self._operation = "resize"
                self._corner = corner
                self.canvas.setCursor(self._resize_cursor(corner))
                return
            rectangle = self._extent_at(event.pos())
            if rectangle is not None:
                self._start_extent = rectangle
                self._extent = QgsRectangle(rectangle)
                self._press_point = self.toMapCoordinates(event.pos())
                self._operation = "move"
                self._corner = None
                self.canvas.setCursor(Qt.SizeAllCursor)
                return
        self._clear_operation()
        super().canvasPressEvent(event)

    @staticmethod
    def _resize_cursor(corner):
        return (
            Qt.SizeBDiagCursor if corner in (0, 2)
            else Qt.SizeFDiagCursor
        )

    def _hover_cursor(self, position):
        corner = self._corner_at(position)
        if corner is not None:
            return self._resize_cursor(corner)
        if self._extent_at(position) is not None:
            return Qt.SizeAllCursor
        return Qt.OpenHandCursor

    def _start_pan(self, position):
        self._pan_point = position
        self._pan_origin = position
        self._pan_extent = QgsRectangle(self.canvas.extent())
        self._pan_units = self.canvas.mapUnitsPerPixel()
        self.canvas.setCursor(Qt.ClosedHandCursor)

    def _pan(self, position):
        delta = position - self._pan_origin
        if delta.isNull():
            return
        # Карта следует за курсором: тянем вправо — область уходит влево.
        dx = -delta.x() * self._pan_units
        dy = delta.y() * self._pan_units
        extent = self._pan_extent
        self.canvas.setExtent(
            QgsRectangle(
                extent.xMinimum() + dx,
                extent.yMinimum() + dy,
                extent.xMaximum() + dx,
                extent.yMaximum() + dy,
            )
        )
        self.canvas.refresh()

    def _stop_pan(self):
        if self._pan_point is None:
            return
        self._pan_point = None
        self._pan_origin = None
        self._pan_extent = None
        self._pan_units = None
        self.canvas.setCursor(Qt.OpenHandCursor)

    def _resize(self, position):
        current = self.toMapCoordinates(position)
        corners = self._corners(self._start_extent)
        anchor = corners[(self._corner + 2) % 4]
        rectangle = QgsRectangle(
            anchor.x(), anchor.y(), current.x(), current.y()
        )
        rectangle.normalize()
        return rectangle

    def _move(self, position):
        current = self.toMapCoordinates(position)
        start = self._press_point
        dx = current.x() - start.x()
        dy = current.y() - start.y()
        return QgsRectangle(
            self._start_extent.xMinimum() + dx,
            self._start_extent.yMinimum() + dy,
            self._start_extent.xMaximum() + dx,
            self._start_extent.yMaximum() + dy,
        )

    def canvasMoveEvent(self, event):
        if self._pan_point is not None:
            if event.buttons() & _right_button():
                self._pan(event.pos())
            else:
                self._stop_pan()
            return
        if self._operation is not None and self._start_extent is not None:
            if self._operation == "resize":
                self._extent = self._resize(event.pos())
            else:
                self._extent = self._move(event.pos())
            if self._show_extent is not None:
                self._show_extent(self._extent)
            if self._extent_changed is not None:
                self._extent_changed(QgsRectangle(self._extent))
            if self.handles is not None:
                self.handles.update()
            return
        if not (event.buttons() & _left_button()):
            self.canvas.setCursor(self._hover_cursor(event.pos()))
        super().canvasMoveEvent(event)

    def canvasReleaseEvent(self, event):
        if event.button() == _right_button() and self._pan_point is not None:
            self._stop_pan()
            return
        if self._operation is not None:
            rectangle = self._extent
            self._clear_operation()
            if rectangle is not None:
                if self._show_extent is not None:
                    self._show_extent(rectangle)
                if self._extent_finished is not None:
                    self._extent_finished(QgsRectangle(rectangle))
            self.canvas.setCursor(self._hover_cursor(event.pos()))
            self.refresh_handles()
            return
        super().canvasReleaseEvent(event)

    def _clear_operation(self):
        self._operation = None
        self._corner = None
        self._start_extent = None
        self._extent = None
        self._press_point = None

    def deactivate(self):
        self._clear_operation()
        self._stop_pan()
        if self.handles is not None:
            self.handles.hide()
        super().deactivate()

    def dispose(self):
        self._clear_operation()
        self._stop_pan()
        handles = self.handles
        self.handles = None
        if handles is None:
            return
        try:
            scene = handles.scene()
            if scene is not None:
                scene.removeItem(handles)
        except Exception:
            pass


_SEARCH_WORKERS = set()
_QUIT_HANDLER_INSTALLED = False


def shutdown_nominatim_workers():
    """Дождаться поисковых потоков до завершения QGIS."""
    for worker in list(_SEARCH_WORKERS):
        try:
            if worker.isRunning():
                if not worker.wait(25000):
                    worker.terminate()
                    worker.wait(2000)
        except RuntimeError:
            pass
    _SEARCH_WORKERS.clear()


def install_quit_handler():
    """Подключить ожидание поисковых потоков к завершению QGIS."""
    global _QUIT_HANDLER_INSTALLED
    if _QUIT_HANDLER_INSTALLED:
        return
    app = QgsApplication.instance()
    if app is None:
        return
    app.aboutToQuit.connect(shutdown_nominatim_workers)
    _QUIT_HANDLER_INSTALLED = True


class NominatimWorker(QThread):
    """Поиск мест по Nominatim в отдельном потоке."""

    result = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, query, parent=None):
        super().__init__(parent)
        self.query = query
        _SEARCH_WORKERS.add(self)
        self.finished.connect(self._forget)

    def _forget(self):
        _SEARCH_WORKERS.discard(self)

    def run(self):
        try:
            places = nominatim.search(self.query)
        except Exception as exc:  # noqa: BLE001 - сеть не должна ломать окно
            self.failed.emit(str(exc))
        else:
            self.result.emit(places)


class ExtentDialog(QDialog):
    def __init__(self, bbox_text="", parent=None, estimate_provider=None):
        super().__init__(parent)
        install_quit_handler()
        self.setWindowTitle("Выбор охвата карты")
        self._fitted_to_screen = False
        self._fit_to_screen()
        self._bbox = None
        self._drawn_extent = None
        self._frame_labels = ("", "")
        self._search_worker = None
        self._estimate_provider = estimate_provider
        self._estimate_generation = 0
        self._estimate_cancel = None
        self._estimate_pending = False
        self._estimate_blocked = False
        self._wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        self._mercator = QgsCoordinateReferenceSystem("EPSG:3857")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._places = {}
        self._pending_query = None
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._start_search)

        # Оценка объёма и времени пересчитывается после паузы: рамку
        # двигают мышью, и запускать расчёт на каждое движение нельзя.
        self._estimate_timer = QTimer(self)
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.setInterval(350)
        self._estimate_timer.timeout.connect(self._request_estimate)

        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(5000)
        self._notice_timer.timeout.connect(self._clear_notice)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск места")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setMaximumWidth(SEARCH_FIELD_WIDTH)
        self.search_input.setMinimumWidth(120)
        self.search_input.textChanged.connect(self._search_text_changed)
        self.search_input.returnPressed.connect(self._start_search)
        # Результаты показываются выпадающим списком под строкой поиска.
        self._search_model = QStandardItemModel(self)
        self._completer = QCompleter(self._search_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        # Nominatim отвечает на запрос «Париж» названием «Paris»: локальная
        # фильтрация выбросила бы все результаты, поэтому список показывается
        # целиком, как его вернул сервер.
        self._completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self._completer.setMaxVisibleItems(10)
        self._completer.activated[str].connect(self._place_activated)
        self._completer.popup().setMinimumWidth(SEARCH_POPUP_WIDTH)
        self.search_input.setCompleter(self._completer)

        self.basemap_combo = QComboBox()
        for basemap in BASEMAPS:
            self.basemap_combo.addItem(basemap.name, basemap.key)
        self.basemap_combo.setToolTip("Подложка карты")
        self.basemap_combo.currentIndexChanged.connect(self._basemap_changed)

        tools = QHBoxLayout()
        self.draw_button = QPushButton()
        self.draw_button.setCheckable(True)
        self.draw_button.setIcon(
            QgsApplication.getThemeIcon("mActionSelectRectangle.svg")
        )
        self.draw_button.setIconSize(QSize(22, 22))
        self.draw_button.setMinimumSize(46, 38)
        self.draw_button.setAccessibleName("Нарисовать охват")
        self.draw_button.setStyleSheet(DRAW_TOOL_STYLE)
        if self.draw_button.icon().isNull():
            self.draw_button.setText("Нарисовать охват")
        self.draw_button.setToolTip(
            "Нарисовать охват: левой кнопкой протяните прямоугольник, "
            "правой — перемещайте карту"
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
        # Поиск — в одной строке с инструментами, справа от координат.
        tools.addWidget(self.search_input)
        tools.addWidget(self.basemap_combo)
        layout.addLayout(tools)

        self.canvas = MeasuredCanvas(self)
        self.canvas.setCanvasColor(QColor("white"))
        self.canvas.enableAntiAliasing(True)
        self.canvas.setDestinationCrs(self._mercator)
        self.layer = None
        self._basemap_changed(0)
        self._draw_tool = RectangleMapTool(self.canvas)
        self._draw_tool.rectangleDrawn.connect(self._rectangle_drawn)
        self._draw_tool.rectangleChanged.connect(self._rectangle_changed)
        self._pan_tool = ExtentPanTool(
            self.canvas,
            lambda: self._drawn_extent,
            self._draw_tool.show_rectangle,
            self._rectangle_changed,
            self._rectangle_drawn,
        )
        self.canvas.setMapTool(self._pan_tool)
        initial, remembered = self._initial_extent(bbox_text)
        self.canvas.setExtent(
            self._padded_extent(initial) if remembered else initial
        )
        if remembered:
            self._drawn_extent = QgsRectangle(initial)
            self._draw_tool.show_rectangle(self._drawn_extent)
            self.reset_button.setEnabled(True)
            self._pan_tool.refresh_handles()
        self.canvas.extentsChanged.connect(self._update_coords)
        self.canvas.extentsChanged.connect(self._update_measurement)
        self.canvas.set_frame_provider(self._frame_provider)
        self.canvas.viewport().installEventFilter(self)
        layout.addWidget(self.canvas, 1)

        hint = QLabel(
            "Перетаскивайте карту мышью левой или правой кнопкой, колесом "
            "меняйте масштаб. Готовую рамку можно тянуть прямо за неё, а за "
            "угловые маркеры — менять размер. Кнопка с иконкой "
            "прямоугольника рисует новую рамку. Затем нажмите "
            "«Использовать охват»."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        self.notice = OneLineLabel("")
        self.notice.setStyleSheet("color:#b35c00; font-weight:bold;")
        # Уведомление короткое: даём ему естественную ширину, но без
        # роста окна на длинной строке.
        self.notice.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Preferred
        )
        self.notice.setMaximumWidth(260)
        bottom.addWidget(self.notice)
        self.estimate = OneLineLabel("")
        self.estimate.setMinimumWidth(0)
        bottom.addWidget(self.estimate, 1)
        self.use_button = QPushButton("Использовать охват")
        self.use_button.setDefault(True)
        self.use_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.reject)
        bottom.addWidget(self.use_button)
        bottom.addWidget(cancel_button)
        layout.addLayout(bottom)

        # Enter применяет охват и закрывает окно: канвас забирает фокус,
        # поэтому нужен явный shortcut.  Он живёт на канвасе, чтобы в
        # строке поиска Enter запускал поиск, а не закрывал окно.
        for key in (Qt.Key_Return, Qt.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.canvas)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(self.accept)

        # Esc отменяет выбор и закрывает окно: карта перехватывает его
        # для отмены рисования, поэтому shortcut задаётся явно.
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.setContext(Qt.WindowShortcut)
        escape.activated.connect(self.reject)

        self._update_coords()
        self._update_measurement()
        self._schedule_estimate()

    def keyPressEvent(self, event):
        # Enter в строке поиска запускает поиск, а не применяет охват:
        # строка отдаёт клавишу диалогу, иначе окно закрылось бы.
        if QApplication.focusWidget() is self.search_input:
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.reject()
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
        # Фокус у карты: Enter применяет охват, а в строке поиска
        # запускает поиск.
        self.canvas.setFocus()
        super().showEvent(event)

    def _toggle_draw(self, active):
        self.canvas.setMapTool(
            self._draw_tool if active else self._pan_tool
        )

    def _basemap_changed(self, _index):
        """Сменить подложку карты."""
        basemap = BASEMAP_BY_KEY.get(self.basemap_combo.currentData())
        if basemap is None:
            return
        layer = QgsRasterLayer(basemap.uri, basemap.name, "wms")
        self.layer = layer
        self.canvas.setLayers([layer] if layer.isValid() else [])
        self.canvas.set_attribution(basemap.attribution_text)

    def _search_text_changed(self, text):
        """Поиск по мере ввода: пауза, затем запрос к Nominatim."""
        query = text.strip()
        if len(query) < MIN_SEARCH_LENGTH:
            self._search_timer.stop()
            self._set_search_results([])
            return
        self._search_timer.start()

    def _start_search(self):
        """Запустить поиск места по Nominatim."""
        self._search_timer.stop()
        if self.sender() is self.search_input:
            # Enter при выбранном результате из выпадающего списка сам
            # переходит к месту: повторный запрос к Nominatim не нужен.
            if self._activate_index(self._completion_index()):
                return
        query = self.search_input.text().strip()
        if len(query) < MIN_SEARCH_LENGTH:
            return
        if self._search_worker is not None:
            # Предыдущий запрос ещё выполняется: его результат уже не
            # нужен, но Nominatim не любит параллельные обращения.
            self._pending_query = query
            return
        self._run_search(query)

    def _completion_index(self):
        """Текущий элемент выпадающего списка, если он указывает на место."""
        popup = self._completer.popup()
        if not popup.isVisible():
            return None
        index = popup.currentIndex()
        if not index.isValid() or index.data() not in self._places:
            return None
        return index

    def _activate_index(self, index):
        """Перейти к месту, выбранному в выпадающем списке."""
        name = index.data() if index is not None else None
        if name not in self._places:
            return False
        self._search_timer.stop()
        # QCompleter уже подставил название в строку; сигналы блокируем,
        # чтобы по полному названию не ушёл новый запрос.
        self.search_input.blockSignals(True)
        try:
            self.search_input.setText(name)
            self.search_input.setCursorPosition(len(name))
        finally:
            self.search_input.blockSignals(False)
        self._place_activated(name)
        return True

    def _run_search(self, query):
        self._pending_query = None
        # Старые результаты к новому запросу не относятся. Чистим их здесь,
        # а не в textChanged: QCompleter сначала подставляет выбранное
        # название в строку и только потом шлёт сигнал активации, а очистка
        # модели на этом шаге его обрывала.
        self._set_search_results([])
        # Поток — не ребёнок окна: окно может закрыться во время запроса,
        # и тогда поток должен спокойно доработать и удалиться сам.
        worker = NominatimWorker(query, QgsApplication.instance())
        worker.result.connect(self._search_done)
        worker.failed.connect(self._search_failed)
        worker.finished.connect(self._search_finished)
        worker.finished.connect(worker.deleteLater)
        self._search_worker = worker
        worker.start()

    def _search_done(self, places):
        """Показать результаты выпадающим списком под строкой поиска."""
        self.search_input.setToolTip("")
        if not places:
            self._show_search_message(i18n.tr("Ничего не найдено"))
            return
        self._set_search_results(places)
        if self.search_input.hasFocus():
            self._completer.complete()

    def _search_failed(self, message):
        self._set_search_results([])
        # Ошибку не показываем отдельной строкой: она не должна занимать
        # место в панели инструментов.
        self.search_input.setToolTip(i18n.trf("Ошибка поиска: {}", message))

    def _search_finished(self):
        """Поиск закончен — можно выполнить отложенный запрос."""
        self._search_worker = None
        pending = self._pending_query
        if pending is None:
            return
        query = self.search_input.text().strip()
        if query and query != pending:
            pending = query
        if len(pending) >= MIN_SEARCH_LENGTH:
            self._run_search(pending)

    def _set_search_results(self, places):
        # Nominatim может вернуть несколько объектов с одинаковым
        # отображаемым названием: в списке они выглядели бы повторами.
        self._places = {}
        for place in places:
            self._places.setdefault(place.name, place)
        self._search_model.clear()
        for name in self._places:
            item = QStandardItem(name)
            item.setEditable(False)
            self._search_model.appendRow(item)
        if not self._places:
            self._completer.popup().hide()

    def _show_search_message(self, text):
        """Показать в выпадающем списке сообщение вместо результатов."""
        self._places = {}
        self._search_model.clear()
        item = QStandardItem(text)
        item.setEditable(False)
        # Сообщение нельзя выбрать: иначе оно подставилось бы в строку.
        item.setEnabled(False)
        item.setSelectable(False)
        self._search_model.appendRow(item)
        if self.search_input.hasFocus():
            self._completer.complete()

    def _place_activated(self, text):
        """Выбран результат поиска — переходим к найденному месту."""
        # Название подставлено в строку поиска: повторный запрос не нужен.
        self._search_timer.stop()
        place = self._places.get(text)
        if place is None:
            return
        self._show_place(place)

    def _show_place(self, place):
        """Перейти к найденному месту: центрировать и приблизить."""
        extent = self._place_extent(place)
        if extent is None:
            return
        # Рамка, нарисованная раньше, больше не задаёт охват: иначе
        # переход к месту ничего бы не менял.
        self._reset_drawn()
        self.canvas.setExtent(extent)
        self.canvas.refresh()
        self._update_coords()
        self._update_measurement()

    def _place_extent(self, place):
        """Прямоугольник найденного места в координатах холста."""
        bounds = place.bounds()
        if bounds is None:
            west = east = place.lon
            south = north = place.lat
        else:
            west, south, east, north = bounds
        transform = QgsCoordinateTransform(
            self._wgs84, self._mercator, QgsProject.instance()
        )
        extent = transform.transformBoundingBox(
            QgsRectangle(west, south, east, north)
        )
        extent.normalize()
        if extent.width() <= 0 or extent.height() <= 0:
            # Точка без границ: небольшое окно вокруг неё.
            center = transform.transform(QgsPointXY(place.lon, place.lat))
            half = PLACE_SPAN * MERCATOR_EXTENT / 180.0
            extent = QgsRectangle(
                center.x() - half, center.y() - half,
                center.x() + half, center.y() + half,
            )
        return self._padded_extent(extent)

    def _rectangle_drawn(self, rectangle):
        if rectangle is None or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._drawn_extent = QgsRectangle(rectangle)
        self._drawn_extent.normalize()
        self._clear_notice()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(True)
        self._pan_tool.refresh_handles()
        self._update_coords()
        self._update_measurement()
        self._schedule_estimate()

    def _rectangle_changed(self, rectangle):
        """Рамка меняется прямо во время рисования — обновляем подписи."""
        if rectangle is None or rectangle.width() <= 0 or rectangle.height() <= 0:
            return
        self._drawn_extent = QgsRectangle(rectangle)
        self._drawn_extent.normalize()
        self._clear_notice()
        self._pan_tool.refresh_handles()
        self._update_coords()
        self._update_measurement()
        self._clear_estimate()

    def _reset_drawn(self):
        self._drawn_extent = None
        self._draw_tool.clear()
        self.draw_button.setChecked(False)
        self.reset_button.setEnabled(False)
        self._pan_tool.refresh_handles()
        self._update_coords()
        self._update_measurement()
        self._clear_estimate()

    def _show_notice(self, text):
        """Немодальное уведомление в нижней строке."""
        self.notice.setText(text)
        self._notice_timer.start()

    def _clear_notice(self):
        self._notice_timer.stop()
        self.notice.setText("")

    def _clear_estimate(self):
        self._estimate_timer.stop()
        self._stop_estimate()
        self.estimate.setText("")
        self.estimate.setStyleSheet("")
        self._estimate_pending = False
        self._estimate_blocked = False
        self._update_use_button()

    def _schedule_estimate(self):
        if self._estimate_provider is None or self._drawn_extent is None:
            self._clear_estimate()
            return
        self._clear_estimate()
        # Пока оценка не готова, охват не применяем: превышение предела
        # SliceOSM или часовой расчёт могли бы остаться незамеченными.
        self._estimate_pending = True
        self._update_use_button()
        self._estimate_timer.start()

    def _update_use_button(self):
        blocked = self._estimate_pending or self._estimate_blocked
        self.use_button.setEnabled(not blocked)
        if self._estimate_blocked:
            self.use_button.setToolTip(
                i18n.tr(
                    "Охват превышает предел SliceOSM или расчётное время "
                    "больше 1 часа"
                )
            )
        else:
            self.use_button.setToolTip("")

    def _request_estimate(self):
        """Запросить оценку у панели для выбранной рамки."""
        self._estimate_timer.stop()
        self._stop_estimate()
        if self._estimate_provider is None or self._drawn_extent is None:
            self._clear_estimate()
            return
        try:
            bbox = self._extent_as_bbox()
        except Exception:
            self._clear_estimate()
            return
        self._estimate_generation += 1
        generation = self._estimate_generation
        self._estimate_pending = True
        self._update_use_button()
        self.estimate.setStyleSheet("")
        self.estimate.setText(i18n.tr("Оценка…"))

        def ready(text, critical=False, tooltip=""):
            self._estimate_done(generation, text, critical, tooltip)

        try:
            cancel = self._estimate_provider(bbox, ready)
        except Exception:
            self._clear_estimate()
            return
        if callable(cancel):
            self._estimate_cancel = cancel

    def _estimate_done(self, generation, text, critical=False, tooltip=""):
        if generation != self._estimate_generation:
            return
        self._estimate_pending = False
        # critical означает, что охват выбирать нельзя: предел SliceOSM
        # превышен или расчётное время больше часа.
        self._estimate_blocked = bool(critical)
        self._update_use_button()
        self.estimate.setText(text or "")
        if tooltip:
            self.estimate.setToolTip(tooltip)
        self.estimate.setStyleSheet(
            "color:#e74c3c; font-weight:bold;" if critical else ""
        )

    def _stop_estimate(self):
        self._estimate_generation += 1
        cancel = self._estimate_cancel
        self._estimate_cancel = None
        if cancel is None:
            return
        try:
            cancel()
        except Exception:
            pass

    @staticmethod
    def _padded_extent(extent):
        """Отступ от краёв, чтобы границы прямоугольника не шли по краям."""
        margin = max(extent.width(), extent.height()) * 0.12
        padded = QgsRectangle(extent)
        padded.grow(margin)
        return padded

    def _initial_extent(self, bbox_text):
        """Прямоугольник карты и признак того, что охват был задан."""
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
        source = self._frame_rectangle()
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

    def _frame_rectangle(self):
        """Рамка охвата в координатах холста: нарисованная или текущая."""
        if self._drawn_extent is not None:
            return self._drawn_extent
        return self.canvas.extent()

    def _update_measurement(self, *_args):
        """Подписи размеров сторон нарисованной рамки.

        Без рамки подписей нет: видимый охват карты — это не выбранный
        охват, и при перетаскивании его размеры не должны бегать по краям.
        """
        if self._drawn_extent is None:
            self._frame_labels = ("", "")
            self.canvas.update()
            return
        try:
            bbox = self._extent_as_bbox()
        except Exception:
            self._frame_labels = ("", "")
            self.canvas.update()
            return
        width_km, height_km = frame_sizes_km(bbox)
        self._frame_labels = (
            format_length(width_km),
            format_length(height_km),
        )
        self.canvas.update()

    def _frame_provider(self):
        """Рамка и подписи для отрисовки.

        Вызывается при каждой отрисовке холста: подписи размеров сторон
        идут за нарисованной рамкой, даже когда карта перемещается, а сама
        рамка в координатах не меняется. Без рамки подписей нет.
        """
        if self._drawn_extent is None:
            return None, "", ""
        labels = getattr(self, "_frame_labels", ("", ""))
        return self._drawn_extent, labels[0], labels[1]

    def accept(self):
        if self._drawn_extent is None:
            # Без выбранной рамки текущий вид карты — ещё не охват.
            self._show_notice(i18n.tr("Выберите охват на карте"))
            if not self.draw_button.isChecked():
                self.draw_button.setChecked(True)
            self.canvas.setMapTool(self._draw_tool)
            self.canvas.setFocus()
            return
        if self._estimate_pending:
            self._show_notice(i18n.tr("Дождитесь оценки охвата"))
            return
        if self._estimate_blocked:
            self._show_notice(
                i18n.tr(
                    "Охват слишком велик: уменьшите его или выберите "
                    "готовые карты"
                )
            )
            return
        try:
            self._bbox = self._extent_as_bbox()
        except Exception as exc:
            QMessageBox.warning(
                self, "QGIS-xtopo",
                i18n.trf("Не удалось определить охват: {}", exc),
            )
            return
        super().accept()

    def done(self, result):
        self._estimate_timer.stop()
        self._stop_estimate()
        self._notice_timer.stop()
        self._draw_tool.clear()
        self._pan_tool.dispose()
        self.canvas.stopRendering()
        self.canvas.setLayers([])
        # Поиск и подсказки больше не нужны: окно закрывается, а модель
        # поиска иначе остаётся висеть до конца сессии.
        self._search_timer.stop()
        self._search_model.clear()
        self._places.clear()
        super().done(result)
