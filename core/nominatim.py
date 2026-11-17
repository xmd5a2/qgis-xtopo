"""Поиск мест по Nominatim — геокодеру на данных OpenStreetMap.

Запросы идут по HTTP к nominatim.openstreetmap.org.  Сервис требует
заголовок User-Agent и просит обращаться не чаще одного раза в секунду,
поэтому между запросами выдерживается пауза, а ответы кэшируются по строке
запроса: повторный поиск того же текста сеть не дёргает.  Ответ «слишком
много запросов» (429) обрабатывается отдельно: сервис сам говорит, сколько
ждать.

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
# Сколько живёт запись кэша, секунды: данные Nominatim меняются медленно,
# но и вечно держать их нельзя.
CACHE_TTL = 3600.0
# Сколько раз повторяем запрос после ответа 429.
RETRY_LIMIT = 3
# Пауза перед повтором, если сервер не назвал свою (Retry-After).
RETRY_DELAY = 5.0
# Больше этой паузы не ждём: запрос лучше прервать, чем висеть.
MAX_RETRY_DELAY = 60.0

_lock = threading.Lock()
# Отдельный замок для паузы между запросами: спящий поток не должен
# закрывать доступ к кэшу.
_turn_lock = threading.Lock()
_last_request = 0.0
# Кэш: ключ → (время, список мест).
_cache = {}
# Запросы, которые уже выполняются: ключ → событие окончания.
_inflight = {}

_urlopen = urllib.request.urlopen


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
    with _turn_lock:
        wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _retry_after(exc, attempt):
    """Пауза перед повтором: из Retry-After или по умолчанию."""
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value:
        try:
            return min(MAX_RETRY_DELAY, max(0.0, float(value)))
        except (TypeError, ValueError):
            pass
    return min(MAX_RETRY_DELAY, RETRY_DELAY * (attempt + 1))


def _fetch(query, limit, timeout):
    """Ответ сервиса по строке запроса (список словарей)."""
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
    payload = b""
    for attempt in range(RETRY_LIMIT + 1):
        try:
            with _urlopen(request, timeout=timeout) as response:
                payload = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < RETRY_LIMIT:
                # Сервис просит подождать: ждём столько, сколько он
                # назвал, и повторяем запрос.
                time.sleep(_retry_after(exc, attempt))
                continue
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


def _cached(key):
    """Запись кэша, если она ещё не устарела."""
    with _lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    stored, places = entry
    if time.time() - stored > CACHE_TTL:
        return None
    return places


def search(query, limit=RESULT_LIMIT, timeout=REQUEST_TIMEOUT):
    """Места по строке запроса.

    Повторный запрос с тем же текстом берётся из кэша.  Одинаковые
    параллельные запросы выполняются один раз: второй ждёт результат
    первого и берёт его из кэша.  Ошибка сети поднимается как
    :class:`NominatimError`.
    """
    text = str(query or "").strip()
    if not text:
        return []
    key = (text.lower(), limit)
    while True:
        places = _cached(key)
        if places is not None:
            return list(places)
        with _lock:
            event = _inflight.get(key)
            owner = event is None
            if owner:
                event = threading.Event()
                _inflight[key] = event
        if owner:
            break
        # Такой же запрос уже выполняется: ждём его результат.
        event.wait(timeout)
        places = _cached(key)
        if places is not None:
            return list(places)
        # Первый запрос не удался: следующий по очереди становится
        # исполнителем.
    try:
        _wait_turn()
        items = _fetch(text, limit, timeout)
        found = []
        for item in items:
            if not isinstance(item, dict):
                continue
            place = _place(item)
            if place is not None and place.name:
                found.append(place)
        with _lock:
            if len(_cache) >= CACHE_LIMIT:
                _cache.clear()
            _cache[key] = (time.time(), list(found))
        return found
    finally:
        with _lock:
            _inflight.pop(key, None)
        event.set()


def clear_cache():
    """Сбросить кэш ответов (для тестов)."""
    global _last_request
    with _lock:
        _cache.clear()
        _inflight.clear()
    with _turn_lock:
        _last_request = 0.0
