"""Измерение размеров на местности.

Общие helpers для подписей размеров: окно выбора охвата и разметка
страниц атласа показывают длины сторон одинаково.
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsPointXY,
    QgsProject,
)


def distance_area(crs=None):
    """Расстояния по эллипсоиду WGS84."""
    area = QgsDistanceArea()
    area.setSourceCrs(
        crs or QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsProject.instance().transformContext(),
    )
    area.setEllipsoid("WGS84")
    return area


def frame_sizes_km(bbox):
    """Ширина и высота рамки в километрах.

    Расстояния считаются по эллипсоиду WGS84, поэтому подписи совпадают
    с реальными расстояниями, а не с разницей координат.
    """
    distance = distance_area()
    lon_mid = (bbox.lon_min + bbox.lon_max) / 2
    lat_mid = (bbox.lat_min + bbox.lat_max) / 2
    width = distance.measureLine(
        QgsPointXY(bbox.lon_min, lat_mid), QgsPointXY(bbox.lon_max, lat_mid)
    )
    height = distance.measureLine(
        QgsPointXY(lon_mid, bbox.lat_min), QgsPointXY(lon_mid, bbox.lat_max)
    )
    return width / 1000.0, height / 1000.0


def format_length(km):
    """Подпись длины в километрах."""
    from . import i18n

    value = max(0.0, float(km))
    if value < 10:
        text = f"{value:.2f}"
    elif value < 100:
        text = f"{value:.1f}"
    else:
        text = f"{value:.0f}"
    return f"{text} {i18n.tr('км')}"
