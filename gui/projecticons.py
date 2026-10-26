from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (
    QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap,
)


def _enum(owner, group, name):
    try:
        return getattr(getattr(owner, group), name)
    except AttributeError:
        return getattr(owner, name)


def _pen(color, width=1.6):
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(_enum(Qt, "PenCapStyle", "RoundCap"))
    pen.setJoinStyle(_enum(Qt, "PenJoinStyle", "RoundJoin"))
    return pen


def _fill(color):
    return QColor(color.red(), color.green(), color.blue(), 55)


def _shadow(color):
    return QColor(color.red(), color.green(), color.blue(), 110)


def _mountain_path():
    path = QPainterPath(QPointF(2, 20))
    path.lineTo(11, 4)
    path.lineTo(22, 20)
    path.closeSubpath()
    return path


def _paint(kind, painter, color, accent):
    painter.setRenderHint(_enum(QPainter, "RenderHint", "Antialiasing"), True)
    painter.setBrush(Qt.NoBrush if hasattr(Qt, "NoBrush") else Qt.BrushStyle.NoBrush)
    painter.setPen(_pen(color, 1.5))

    if kind == "isolines":
        for dy in (0, 4, 8, 12):
            path = QPainterPath(QPointF(2, 20 - dy))
            path.cubicTo(
                QPointF(6, 17 - dy),
                QPointF(10, 19 - dy),
                QPointF(14, 16 - dy),
            )
            path.cubicTo(
                QPointF(17, 14 - dy),
                QPointF(20, 16 - dy),
                QPointF(22, 13 - dy),
            )
            painter.drawPath(path)
        return

    if kind == "steep":
        path = QPainterPath(QPointF(2, 20))
        path.lineTo(9, 5)
        path.lineTo(13, 12)
        path.lineTo(16, 8)
        path.lineTo(22, 20)
        painter.drawPath(path)
        painter.setPen(_pen(QColor(color.red(), color.green(), color.blue(), 165), 1.1))
        for x1, y1, x2, y2 in (
            (8, 6, 3, 19),
            (9, 8, 5, 19),
            (11, 15, 8, 19),
            (13, 12, 10, 19),
            (16, 8, 12, 19),
            (17, 11, 14, 19),
            (19, 15, 17, 19),
        ):
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        return

    if kind == "slight":
        path = QPainterPath(QPointF(2, 18))
        path.quadTo(7, 12, 12, 18)
        path.quadTo(17, 12, 22, 18)
        painter.drawPath(path)
        path = QPainterPath(QPointF(2, 12))
        path.quadTo(7, 7, 12, 12)
        path.quadTo(17, 7, 22, 12)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen if hasattr(Qt, "NoPen") else Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        for x, y in ((5, 9.5), (10, 7.5), (16, 9.5), (20, 7.5)):
            painter.drawEllipse(QPointF(x, y), 0.9, 0.9)
        return

    if kind in ("hillshade1", "hillshade2"):
        path = _mountain_path()
        painter.setPen(_pen(color, 1.5))
        painter.setBrush(_fill(color))
        painter.drawPath(path)
        shadow = QPainterPath(QPointF(11, 4))
        if kind == "hillshade1":
            shadow.lineTo(22, 20)
            shadow.lineTo(11, 20)
        else:
            shadow.lineTo(2, 20)
            shadow.lineTo(11, 20)
        shadow.closeSubpath()
        painter.setPen(_pen(QColor(color.red(), color.green(), color.blue(), 0), 0))
        painter.setBrush(_shadow(color))
        painter.drawPath(shadow)
        painter.setPen(Qt.NoPen if hasattr(Qt, "NoPen") else Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        sun_x = 4 if kind == "hillshade1" else 16
        painter.drawEllipse(QRectF(sun_x, 3, 4, 4))
        return

    if kind == "labels":
        path = QPainterPath(QPointF(1, 20))
        path.lineTo(7, 8)
        path.lineTo(13, 20)
        painter.drawPath(path)
        painter.setBrush(_fill(color))
        painter.drawRect(QRectF(13, 3, 9, 8))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(8)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRectF(13, 3, 9, 8), _enum(Qt, "AlignmentFlag", "AlignCenter"), "A")


def terrain_icon(kind, palette):
    color = palette.color(_enum(QPalette, "ColorRole", "WindowText"))
    accent = palette.color(_enum(QPalette, "ColorRole", "Highlight"))
    icon = QIcon()
    for size in (24, 48):
        pixmap = QPixmap(size, size)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.scale(size / 24.0, size / 24.0)
        _paint(kind, painter, color, accent)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
