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
    QgsUnitTypes,
    Qgis,
)


WEB_MERCATOR_RADIUS = 6378137.0
FEET_PER_METER = 3.28084


def mercator_ground_factor(y):
    """Во сколько раз метр EPSG:3857 длиннее метра на местности.

    В Web Mercator масштаб постоянен по широте: на 62° один метр карты
    соответствует примерно 0,48 метра на земле.
    """
    latitude = 2.0 * math.atan(
        math.exp(float(y) / WEB_MERCATOR_RADIUS)
    ) - math.pi / 2.0
    return math.cos(latitude)


def ground_width(extent, crs):
    """Ширина охвата в метрах на местности.

    В EPSG:3857 метры проекции растянуты с широтой: на 62° один метр
    карты соответствует примерно 0,48 метра на земле.  Для географических
    СК ширина считается по эллипсоиду WGS84.
    """
    if extent is None or crs is None or not crs.isValid():
        return 0.0
    if extent.width() <= 0:
        return 0.0
    if crs.isGeographic():
        try:
            distance = QgsDistanceArea()
            distance.setSourceCrs(
                crs, QgsProject.instance().transformContext()
            )
            distance.setEllipsoid("WGS84")
            middle = extent.center().y()
            return distance.measureLine(
                QgsPointXY(extent.xMinimum(), middle),
                QgsPointXY(extent.xMaximum(), middle),
            )
        except Exception:
            return 0.0
    try:
        factor = QgsUnitTypes.fromUnitToUnitFactor(
            crs.mapUnits(), Qgis.DistanceUnit.Meters
        )
    except Exception:
        factor = 1.0
    width = extent.width() * factor
    if crs.authid() == "EPSG:3857":
        width *= mercator_ground_factor(extent.center().y())
    return width


def scale_bar(meters_per_pixel, max_length=140.0, units="m"):
    """Длина полосы шкалы в пикселях и её подпись.

    Длина подбирается так, чтобы соответствовать «круглому»
    расстоянию: 1, 2 или 5 × 10ⁿ метров, а при ``units="ft"`` — футов.
    """
    if not meters_per_pixel or meters_per_pixel <= 0:
        return 0.0, ""
    feet = str(units or "").strip().lower() == "ft"
    unit_per_pixel = meters_per_pixel * (FEET_PER_METER if feet else 1.0)
    target = unit_per_pixel * max_length
    exponent = math.floor(math.log10(target)) if target > 0 else 0
    best = 0.0
    for factor in (1.0, 2.0, 5.0):
        value = factor * (10 ** exponent)
        if value <= target and value > best:
            best = value
    if best <= 0:
        best = target
    length = best / unit_per_pixel
    if length < 10 or length > max_length:
        return 0.0, ""
    if feet:
        text = f"{best:,.0f}".replace(",", " ")
        return length, f"{text} ф"
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


# Средний радиус Земли, км: для площади рамки достаточно сферической
# формулы, расхождение с эллипсоидом — доли процента.
EARTH_RADIUS_KM = 6371.0088


def _rect_area_km2(min_lon, min_lat, max_lon, max_lat):
    return (
        EARTH_RADIUS_KM ** 2
        * math.radians(max_lon - min_lon)
        * (
            math.sin(math.radians(max_lat))
            - math.sin(math.radians(min_lat))
        )
    )


def frame_area_km2(bbox):
    """Площадь рамки на сфере в квадратных километрах."""
    return _rect_area_km2(
        bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max
    )


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
