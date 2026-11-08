"""Поиск мест по Nominatim — геокодеру на данных OpenStreetMap.

Запросы идут по HTTP к nominatim.openstreetmap.org.  Сервис требует
заголовок User-Agent и просит обращаться не чаще одного раза в секунду,
поэтому между запросами выдерживается пауза, а ответы кэшируются по строке
запроса: повторный поиск того же текста сеть не дёргает.

Модуль ничего не знает про интерфейс — он только возвращает список мест.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "QGIS-xtopo/1.0 (+https://github.com/xmd5a2/qgis-xtopo)"
RESULT_LIMIT = 10
REQUEST_TIMEOUT = 20
# Пауза между запросами: политика сервиса — не больше одного запроса в секунду.
REQUEST_INTERVAL = 1.0
# Сколько ответов держим в кэше.
CACHE_LIMIT = 50

_urlopen = urllib.request.urlopen
_lock = threading.Lock()
_last_request = 0.0
_cache = {}


class NominatimError(RuntimeError):
    """Ошибка запроса к Nominatim."""


@dataclass(frozen=True)
class Place:
    """Найденное место.

    Границы (``south``/``north``/``west``/``east``) приходят не у всех
    результатов: у точки их нет, и тогда ``bounds()`` возвращает None.
    """

    name: str
    lat: float
    lon: float
    south: float = 0.0
    north: float = 0.0
    west: float = 0.0
    east: float = 0.0
    kind: str = ""
    osm_type: str = ""
    osm_id: int = 0

    @property
    def has_bounds(self):
        """Есть ли у места прямоугольник границ."""
        return self.north > self.south and self.east > self.west

    def bounds(self):
        """Границы места (west, south, east, north) или None."""
        if not self.has_bounds:
            return None
        return (self.west, self.south, self.east, self.north)


def _wait_turn():
    """Выдержать паузу между запросами."""
    global _last_request
    with _lock:
        wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _fetch(query, limit, timeout):
    """Ответ сервира по строке запроса (список словарей)."""
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": limit,
            "addressdetails": 0,
        }
    )
    request = urllib.request.Request(
        f"{SEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with _urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise NominatimError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise NominatimError(str(exc)) from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NominatimError("не удалось разобрать ответ") from exc
    if not isinstance(data, list):
        raise NominatimError("неожиданный ответ")
    return data


def _place(item):
    """Разбор одного результата (None — если разобрать нельзя)."""
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    values = []
    for value in (item.get("boundingbox") or [])[:4]:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            values = []
            break
    south = north = west = east = 0.0
    if len(values) == 4:
        # В ответе границы идут в порядке: юг, север, запад, восток.
        south, north, west, east = values
    try:
        osm_id = int(item.get("osm_id") or 0)
    except (TypeError, ValueError):
        osm_id = 0
    return Place(
        name=str(item.get("display_name") or item.get("name") or ""),
        lat=lat,
        lon=lon,
        south=south,
        north=north,
        west=west,
        east=east,
        kind=str(item.get("type") or ""),
        osm_type=str(item.get("osm_type") or ""),
        osm_id=osm_id,
    )


def search(query, limit=RESULT_LIMIT, timeout=REQUEST_TIMEOUT):
    """Места по строке запроса.

    Повторный запрос с тем же текстом берётся из кэша.  Ошибка сети
    поднимается как :class:`NominatimError`.
    """
    text = str(query or "").strip()
    if not text:
        return []
    key = (text.lower(), limit)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return list(cached)
    _wait_turn()
    items = _fetch(text, limit, timeout)
    places = []
    for item in items:
        if not isinstance(item, dict):
            continue
        place = _place(item)
        if place is not None and place.name:
            places.append(place)
    with _lock:
        if len(_cache) >= CACHE_LIMIT:
            _cache.clear()
        _cache[key] = list(places)
    return places


def clear_cache():
    """Сбросить кэш ответов (для тестов)."""
    global _last_request
    with _lock:
        _cache.clear()
        _last_request = 0.0
