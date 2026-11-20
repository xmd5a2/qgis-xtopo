"""Измерение размеров на местности.

Общие helpers для подписей размеров: окно выбора охвата и разметка
страниц атласа показывают длины сторон одинаково.
"""

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsPointXY,
    QgsProject,
)


def scale_bar(meters_per_pixel, max_length=140.0):
    """Длина полосы шкалы в пикселях и её подпись.

    Длина подбирается так, чтобы соответствовать «круглому»
    расстоянию: 1, 2 или 5 × 10ⁿ метров.
    """
    if not meters_per_pixel or meters_per_pixel <= 0:
        return 0.0, ""
    target = meters_per_pixel * max_length
    exponent = math.floor(math.log10(target)) if target > 0 else 0
    best = 0.0
    for factor in (1.0, 2.0, 5.0):
        value = factor * (10 ** exponent)
        if value <= target and value > best:
            best = value
    if best <= 0:
        best = target
    length = best / meters_per_pixel
    if length < 10 or length > max_length:
        return 0.0, ""
    if best >= 1000:
        return length, f"{best / 1000:g} км"
    return length, f"{best:g} м"


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


def format_scale(width_km, page_width_mm):
    """Знаменатель масштаба листа при экспорте, например ``1:50 000``.

    Ширина листа на местности делится на ширину страницы: карта в макете
    занимает страницу целиком.
    """
    if width_km <= 0 or page_width_mm <= 0:
        return ""
    denominator = float(width_km) * 1_000_000.0 / float(page_width_mm)
    if denominator < 1:
        return ""
    digits = f"{round(denominator):,}".replace(",", " ")
    return f"1:{digits}"
