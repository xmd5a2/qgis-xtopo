"""Координатная сетка для экспорта макета.

Сетка строится в градусах WGS84 поверх карты в её СК (обычно
EPSG:3857).  Интервал выбирается из ряда «круглых» значений так, чтобы
на альбомном листе получилось 5–6 столбцов и 3–4 строки, а на портретном
количество столбцов и строк меняется местами.  Подписи — градусы, минуты
и секунды с латинской стороной света перед координатами; они стоят
только снизу и справа и идут вдоль линий сетки.
"""

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsLayoutItem,
    QgsPointXY,
    QgsProject,
    Qgis,
)
from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)

# Ряд допустимых интервалов в градусах, от крупного к мелкому.  Все они
# кратны целым секундам, поэтому линии ложатся на круглые координаты.
GRID_STEPS = (
    60.0,
    30.0,
    20.0,
    10.0,
    5.0,
    2.0,
    1.0,
    30.0 / 60.0,
    20.0 / 60.0,
    15.0 / 60.0,
    10.0 / 60.0,
    5.0 / 60.0,
    2.0 / 60.0,
    1.0 / 60.0,
    30.0 / 3600.0,
    20.0 / 3600.0,
    15.0 / 3600.0,
    10.0 / 3600.0,
    5.0 / 3600.0,
    2.0 / 3600.0,
    1.0 / 3600.0,
)

LANDSCAPE_COLUMNS = (5, 6)
LANDSCAPE_ROWS = (3, 4)
PORTRAIT_COLUMNS = LANDSCAPE_ROWS
PORTRAIT_ROWS = LANDSCAPE_COLUMNS

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

GRID_COLOR = QColor(85, 85, 85)
TEXT_COLOR = QColor(0, 0, 0)
LINE_WIDTH_MM = 0.1
FONT_POINT_SIZE = 6.0
EDGE_MARGIN_MM = 1.2
CORNER_GAP_MM = 3.0
SAMPLES = 24
EPSILON = 1e-9


def choose_grid_step(span, target):
    """Подбирает круглый шаг сетки под число столбцов или строк.

    ``span`` — размер охвата в градусах, ``target`` — желаемое число
    столбцов (строк).  Сначала берётся шаг, дающий попадание в диапазон,
    затем — ближайший к нему; при равенстве предпочитается более крупный
    шаг, чтобы сетка не была гуще нужного.
    """
    if span <= 0:
        return None
    ideal = (target[0] + target[1]) / 2.0
    best = None
    for step in GRID_STEPS:
        cells = span / step
        if target[0] <= cells <= target[1]:
            score = abs(cells - ideal)
        else:
            score = 100.0 + min(
                abs(cells - target[0]), abs(cells - target[1])
            )
        if best is None or score < best[0] - EPSILON:
            best = (score, step)
    return best[1] if best is not None else None


def format_coordinate(value, longitude):
    """Градусы, минуты и секунды с латинской стороной света впереди."""
    hemisphere = ("E" if value >= 0 else "W") if longitude else (
        "N" if value >= 0 else "S"
    )
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60.0
    minutes = int(minutes_float)
    seconds = int(round((minutes_float - minutes) * 60.0))
    if seconds >= 60:
        seconds -= 60
        minutes += 1
    if minutes >= 60:
        minutes -= 60
        degrees += 1
    return f"{hemisphere}{degrees:02d}°{minutes:02d}′{seconds:02d}″"


def _line_values(low, high, step):
    if step is None or step <= 0 or high < low:
        return []
    value = math.floor(low / step) * step
    while value < low - EPSILON:
        value += step
    values = []
    while value <= high + EPSILON:
        values.append(value)
        value += step
    return values


def _geographic_bounds(extent, source_crs):
    if not source_crs.isValid() or extent.isEmpty():
        return None
    try:
        transform = QgsCoordinateTransform(
            source_crs, WGS84, QgsProject.instance()
        )
        bounds = transform.transformBoundingBox(extent)
    except Exception:
        return None
    if bounds.isEmpty():
        return None
    return (
        bounds.xMinimum(),
        bounds.xMaximum(),
        bounds.yMinimum(),
        bounds.yMaximum(),
    )


