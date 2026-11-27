"""Шкала масштаба для макета атласа.

Повторяет шкалу из окна выбора охвата: четыре чередующихся чёрно-белых
отрезка и подпись «круглого» расстояния над ними.  Длина полосы
подбирается под ширину листа, поэтому на граничных масштабах подпись не
превращается в нечитаемое число.
"""

from qgis.core import (
    QgsLayoutItem,
    QgsLayoutMeasurement,
    Qgis,
)
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPainterPath, QPen

from ..core import map_units
from ..core.measure import ground_width, scale_bar

SCALE_BAR_MAX_MM = 45.0
BAR_HEIGHT_MM = 1.5
TEXT_POINT_SIZE = 7.0
LINE_WIDTH_MM = 0.15

# Положение и размер на случай, когда штатная шкала уже удалена из макета
# (например, проект сохранён после первого предпросмотра).
DEFAULT_X_MM = 5.0
DEFAULT_BOTTOM_MM = 5.589
DEFAULT_WIDTH_MM = 56.2955
DEFAULT_HEIGHT_MM = 11.9
DEFAULT_Z_VALUE = 2.0


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


class LayoutScaleBarItem(QgsLayoutItem):
    """Шкала масштаба, привязанная к карте макета."""

    def __init__(self, layout, map_item):
        super().__init__(layout)
        self.map_item = map_item
        # QgsLayoutItem по умолчанию заливает прямоугольник белым.
        self.setBackgroundEnabled(False)
        self.setFrameEnabled(False)

    def _meters_per_mm(self):
        """Метров на миллиметр листа для текущего охвата карты."""
        layout = self.layout()
        if layout is None or self.map_item is None:
            return 0.0
        units_per_mm = layout.convertToLayoutUnits(
            QgsLayoutMeasurement(1.0, Qgis.LayoutUnit.Millimeters)
        )
        if units_per_mm <= 0:
            return 0.0
        map_width_mm = self.map_item.rect().width() / units_per_mm
        if map_width_mm <= 0:
            return 0.0
        width_m = ground_width(self.map_item.extent(), self.map_item.crs())
        return width_m / map_width_mm

    def draw(self, context):
        painter = context.renderContext().painter()
        if painter is None or self.map_item is None:
            return
        meters_per_mm = self._meters_per_mm()
        if meters_per_mm <= 0:
            return
        length_mm, text = scale_bar(
            meters_per_mm, SCALE_BAR_MAX_MM,
            map_units.display_units(),
        )
        if not length_mm:
            return

        millimeters = context.renderContext().convertToPainterUnits(
            1.0, Qgis.RenderUnit.Millimeters
        )
        length = length_mm * millimeters
        height = self.rect().height() * millimeters
        bar_height = BAR_HEIGHT_MM * millimeters
        bottom = height - millimeters
        top = bottom - bar_height
        pen_width = max(1.0, LINE_WIDTH_MM * millimeters)

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            segment = length / 4.0
            for index in range(4):
                painter.setPen(QPen(QColor(25, 25, 25), pen_width))
                painter.setBrush(
                    QColor(25, 25, 25)
                    if index % 2 == 0
                    else QColor(255, 255, 255)
                )
                painter.drawRect(
                    QRectF(index * segment, top, segment, bar_height)
                )
            font = painter.font()
            font.setPointSizeF(TEXT_POINT_SIZE)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(text)
            baseline = top - 2.0 - metrics.descent()
            path = QPainterPath()
            path.addText(
                (length - text_width) / 2.0, baseline, font, text
            )
            halo = QPen(QColor(255, 255, 255), max(1.0, 0.3 * millimeters))
            halo.setJoinStyle(_enum(Qt, "PenJoinStyle", "RoundJoin"))
            painter.setPen(halo)
            painter.setBrush(_enum(Qt, "BrushStyle", "NoBrush"))
            painter.drawPath(path)
            painter.setPen(_enum(Qt, "PenStyle", "NoPen"))
            painter.setBrush(QColor(25, 25, 25))
            painter.drawPath(path)
        finally:
            painter.restore()
