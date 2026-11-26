"""Шкала масштаба поверх холста карты.

Повторяет шкалу, которая печатается в макете: четыре чередующихся
чёрно-белых отрезка и подпись «круглого» расстояния над ними.  Виджет
живёт в левом нижнем углу холста, остаётся на месте при перетаскивании
карты и пересчитывается при каждом изменении охвата.
"""

import math

from qgis.PyQt.QtCore import QEvent, QRect, QRectF, Qt
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)
from qgis.PyQt.QtWidgets import QWidget

from ..core.measure import ground_width, scale_bar

MARGIN = 10
MAX_LENGTH = 140.0
# Те же пропорции, что у шкалы макета (gui/layoutscalebar.py).
BAR_HEIGHT_MM = 1.5
LINE_WIDTH_MM = 0.15
TEXT_POINT_SIZE = 7.0
BAR_COLOR = QColor(25, 25, 25)


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


class CanvasScaleBar(QWidget):
    """Шкала масштаба в левом нижнем углу ``QgsMapCanvas``."""

    def __init__(self, canvas, parent=None):
        viewport = canvas.viewport()
        super().__init__(parent if parent is not None else viewport)
        self.canvas = canvas
        self._viewport = viewport
        self.setObjectName("XtopoCanvasScaleBar")
        for name in (
            "WA_TransparentForMouseEvents",
            "WA_NoSystemBackground",
            "WA_TranslucentBackground",
        ):
            self.setAttribute(_enum(Qt, "WidgetAttribute", name), True)
        self.setAutoFillBackground(False)
        self._font = QFont(self.font())
        self._font.setPointSizeF(TEXT_POINT_SIZE)
        dpi = float(self.logicalDpiY() or 96)
        self._millimeters = dpi / 25.4
        self._bar_height = max(3.0, BAR_HEIGHT_MM * self._millimeters)
        self._pen_width = max(1.0, LINE_WIDTH_MM * self._millimeters)
        metrics = QFontMetricsF(self._font)
        self._height = int(
            math.ceil(metrics.height() + self._bar_height) + 4
        )
        canvas.extentsChanged.connect(self._sync)
        canvas.destinationCrsChanged.connect(self._sync)
        canvas.mapCanvasRefreshed.connect(self._sync)
        viewport.installEventFilter(self)
        self._sync()
        self.show()

    def eventFilter(self, obj, event):
        if obj is self._viewport and event.type() in (
            _enum(QEvent, "Type", "Resize"),
            _enum(QEvent, "Type", "Show"),
        ):
            self._sync()
        return False

    def _sync(self, *_args):
        try:
            viewport_height = self._viewport.height()
        except RuntimeError:
            return
        width = int(math.ceil(MAX_LENGTH)) + 4
        position = QRect(
            MARGIN,
            max(0, viewport_height - MARGIN - self._height),
            width,
            self._height,
        )
        if position != self.geometry():
            self.setGeometry(position)
        self.update()

    def meters_per_pixel(self):
        """Метров на местности на пиксель холста."""
        try:
            viewport_width = self._viewport.width()
            extent = self.canvas.extent()
            crs = self.canvas.mapSettings().destinationCrs()
        except RuntimeError:
            return 0.0
        if viewport_width <= 0:
            return 0.0
        width = ground_width(extent, crs)
        if width <= 0:
            return 0.0
        return width / viewport_width

    def current_scale(self):
        """Длина полосы в пикселях и подпись для текущего охвата."""
        return scale_bar(self.meters_per_pixel(), MAX_LENGTH)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(
                _enum(QPainter, "RenderHint", "Antialiasing"), True
            )
            length, text = self.current_scale()
            if not length:
                return
            top = self.height() - self._bar_height
            segment = length / 4.0
            for index in range(4):
                painter.setPen(QPen(BAR_COLOR, self._pen_width))
                painter.setBrush(
                    BAR_COLOR if index % 2 == 0 else QColor(255, 255, 255)
                )
                painter.drawRect(
                    QRectF(
                        index * segment, top, segment, self._bar_height
                    )
                )
            painter.setFont(self._font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(text)
            baseline = top - 2.0 - metrics.descent()
            path = QPainterPath()
            path.addText(
                (length - text_width) / 2.0, baseline, self._font, text
            )
            halo = QPen(
                QColor(255, 255, 255),
                max(2.0, 0.5 * self._millimeters),
            )
            halo.setJoinStyle(_enum(Qt, "PenJoinStyle", "RoundJoin"))
            painter.setPen(halo)
            painter.setBrush(_enum(Qt, "BrushStyle", "NoBrush"))
            painter.drawPath(path)
            painter.setPen(_enum(Qt, "PenStyle", "NoPen"))
            painter.setBrush(BAR_COLOR)
            painter.drawPath(path)
        finally:
            painter.end()