class LayoutCoordinateGridItem(QgsLayoutItem):
    """Сетка и подписи поверх карты макета.

    Элемент рисуется при каждом рендере страницы, поэтому у листов атласа
    с разным охватом шаг сетки подбирается отдельно.
    """

    def __init__(self, layout, map_item):
        super().__init__(layout)
        self.map_item = map_item
        self.setBackgroundEnabled(False)
        self.setFrameEnabled(False)
        self.setReferencePoint(QgsLayoutItem.UpperLeft)

    def _to_item(self, transform, longitude, latitude):
        point = transform.transform(QgsPointXY(longitude, latitude))
        return self.map_item.mapToItemCoords(
            QPointF(point.x(), point.y())
        )

    def draw(self, context):
        if self.map_item is None:
            return
        painter = context.renderContext().painter()
        if painter is None:
            return
        extent = self.map_item.extent()
        bounds = _geographic_bounds(extent, self.map_item.crs())
        if bounds is None:
            return
        lon_min, lon_max, lat_min, lat_max = bounds
        if lon_max <= lon_min or lat_max <= lat_min:
            return

        landscape = self.rect().width() >= self.rect().height()
        step_lon = choose_grid_step(
            lon_max - lon_min,
            LANDSCAPE_COLUMNS if landscape else PORTRAIT_COLUMNS,
        )
        step_lat = choose_grid_step(
            lat_max - lat_min,
            LANDSCAPE_ROWS if landscape else PORTRAIT_ROWS,
        )
        if step_lon is None or step_lat is None:
            return

        try:
            transform = QgsCoordinateTransform(
                WGS84, self.map_item.crs(), QgsProject.instance()
            )
        except Exception:
            return

        per_mm = context.renderContext().convertToPainterUnits(
            1.0, Qgis.RenderUnit.Millimeters
        )
        if per_mm <= 0:
            return
        width = self.rect().width() * per_mm
        height = self.rect().height() * per_mm
        margin = EDGE_MARGIN_MM * per_mm
        corner_gap = CORNER_GAP_MM * per_mm

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setClipRect(QRectF(0.0, 0.0, width, height))
            pen = QPen(GRID_COLOR, LINE_WIDTH_MM * per_mm)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            lon_lines = self._longitude_lines(transform, bounds, step_lon)
            lat_lines = self._latitude_lines(transform, bounds, step_lat)
            for _longitude, points in lon_lines:
                self._draw_line(painter, points, per_mm)
            for _latitude, points in lat_lines:
                self._draw_line(painter, points, per_mm)

            # QPainterPath считает кегль в пикселях при 96 dpi, поэтому
            # для нужного размера задаём пиксельный кегль по dpi листа.
            font = QFont()
            font.setPixelSize(max(
                1, round(FONT_POINT_SIZE * per_mm * 25.4 / 72.0)
            ))
            for longitude, points in lon_lines:
                if not points:
                    continue
                anchor = points[-1]
                if (
                    anchor.x() * per_mm < corner_gap
                    or anchor.x() * per_mm > width - corner_gap
                ):
                    continue
                self._draw_vertical_label(
                    painter, font, format_coordinate(longitude, True),
                    anchor.x() * per_mm, height - margin, per_mm,
                )
            for latitude, points in lat_lines:
                if not points:
                    continue
                anchor = points[-1]
                if (
                    anchor.y() * per_mm < 2.0 * per_mm
                    or anchor.y() * per_mm > height - 2.0 * per_mm
                ):
                    continue
                self._draw_horizontal_label(
                    painter, font, format_coordinate(latitude, False),
                    width - margin, anchor.y() * per_mm, per_mm,
                )
        finally:
            painter.restore()

    def _longitude_lines(self, transform, bounds, step):
        lon_min, lon_max, lat_min, lat_max = bounds
        lines = []
        for longitude in _line_values(lon_min, lon_max, step):
            points = []
            for index in range(SAMPLES + 1):
                latitude = lat_max - (lat_max - lat_min) * index / SAMPLES
                try:
                    points.append(
                        self._to_item(transform, longitude, latitude)
                    )
                except Exception:
                    points = []
                    break
            if points:
                lines.append((longitude, points))
        return lines

    def _latitude_lines(self, transform, bounds, step):
        lon_min, lon_max, lat_min, lat_max = bounds
        lines = []
        for latitude in _line_values(lat_min, lat_max, step):
            points = []
            for index in range(SAMPLES + 1):
                longitude = lon_min + (lon_max - lon_min) * index / SAMPLES
                try:
                    points.append(
                        self._to_item(transform, longitude, latitude)
                    )
                except Exception:
                    points = []
                    break
            if points:
                lines.append((latitude, points))
        return lines

    @staticmethod
    def _draw_line(painter, points, per_mm):
        polygon = QPolygonF(
            [QPointF(point.x() * per_mm, point.y() * per_mm)
             for point in points]
        )
        if polygon.count() >= 2:
            painter.drawPolyline(polygon)

    @staticmethod
    def _draw_vertical_label(painter, font, text, x, bottom, per_mm):
        """Подпись вдоль меридиана: читается снизу вверх, внутри листа."""
        path = QPainterPath()
        path.addText(0.0, 0.0, font, text)
        rect = path.boundingRect()
        path.translate(
            EDGE_MARGIN_MM * per_mm - rect.left(),
            -(rect.top() + rect.bottom()) / 2.0,
        )
        transform = QTransform()
        transform.translate(x, bottom)
        transform.rotate(-90.0)
        painter.fillPath(transform.map(path), TEXT_COLOR)

    @staticmethod
    def _draw_horizontal_label(painter, font, text, right, center_y, per_mm):
        """Подпись вдоль параллели: прижата к правому краю, внутри листа."""
        path = QPainterPath()
        path.addText(0.0, 0.0, font, text)
        rect = path.boundingRect()
        path.translate(
            right - rect.right(),
            center_y - (rect.top() + rect.bottom()) / 2.0,
        )
        painter.fillPath(path, TEXT_COLOR)
