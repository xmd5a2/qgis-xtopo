import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BBox:
    lon_min: float
    lat_min: float
    lon_max: float
    lat_max: float

    def __post_init__(self):
        values = (self.lon_min, self.lat_min, self.lon_max, self.lat_max)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Bounding box contains non-finite values")
        if not (-180 <= self.lon_min < self.lon_max <= 180):
            raise ValueError("Longitude must satisfy -180 <= lon_min < lon_max <= 180")
        if not (-90 <= self.lat_min < self.lat_max <= 90):
            raise ValueError("Latitude must satisfy -90 <= lat_min < lat_max <= 90")

    def as_csv(self, places=4):
        def format_value(value):
            text = f"{value:.{places}f}".rstrip("0").rstrip(".")
            return "0" if text in ("", "-0") else text

        return ",".join(format_value(value) for value in (
            self.lon_min, self.lat_min, self.lon_max, self.lat_max
        ))

    def as_overpass(self):
        return f"{self.lat_min},{self.lon_min},{self.lat_max},{self.lon_max}"

    def as_osmium(self):
        return f"{self.lon_min},{self.lat_min},{self.lon_max},{self.lat_max}"

    def as_tuple(self):
        return self.lon_min, self.lat_min, self.lon_max, self.lat_max

    def polygon_geojson(self):
        ring = [
            [self.lon_min, self.lat_max],
            [self.lon_max, self.lat_max],
            [self.lon_max, self.lat_min],
            [self.lon_min, self.lat_min],
            [self.lon_min, self.lat_max],
        ]
        return {
            "type": "FeatureCollection",
            "name": "crop",
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
            "features": [
                {
                    "type": "Feature",
                    "properties": {"properties": None},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            ],
        }


def parse_bbox(value):
    text = (value or "").strip()
    if not text:
        raise ValueError("Bounding box is empty")
    if "openstreetmap" in text or "map=" in text:
        result = bbox_from_osm_link(text)
    else:
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 4:
            raise ValueError("Bounding box must contain four comma-separated values")
        try:
            result = BBox(*(float(part) for part in parts))
        except ValueError as exc:
            raise ValueError("Bounding box values must be numbers") from exc
    return result


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid coordinate: {value!r}") from exc


def bbox_from_osm_link(link):
    if "map=" in link:
        location = link.split("map=", 1)[1]
        location = location.split("&", 1)[0].split("/")
        if len(location) < 3:
            raise ValueError("Cannot parse the OpenStreetMap link")
        zoom, lat, lon = location[0], location[1], location[2]
    else:
        query = link.split("?", 1)[-1]
        params = dict(re.findall(r"([^=&]+)=([^&]*)", query))
        if not {"lat", "lon", "zoom"}.issubset(params):
            raise ValueError("Cannot parse the OpenStreetMap link")
        zoom, lat, lon = params["zoom"], params["lat"], params["lon"]
    return bbox_from_location(lat, lon, zoom)


def bbox_from_location(lat, lon, zoom):
    lat = _number(lat)
    lon = _number(lon)
    zoom = _number(zoom)
    if zoom < 6:
        raise ValueError("Zoom level must be at least 6")
    if not (-90 <= lat <= 90):
        raise ValueError("Latitude is out of range")
    if not (-180 <= lon <= 180):
        raise ValueError("Longitude is out of range")

    scale_meters_per_pixel = 0.0003
    lat_rad = math.radians(lat)
    meters_per_pixel = 10000000 * math.cos(lat_rad) / (2 ** (zoom + 8))
    earth_meters_per_picture_meters = meters_per_pixel / scale_meters_per_pixel
    meters_north = earth_meters_per_picture_meters / 2
    meters_east = earth_meters_per_picture_meters / 2
    meters_per_degree_lat = 111111.0
    meters_per_degree_lon = 111111.0 * math.cos(lat_rad)
    if meters_per_degree_lon <= 0:
        raise ValueError("Cannot calculate longitude span at this latitude")

    degrees_north = meters_north / meters_per_degree_lat
    degrees_east = 1.6 * (meters_east / meters_per_degree_lon)
    north = min(90.0, round(lat + degrees_north, 3))
    south = max(-90.0, round(lat - degrees_north, 3))
    east = min(180.0, round(lon + degrees_east, 3))
    west = max(-180.0, round(lon - degrees_east, 3))
    return BBox(west, south, east, north)


def bbox_from_canvas(iface):
    canvas = iface.mapCanvas()
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject

    source_crs = canvas.mapSettings().destinationCrs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if source_crs.isValid() and source_crs != wgs84:
        transform = QgsCoordinateTransform(source_crs, wgs84, QgsProject.instance())
        extent = transform.transformBoundingBox(canvas.extent())
    else:
        extent = canvas.extent()
    return BBox(extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum())
