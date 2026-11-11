"""Источники рельефа: описание, покрытие и проверка доступности.

Плагин умеет скачивать несколько наборов DEM.  Наборы различаются
разрешением, вертикальной системой и покрытием, поэтому для выбранного
охвата нужно заранее знать, есть ли в наборе данные.  Проверка идёт по
каталогу STAC (Planetary Computer) для растровых наборов и по известным
границам покрытия для SRTM: так интерфейс не предлагает скачать набор,
данных которого для этого охвата нет.

Кроме обычных наборов есть режим «Автоматически» (``AUTO_SOURCE``):
это не отдельный набор, а способ выбрать набор по тайлам.  Для каждого
тайла охвата подбирается лучший доступный набор — сначала 3DEP, потом
Sonny DTM, а фоном идёт Copernicus GLO-30.  Логика прежнего принудительного
выбора 3DEP переехала именно сюда.

Модуль не скачивает сами тайлы — только описания и проверку доступности.
Загрузка лежит в :mod:`core.terrain`.
"""

import calendar
import http.client
import json
import math
import random
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from . import i18n
from .bbox import BBox

USER_AGENT = "qgis-xtopo/1.0"
STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SIGN_API = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

AUTO_SOURCE = "auto"
SONNY_SOURCE = "sonny"
MANUAL_SOURCE = "manual"

# Источник по умолчанию — режим «Автоматически»: набор подбирается под
# охват (Copernicus GLO-30 фоном, 3DEP и Sonny DTM — где покрывают).
DEFAULT_SOURCE = AUTO_SOURCE
# Старые config.ini без поля terrain_source: источник был SRTM.
LEGACY_DEFAULT_SOURCE = "srtm"
# Набор, которым заполняется фон в режиме «Автоматически».
AUTO_BACKGROUND = "cop30"

# Сколько объектов STAC запрашиваем за один раз и всего.  Для всей страны
# хватает одного ответа с limit=1000, но на всякий случай есть постраничный
# обход.
STAC_LIMIT = 1000
STAC_MAX_ITEMS = 5000

# Слой Natural Earth в ресурсах QGIS: по нему определяется суша.
WORLD_MAP_LAYER = "countries"

# Подпись SAS кэшируется: сервис отвечает 429, если подписывать подряд.
_SIGN_LOCK = threading.Lock()
_SIGN_CACHE = {}
_SIGN_LAST = [0.0]
_SIGN_INTERVAL = 0.3

# Результаты сетевых проверок кэшируются: смена охвата и обновление
# списка тайлов Sonny не должны каждый раз ждать STAC.  Успешная
# проверка живёт дольше, ошибка — меньше, чтобы сетевой сбой не
# «залипал» до перезапуска QGIS.
_CHECK_CACHE = {}
_CHECK_LOCK = threading.Lock()
CHECK_SUCCESS_TTL = 30 * 60
CHECK_FAILURE_TTL = 2 * 60

# Запасные DNS-серверы и DoH: системный резолвер иногда возвращает
# единственный адрес Azure Front Door, у которого закрыт порт 443.
_DNS_SERVERS = ("9.9.9.9", "1.1.1.1", "8.8.8.8")
_DOH_SERVERS = (
    "https://dns.quad9.net/dns-query",
    "https://dns.google/dns-query",
)
_FALLBACK_CACHE = {}
_GOOD_ADDRESS = {}
_FALLBACK_LOCK = threading.Lock()
FALLBACK_TTL = 10 * 60


def _dns_packet(host, query_id):
    """DNS-запрос A-записи в бинарном виде."""
    labels = b"".join(
        bytes([len(label)]) + label.encode("ascii")
        for label in host.rstrip(".").split(".")
    )
    return (
        struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
        + labels + b"\x00"
        + struct.pack("!HH", 1, 1)  # A, IN
    )


def _skip_dns_name(data, offset):
    while offset < len(data):
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length
    return offset


def _parse_dns_a(data, query_id):
    """A-записи из ответа DNS (пустой список — ответ не разобран)."""
    if len(data) < 12:
        return []
    identifier, flags, questions, answers, _auth, _extra = struct.unpack(
        "!HHHHHH", data[:12]
    )
    if identifier != query_id or flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        offset = _skip_dns_name(data, offset) + 4
    addresses = []
    for _ in range(answers):
        offset = _skip_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _ttl, length = struct.unpack(
            "!HHIH", data[offset:offset + 10]
        )
        offset += 10
        if rtype == 1 and rclass == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return addresses


def _dns_udp(host, server, timeout=2.0):
    query_id = random.randrange(0x10000)
    packet = _dns_packet(host, query_id)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 53))
        data, _address = sock.recvfrom(4096)
    return _parse_dns_a(data, query_id)


def _dns_doh(host, url, timeout=3.0):
    query_id = random.randrange(0x10000)
    request = urllib.request.Request(
        url,
        data=_dns_packet(host, query_id),
        method="POST",
        headers={
            "Content-Type": "application/dns-message",
            "Accept": "application/dns-message",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    return _parse_dns_a(data, query_id)


def _fallback_addresses(host):
    """Адреса хоста от публичных резолверов (кэш на время сеанса)."""
    now = time.time()
    with _FALLBACK_LOCK:
        cached = _FALLBACK_CACHE.get(host)
        if cached and now - cached[0] < FALLBACK_TTL:
            return cached[1]
    addresses = []
    for server in _DNS_SERVERS:
        try:
            found = _dns_udp(host, server)
        except OSError:
            continue
        for address in found:
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        for url in _DOH_SERVERS:
            try:
                found = _dns_doh(host, url)
            except Exception:  # noqa: BLE001 - DoH не должен ломать запрос
                continue
            for address in found:
                if address not in addresses:
                    addresses.append(address)
    if addresses:
        with _FALLBACK_LOCK:
            _FALLBACK_CACHE[host] = (now, tuple(addresses))
    return tuple(addresses)


class _MultiAddressHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS-соединение, перебирающее все адреса из ответа DNS.

    Домен STAC отдаёт несколько A-записей, и часть адресов может не
    отвечать (порт 443 закрыт у отдельного узла Azure Front Door).
    Штатный ``socket.create_connection`` получает адреса из одного ответа
    DNS и иногда попадает на нерабочий, ожидая полный таймаут.  Здесь
    адреса перебираются подряд, а на соединение даётся короткий таймаут.
    Если системный резолвер вернул только нерабочий адрес, ответы
    запрашиваются у публичных DNS.
    """

    CONNECT_TIMEOUT = 2.0

    def connect(self):
        if self._tunnel_host:
            return super().connect()
        try:
            addresses = socket.getaddrinfo(
                self.host, self.port, 0, socket.SOCK_STREAM
            )
        except OSError:
            addresses = []
        known = _GOOD_ADDRESS.get(self.host)
        if known:
            addresses = [entry for entry in addresses
                         if entry[4] != known[3]]
            addresses.insert(0, (
                known[0], known[1], known[2], "", known[3],
            ))
        error = self._connect_any(addresses)
        if error is None:
            return
        # Системный DNS дал только нерабочий адрес: пробуем ответы
        # публичных резолверов (у них другой узел Azure Front Door).
        fallback = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "",
             (address, self.port))
            for address in _fallback_addresses(self.host)
        ]
        error = self._connect_any(fallback)
        if error is not None:
            raise error

    def _connect_any(self, addresses):
        last = None
        for family, socktype, proto, _canon, address in addresses:
            sock = socket.socket(family, socktype, proto)
            if self.timeout:
                sock.settimeout(min(self.timeout, self.CONNECT_TIMEOUT))
            try:
                sock.connect(address)
            except OSError as exc:
                last = exc
                sock.close()
                continue
            if self.timeout:
                sock.settimeout(self.timeout)
            try:
                self.sock = self._context.wrap_socket(
                    sock, server_hostname=self.host
                )
            except OSError:
                sock.close()
                raise
            _GOOD_ADDRESS[self.host] = (family, socktype, proto, address)
            return None
        return last


class _MultiAddressHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            _MultiAddressHTTPSConnection, request, context=self._context
        )


_OPENER = urllib.request.build_opener(_MultiAddressHTTPSHandler())


@dataclass(frozen=True)
class DemSource:
    """Описание одного источника рельефа."""

    key: str
    title: str
    short: str
    provider: str
    resolution: str
    datum: str
    coverage: str
    note: str
    # Короткое покрытие для панели: полное с оговорками не помещается.
    coverage_short: str = ""
    attribution: str = ""
    collection: str = ""
    lat_min: float = None
    lat_max: float = None
    native_resolution: float = 1 / 3600.0
    manual: bool = False

    @property
    def remote(self):
        return not self.manual


# Порядок — как в списке интерфейса: сначала автоматический подбор, затем
# обычный SRTM, потом более точные наборы, в конце ручной каталог.
SOURCES = (
    DemSource(
        key=AUTO_SOURCE,
        title="Автоматически (подбор по охвату)",
        short="Автоматически",
        provider="подбор по охвату",
        resolution="по набору",
        datum="по набору",
        coverage="по покрытию наборов",
        coverage_short="по покрытию",
        note="Для каждого тайла выбирается лучший доступный набор: "
             "USGS 3DEP, затем Sonny DTM, фон — Copernicus GLO-30, "
             "в крайнем случае SRTM.",
        attribution="",
    ),
    DemSource(
        key="srtm",
        title="SRTM v3 (skadi), 30 м",
        short="SRTM v3",
        provider="NASA/USGS",
        resolution="30 м (1″)",
        datum="EGM96",
        coverage="56° ю.ш. – 60° с.ш.",
        note="SRTMGL1.003, пустоты заполнены. Текущий источник плагина.",
        attribution="SRTM data courtesy of the U.S. Geological Survey",
        lat_min=-56.0,
        lat_max=60.0,
    ),
    DemSource(
        key="cop30",
        title="Copernicus GLO-30, 30 м",
        short="Copernicus GLO-30",
        provider="ESA/Copernicus",
        resolution="30 м (1″)",
        datum="EGM2008",
        coverage="суша; 25 тайлов не опубликованы (Армения, Азербайджан, "
                 "СЗ Иран, Туркменистан)",
        coverage_short="суша",
        note="TanDEM-X. В крутых склонах точнее SRTM; на море данных нет. "
             "На высоких широтах шаг по долготе грубее.",
        attribution="Produced using Copernicus data and information funded "
                    "by the European Union and ESA",
        collection="cop-dem-glo-30",
    ),
    DemSource(
        key="nasadem",
        title="NASADEM, 30 м",
        short="NASADEM",
        provider="NASA/JPL/USGS",
        resolution="30 м (1″)",
        datum="EGM96",
        coverage="56° ю.ш. – 61° с.ш.",
        note="SRTM v3 с доработкой NASA.",
        attribution="NASADEM — courtesy of NASA/JPL/USGS",
        collection="nasadem",
        lat_min=-56.0,
        lat_max=61.0,
    ),
    DemSource(
        key="alos",
        title="ALOS AW3D30, 30 м",
        short="ALOS AW3D30",
        provider="JAXA",
        resolution="30 м (1″)",
        datum="EGM2008",
        coverage="глобально",
        note="DSM: включает растительность и постройки.",
        attribution="ALOS World 3D-30m — © JAXA",
        collection="alos-dem",
    ),
    DemSource(
        key="3dep",
        title="USGS 3DEP, 10 м",
        short="3DEP",
        provider="USGS",
        resolution="10 м (1/3″)",
        datum="NAVD88",
        coverage="США, Канада, Мексика",
        note="DTM без растительности и построек — единственный такой набор. "
             "В Аляске разрешение грубее.",
        attribution="3D Elevation Program — courtesy of the U.S. Geological "
                    "Survey (public domain)",
        collection="3dep-seamless",
        native_resolution=1 / 10800.0,
    ),
    DemSource(
        key="sonny",
        title="Sonny DTM (LiDAR), 30 м",
        short="Sonny DTM",
        provider="Sonny / 4lima.de",
        resolution="30 м (1″), для Австрии и Швейцарии 15 м (0,5″)",
        datum="национальные системы",
        coverage="Европа",
        coverage_short="Европа",
        note="LiDAR DTM: земная поверхность без растительности и построек. "
             "Точнее SRTM и Copernicus в горах и лесах. "
             "Тайлы 1″ для всей Европы, 0,5″ — только Австрия и Швейцария.",
        attribution="LiDAR Digital Terrain Models of Europe by Sonny — "
                    "https://sonny.4lima.de (CC BY 4.0)",
        native_resolution=1 / 3600.0,
    ),
    DemSource(
        key=MANUAL_SOURCE,
        title="Свой каталог DEM",
        short="Свой каталог",
        provider="пользовательские данные",
        resolution="любое",
        datum="по данным",
        coverage="по вашему каталогу",
        coverage_short="по каталогу",
        note="Тайлы .tif/.hgt/.zip с именами как у SRTM (N42E044).",
        manual=True,
    ),
)

SOURCE_BY_KEY = {source.key: source for source in SOURCES}
SOURCE_KEYS = tuple(source.key for source in SOURCES)


def source(key):
    """Описание источника по ключу."""
    try:
        return SOURCE_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"Unknown DEM source: {key}") from exc


def attribution_for(key):
    """Атрибуция источника (пустая строка, если её не требуется).

    В режиме «Автоматически» подбираются сразу несколько наборов,
    поэтому перечисляются все возможные участники мозаики; точный
    список даёт :func:`attribution_for_keys`.
    """
    if key == AUTO_SOURCE:
        return attribution_for_keys(AUTO_PRIORITY)
    item = SOURCE_BY_KEY.get(key)
    return item.attribution if item is not None else ""


def attribution_for_keys(keys):
    """Атрибуция перечисленных наборов через разделитель."""
    parts = []
    for key in keys:
        value = attribution_for(key) if key != AUTO_SOURCE else ""
        if value and value not in parts:
            parts.append(value)
    return " · ".join(parts)


def tile_name(lat, lon):
    """Имя тайла SRTM-стиля: юго-западный угол."""
    north = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    east = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{north}{east}"


def tile_names(bbox):
    """Тайлы 1°×1°, пересекающие область."""
    if isinstance(bbox, str):
        from .bbox import parse_bbox

        bbox = parse_bbox(bbox)
    names = []
    for lat, lon in tiles_for_bbox(bbox):
        names.append(tile_name(lat, lon))
    return names


def tiles_for_bbox(bbox):
    """Пары (широта, долгота) тайлов 1°×1°, пересекающих область.

    Верхняя граница не включается, если она совпадает с целым градусом:
    тайл, начинающийся ровно на границе, область не пересекает.
    """
    first_lat = math.floor(bbox.lat_min)
    last_lat = math.ceil(bbox.lat_max) - 1
    first_lon = math.floor(bbox.lon_min)
    last_lon = math.ceil(bbox.lon_max) - 1
    tiles = []
    for lat in range(first_lat, last_lat + 1):
        for lon in range(first_lon, last_lon + 1):
            tiles.append((lat, lon))
    return tiles


def aws_tile_url(key, lat, lon):
    """Адрес тайла в публичном бакете AWS."""
    name = tile_name(lat, lon)
    if key == "srtm":
        return (f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/"
                f"{name[:3]}/{name}.hgt.gz")
    if key == "cop30":
        token = f"{name[:3]}_00_{name[3:]}_00"
        return (f"https://copernicus-dem-30m.s3.amazonaws.com/"
                f"Copernicus_DSM_COG_10_{token}_DEM/"
                f"Copernicus_DSM_COG_10_{token}_DEM.tif")
    raise ValueError(f"No AWS bucket for source: {key}")


def item_asset_href(item):
    """Ссылка на растровый файл в объекте STAC."""
    for asset in item.get("assets", {}).values():
        href = asset.get("href", "")
        if href.lower().endswith((".tif", ".tiff")):
            return href
    return ""


def item_tile(item):
    """Тайл 1°×1°, которому принадлежит объект STAC."""
    bbox = item.get("bbox")
    if not bbox or len(bbox) != 4:
        return None
    lat = math.floor((bbox[1] + bbox[3]) / 2.0)
    lon = math.floor((bbox[0] + bbox[2]) / 2.0)
    return lat, lon


def _http_json(url, timeout=30, retries=4, delay=2.0):
    """Запрос JSON с повторами: STAC и подпись отвечают 429."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json"}
    )
    for attempt in range(retries):
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            time.sleep(delay * (2 ** attempt))
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(delay * (2 ** attempt))
    raise RuntimeError("request failed")


def stac_items(collection, bbox, timeout=30, limit=STAC_LIMIT,
               retries=4, delay=2.0, stop_when=None):
    """Объекты STAC коллекции, пересекающие область.

    ``stop_when`` получает уже собранные объекты и может остановить
    постраничный обход: для проверки покрытия достаточно первого
    ответа, в котором есть все нужные тайлы.
    """
    query = urllib.parse.urlencode({
        "collections": collection,
        "bbox": ",".join(str(value) for value in bbox),
        "limit": limit,
    })
    url = f"{STAC_SEARCH}?{query}"
    items = []
    while url and len(items) < STAC_MAX_ITEMS:
        data = _http_json(url, timeout=timeout, retries=retries, delay=delay)
        features = data.get("features") or []
        items.extend(features)
        if not features:
            break
        if stop_when is not None and stop_when(items):
            break
        url = next(
            (link.get("href") for link in data.get("links", [])
             if link.get("rel") == "next"),
            None,
        )
    return items[:STAC_MAX_ITEMS]


def sign_pc(href, timeout=60):
    """Подписанная ссылка Planetary Computer (SAS).

    Подпись — сетевой запрос с ограничением частоты, поэтому результаты
    кэшируются, а запросы выполняются по одному с небольшой паузой.
    """
    now = time.time()
    with _SIGN_LOCK:
        cached = _SIGN_CACHE.get(href)
        if cached and cached[0] > now + 120:
            return cached[1]
        wait = _SIGN_INTERVAL - (now - _SIGN_LAST[0])
        if wait > 0:
            time.sleep(wait)
        url = SIGN_API + "?href=" + urllib.parse.quote(href, safe="")
        signed = _http_json(url, timeout=timeout, retries=6, delay=3.0)["href"]
        _SIGN_LAST[0] = time.time()
        expiry = _SIGN_LAST[0] + 1800
        query = urllib.parse.parse_qs(urllib.parse.urlparse(signed).query)
        stamp = query.get("se", [""])[0]
        if stamp:
            try:
                expiry = calendar.timegm(
                    time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
                )
            except (ValueError, OverflowError):
                pass
        _SIGN_CACHE[href] = (expiry, signed)
        return signed


@dataclass(frozen=True)
class Availability:
    """Результат проверки источника для охвата."""

    available: bool = True
    checked: bool = False
    partial: bool = False
    # Короткая строка для панели и подробное объяснение для подсказки.
    message: str = ""
    detail: str = ""
    missing: int = 0
    total: int = 0
    # Тайлы 1°×1°, по которым проверялось покрытие. Нужны, чтобы
    # отличать океан от суши при выборе лучшего источника.
    required_tiles: frozenset = frozenset()
    covered_tiles: frozenset = frozenset()
    missing_tiles: frozenset = frozenset()


def _bbox_tuple(bbox):
    return bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max


def _clip_to_coverage(bbox, item):
    """Область внутри широтного покрытия набора (None — не пересекается)."""
    minx, miny, maxx, maxy = _bbox_tuple(bbox)
    if item.lat_min is not None:
        miny = max(miny, item.lat_min)
    if item.lat_max is not None:
        maxy = min(maxy, item.lat_max)
    if minx >= maxx or miny >= maxy:
        return None
    return minx, miny, maxx, maxy


def _covered_tiles(items):
    covered = set()
    for item in items:
        tile = item_tile(item)
        if tile is not None:
            covered.add(tile)
    return covered


def _range_availability(item, bbox):
    clipped = _clip_to_coverage(bbox, item)
    if clipped is None:
        return Availability(
            available=False, checked=True, partial=False,
            message=i18n.tr("вне покрытия"),
            detail=i18n.trf(
                "Охват вне покрытия набора: {}", i18n.tr(item.coverage)
            ),
            total=0,
        )
    clipped_bbox = BBox(*clipped)
    tiles = frozenset(tiles_for_bbox(clipped_bbox))
    total = len(tiles)
    partial = clipped != _bbox_tuple(bbox)
    if partial:
        message = i18n.trf("не весь охват, тайлов: {}", total)
        detail = i18n.trf(
            "Данные есть не для всего охвата: часть за пределами {}",
            i18n.tr(item.coverage),
        )
    else:
        message = i18n.trf("весь охват, тайлов: {}", total)
        detail = i18n.trf(
            "Данные есть для всего охвата: тайлов — {}", total
        )
    return Availability(
        available=True, checked=True, partial=partial,
        message=message, detail=detail, missing=0, total=total,
        required_tiles=tiles, covered_tiles=tiles,
    )


def _stac_availability(item, bbox, timeout=30, retries=2):
    clipped = _clip_to_coverage(bbox, item)
    if clipped is None:
        return Availability(
            available=False, checked=True,
            message=i18n.tr("вне покрытия"),
            detail=i18n.trf(
                "Охват вне покрытия набора: {}", i18n.tr(item.coverage)
            ),
        )
    clipped_bbox = BBox(*clipped)
    required = set(tiles_for_bbox(clipped_bbox))
    items = stac_items(
        item.collection, clipped, timeout=timeout, retries=retries,
        delay=1.0,
        stop_when=lambda found: required <= _covered_tiles(found),
    )
    covered = required & _covered_tiles(items)
    outside = clipped != _bbox_tuple(bbox)
    if not covered:
        return Availability(
            available=False, checked=True, partial=False,
            message=i18n.tr("нет данных"),
            detail=i18n.trf(
                "Нет данных для выбранного охвата (покрытие: {})",
                i18n.tr(item.coverage),
            ),
            missing=len(required), total=len(required),
            required_tiles=frozenset(required),
            missing_tiles=frozenset(required),
        )
    missing = required - covered
    partial = bool(missing) or outside
    if partial:
        message = i18n.trf(
            "{} из {} тайлов", len(covered), len(required)
        )
        detail = i18n.trf(
            "Данные есть не для всех тайлов охвата: {} из {} "
            "(часть может быть морем или не опубликована)",
            len(covered), len(required),
        )
        if outside:
            detail += i18n.tr("; часть охвата вне покрытия набора")
    else:
        message = i18n.trf("весь охват, тайлов: {}", len(required))
        detail = i18n.trf(
            "Данные есть для всего охвата: тайлов — {}", len(required)
        )
    return Availability(
        available=True, checked=True, partial=partial,
        message=message, detail=detail,
        missing=len(missing), total=len(required),
        required_tiles=frozenset(required),
        covered_tiles=frozenset(covered),
        missing_tiles=frozenset(missing),
    )


def cop30_tile_exists(lat, lon, timeout=15):
    """Есть ли файл тайла Copernicus GLO-30 (проверка по прямому адресу)."""
    url = aws_tile_url("cop30", lat, lon)
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - сеть не должна ломать проверку
        return False


def cop30_existing_tiles(tiles, timeout=15, workers=8):
    """Тайлы, файлы которых есть в открытом бакете AWS.

    Зеркало в AWS неполное: часть тайлов лежит только в каталоге STAC.
    """
    tiles = list(tiles)
    if not tiles:
        return set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = pool.map(
            lambda tile: cop30_tile_exists(*tile, timeout=timeout), tiles
        )
    return {tile for tile, ok in zip(tiles, found) if ok}


def auto_coverage(bbox, timeout=15, sonny_tiles=(), keys=("3dep", "cop30"),
                  force=False):
    """Покрытие наборов, из которых складывается мозаика «Автоматически».

    Проверяются только нужные наборы: 3DEP и Copernicus GLO-30 (Sonny и
    SRTM считаются по локальным данным).  Остальные наборы в мозаику не
    входят, поэтому их покрытие не запрашивается.  Результаты берутся из
    того же кэша, что и у проверки в панели.
    """
    results = {}
    stac_keys = [key for key in keys if key in ("3dep", "cop30")]

    if stac_keys:
        with ThreadPoolExecutor(max_workers=len(stac_keys)) as pool:
            futures = {
                pool.submit(
                    _check_one, source(key), bbox, timeout, sonny_tiles, force
                ): key
                for key in stac_keys
            }
            for future, key in futures.items():
                results[key] = future.result()

    results["srtm"] = _range_availability(source("srtm"), bbox)
    results[SONNY_SOURCE] = sonny_availability(bbox, sonny_tiles)
    return results


def _bbox_cache_key(bbox):
    return (
        round(bbox.lon_min, 6), round(bbox.lat_min, 6),
        round(bbox.lon_max, 6), round(bbox.lat_max, 6),
    )


def _cache_lookup(key, bbox, force=False):
    """Сохранённый результат проверки или None, если его нет/устарел."""
    if force:
        return None
    cache_key = (key, _bbox_cache_key(bbox))
    entry = _CHECK_CACHE.get(cache_key)
    if entry is None:
        return None
    stored, ttl, state = entry
    if time.time() - stored > ttl:
        with _CHECK_LOCK:
            _CHECK_CACHE.pop(cache_key, None)
        return None
    return state


def _cache_store(key, bbox, state):
    ttl = CHECK_SUCCESS_TTL if state.checked else CHECK_FAILURE_TTL
    with _CHECK_LOCK:
        if len(_CHECK_CACHE) >= 256:
            _CHECK_CACHE.clear()
        _CHECK_CACHE[(key, _bbox_cache_key(bbox))] = (
            time.time(), ttl, state
        )


def clear_check_cache():
    """Сбрасывает кэш проверок (например, перед ручной проверкой)."""
    with _CHECK_LOCK:
        _CHECK_CACHE.clear()


def _cop30_file_availability(bbox, timeout=10):
    """Покрытие Copernicus по прямым адресам, когда STAC недоступен.

    Возвращает None, если не нашёлся ни один файл: значит, сеть не
    работает, и судить о покрытии нельзя.
    """
    required = set(tiles_for_bbox(bbox))
    # Для больших охватов проверять каждый тайл слишком долго: тогда
    # честнее сказать «не удалось проверить», а мозаика возьмёт SRTM.
    if len(required) > 16:
        return None
    covered = cop30_existing_tiles(required, timeout=min(timeout, 6))
    if not covered:
        return None
    missing = required - covered
    if missing:
        message = i18n.trf("{} из {} тайлов", len(covered), len(required))
        detail = i18n.trf(
            "Каталог STAC недоступен; по прямым адресам найдено "
            "{} из {} тайлов.",
            len(covered), len(required),
        )
    else:
        message = i18n.trf("весь охват, тайлов: {}", len(required))
        detail = i18n.tr(
            "Каталог STAC недоступен; по прямым адресам есть все тайлы."
        )
    return Availability(
        available=True, checked=True, partial=bool(missing),
        message=message, detail=detail,
        missing=len(missing), total=len(required),
        required_tiles=frozenset(required),
        covered_tiles=frozenset(covered),
        missing_tiles=frozenset(missing),
    )


def _check_failed(exc):
    """Состояние «проверить не удалось»: источник остаётся доступным."""
    return Availability(
        available=True, checked=False,
        message=i18n.tr("не удалось проверить"),
        detail=i18n.trf(
            "Не удалось проверить доступность: {}. "
            "Повторите проверку кнопкой обновления.",
            str(exc),
        ),
    )


def _check_one(item, bbox, timeout, sonny_tiles=(), force=False):
    if item.manual:
        return Availability(
            available=True, checked=True,
            message=i18n.tr("каталог выбирается вручную"),
        )
    if item.key == SONNY_SOURCE:
        return sonny_availability(bbox, sonny_tiles)
    if not item.collection:
        return _range_availability(item, bbox)
    cached = _cache_lookup(item.key, bbox, force)
    if cached is not None:
        return cached
    try:
        state = _stac_availability(item, bbox, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - проверка не должна ломать UI
        state = _check_failed(exc)
        if item.key == "cop30":
            fallback = _cop30_file_availability(bbox, timeout=timeout)
            if fallback is not None:
                state = fallback
    _cache_store(item.key, bbox, state)
    return state


def check_availability(bbox, cancel=None, timeout=30, sonny_tiles=(),
                       force=False):
    """Проверяет все источники для охвата.

    Возвращает словарь ``ключ → Availability``.  Сетевые проверки идут
    параллельно; ошибка одного набора не мешает остальным.  Результаты
    кэшируются: ``force`` заставляет проверить сеть заново.
    """
    results = {}
    items = [item for item in SOURCES if item.key != AUTO_SOURCE]
    # Все источники проверяются одновременно: медленный STAC одного
    # набора не задерживает остальные.
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        futures = {}
        for item in items:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("DEM availability check cancelled")
            futures[
                pool.submit(
                    _check_one, item, bbox, timeout, sonny_tiles, force
                )
            ] = item
        for future, item in futures.items():
            if cancel is not None and cancel.is_set():
                raise InterruptedError("DEM availability check cancelled")
            results[item.key] = future.result()
    results[AUTO_SOURCE] = auto_availability(bbox, results, sonny_tiles)
    return results


def _world_map_path():
    """Путь к Natural Earth в ресурсах QGIS (None — данных нет)."""
    try:
        from qgis.core import QgsApplication
    except Exception:
        return None
    base = QgsApplication.pkgDataPath()
    if not base:
        return None
    path = Path(base) / "resources" / "data" / "world_map.gpkg"
    return path if path.exists() else None


def _tile_rect(bbox, lat, lon):
    """Пересечение тайла 1°×1° с охватом (None — не пересекаются)."""
    minx = max(bbox.lon_min, float(lon))
    miny = max(bbox.lat_min, float(lat))
    maxx = min(bbox.lon_max, float(lon + 1))
    maxy = min(bbox.lat_max, float(lat + 1))
    if minx >= maxx or miny >= maxy:
        return None
    return minx, miny, maxx, maxy


def _tile_geometry(rect):
    """Прямоугольник тайла как геометрия OGR."""
    from osgeo import ogr

    minx, miny, maxx, maxy = rect
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((minx, miny), (maxx, miny), (maxx, maxy),
                 (minx, maxy), (minx, miny)):
        ring.AddPoint_2D(x, y)
    polygon = ogr.Geometry(ogr.wkbPolygon)
    polygon.AddGeometry(ring)
    return polygon


@lru_cache(maxsize=32)
def _world_map_land_tiles(bbox):
    """Тайлы охвата с сушей по Natural Earth из ресурсов QGIS.

    Слой ``countries`` содержит полигоны суши (Natural Earth 1:10m),
    поэтому океанские квадраты в него не попадают.  Возвращается
    ``None``, если файл ресурсов недоступен: тогда используется
    запасная проверка по ALOS/Copernicus.
    """
    path = _world_map_path()
    if path is None:
        return None
    try:
        from osgeo import ogr

        dataset = ogr.Open(str(path))
        if dataset is None:
            return None
        layer = dataset.GetLayerByName(WORLD_MAP_LAYER)
        if layer is None:
            dataset = None
            return None
        land = set()
        for lat, lon in tiles_for_bbox(bbox):
            rect = _tile_rect(bbox, lat, lon)
            if rect is None:
                continue
            layer.SetSpatialFilterRect(*rect)
            tile = _tile_geometry(rect)
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None and geometry.Intersects(tile):
                    land.add((lat, lon))
                    break
        layer.SetSpatialFilter(None)
        dataset = None
        return frozenset(land)
    except Exception:
        return None


def _land_tiles(bbox, availability):
    """Тайлы охвата, где есть суша.

    Основная маска — Natural Earth из ресурсов QGIS: океанские тайлы в
    неё не попадают.  Запасной вариант — покрытие ALOS/Copernicus: их
    тайлы могут включать океан с нулевыми высотами, поэтому маска менее
    точна и используется только при отсутствии Natural Earth.
    """
    land = _world_map_land_tiles(bbox)
    if land is not None:
        return set(land)
    required = set(tiles_for_bbox(bbox))
    fallback = set()
    for key in ("alos", "cop30"):
        state = availability.get(key)
        if state is None or not state.checked or not state.available:
            continue
        fallback |= set(state.covered_tiles) & required
    return fallback


def preferred_source(bbox, availability):
    """Ключ лучшего источника для охвата (пока только 3DEP).

    ``"3dep"`` возвращается, если 3DEP покрывает всю сушу охвата.
    Океанские тайлы игнорируются: данных 3DEP там всё равно нет.
    Если маску суши построить не удалось, требуется покрытие всех
    тайлов — консервативный запасной вариант.
    """
    state = availability.get("3dep")
    if state is None or not state.checked or not state.available:
        return None
    required = set(state.required_tiles) or set(tiles_for_bbox(bbox))
    land = _land_tiles(bbox, availability)
    if not land:
        land = required
    if not land or land - set(state.covered_tiles):
        return None
    return "3dep"


# --- Режим «Автоматически» --------------------------------------------

# Наборы, из которых складывается мозаика в режиме «Автоматически».
# Порядок — приоритет: первый подходящий набор выигрывает тайл.
AUTO_PRIORITY = ("3dep", "sonny", "cop30", "srtm")

# Наборы с собственным набором тайлов: покрытие берётся из локального
# каталога, а не из STAC.
LOCAL_SOURCES = (SONNY_SOURCE,)


def _coverage(availability, key, sonny_tiles):
    """Множество тайлов 1°×1°, покрытых набором."""
    if key == SONNY_SOURCE:
        return set(sonny_tiles)
    state = availability.get(key)
    if state is None or not state.checked:
        return set()
    return set(state.covered_tiles)


def auto_plan(bbox, availability, sonny_tiles=(), priority=None):
    """Раскладка тайлов охвата по наборам для режима «Автоматически».

    Для каждого тайла 1°×1° выбирается первый набор из ``priority``
    (по умолчанию :data:`AUTO_PRIORITY`), который этот тайл покрывает.
    Тайлы, которых не покрывает ни один набор, остаются без данных.
    """
    tiles = set(tiles_for_bbox(bbox))
    order = tuple(priority) if priority else AUTO_PRIORITY
    coverage = {
        key: _coverage(availability, key, sonny_tiles)
        for key in order
    }
    plan = {}
    for tile in sorted(tiles):
        for key in order:
            if tile in coverage[key]:
                plan[tile] = key
                break
    return plan


def auto_availability(bbox, availability, sonny_tiles=()):
    """Доступность режима «Автоматически» для охвата.

    Показывает, какие наборы войдут в мозаику и сколько тайлов достанется
    каждому.
    """
    tiles = set(tiles_for_bbox(bbox))
    plan = auto_plan(bbox, availability, sonny_tiles)
    counts = {}
    for key in plan.values():
        counts[key] = counts.get(key, 0) + 1
    if not plan:
        unchecked = [
            source(key).short for key in AUTO_PRIORITY
            if availability.get(key) is not None
            and not availability[key].checked
        ]
        if unchecked:
            return Availability(
                available=True, checked=False,
                message=i18n.tr("не удалось проверить"),
                detail=i18n.trf(
                    "Покрытие не удалось проверить: {}. "
                    "Повторите проверку кнопкой обновления.",
                    ", ".join(unchecked),
                ),
                total=len(tiles),
                required_tiles=frozenset(tiles),
            )
        return Availability(
            available=False, checked=True,
            message=i18n.tr("нет данных"),
            detail=i18n.tr(
                "Ни один набор не покрывает выбранный охват."
            ),
            total=len(tiles),
            required_tiles=frozenset(tiles),
            missing_tiles=frozenset(tiles),
        )
    parts = []
    for key in AUTO_PRIORITY:
        if counts.get(key):
            parts.append(f"{source(key).short}: {counts[key]}")
    message = i18n.trf("тайлов: {}", ", ".join(parts))
    detail = i18n.trf(
        "Тайлы 1°×1° разложены по наборам: {}. "
        "Системы высот у наборов разные, на границах возможны ступеньки.",
        ", ".join(parts),
    )
    return Availability(
        available=True, checked=True, partial=len(plan) < len(tiles),
        message=message, detail=detail,
        total=len(tiles),
        required_tiles=frozenset(tiles),
        covered_tiles=frozenset(plan),
        missing_tiles=frozenset(tiles - set(plan)),
    )


def sonny_availability(bbox, sonny_tiles):
    """Доступность набора Sonny DTM по локальному каталогу тайлов."""
    tiles = set(tiles_for_bbox(bbox))
    if not sonny_tiles:
        # Список тайлов ещё не загружен (он лежит в Google Drive) —
        # это не «нет данных», а «неизвестно».
        return Availability(
            available=True, checked=False,
            message=i18n.tr("список тайлов не загружен"),
            detail=i18n.tr(
                "Список тайлов Sonny DTM ещё не загружен. "
                "Повторите проверку позже."
            ),
            total=len(tiles),
            required_tiles=frozenset(tiles),
        )
    covered = tiles & set(sonny_tiles)
    if not covered:
        return Availability(
            available=False, checked=True,
            message=i18n.tr("нет данных"),
            detail=i18n.tr(
                "Набор Sonny DTM не покрывает выбранный охват."
            ),
            total=len(tiles),
            required_tiles=frozenset(tiles),
            missing_tiles=frozenset(tiles),
        )
    partial = len(covered) < len(tiles)
    if partial:
        message = i18n.trf("{} из {} тайлов", len(covered), len(tiles))
        detail = i18n.tr(
            "Sonny DTM покрывает часть охвата; остальные тайлы "
            "доберутся из другого набора."
        )
    else:
        message = i18n.trf("весь охват, тайлов: {}", len(tiles))
        detail = i18n.trf(
            "Sonny DTM покрывает весь охват: тайлов — {}", len(tiles)
        )
    return Availability(
        available=True, checked=True, partial=partial,
        message=message, detail=detail,
        total=len(tiles),
        required_tiles=frozenset(tiles),
        covered_tiles=frozenset(covered),
        missing_tiles=frozenset(tiles - covered),
    )


def auto_source_keys(availability, sonny_tiles=()):
    """Наборы, участвующие в мозаике, в порядке приоритета."""
    keys = []
    for key in AUTO_PRIORITY:
        if _coverage(availability, key, sonny_tiles):
            keys.append(key)
    return keys


def display_availability(key, bbox, availability):
    """Доступность для панели: океанские тайлы не считаются пропуском."""
    state = availability.get(key)
    if state is None or key != "3dep" or bbox is None:
        return state
    if not state.missing_tiles:
        return state
    if preferred_source(bbox, availability) != "3dep":
        return state
    return replace(
        state,
        partial=False,
        message=i18n.tr("суша покрыта, океан без данных"),
        detail=i18n.trf(
            "Данные есть для всей суши охвата: тайлов — {}; "
            "океанские тайлы без данных",
            len(state.covered_tiles),
        ),
    )


def source_warning(key, bbox, availability, sonny_tiles=()):
    """Предупреждение для выбранного источника.

    Для обычных наборов — предложение более точного набора.  Для
    мозаики («Автоматически» или Sonny DTM) — предупреждение о разных
    системах высот: на границах тайлов возможны ступеньки.
    """
    if key in (AUTO_SOURCE, SONNY_SOURCE):
        return mixed_datum_warning(key, bbox, availability, sonny_tiles)
    if key in ("3dep", MANUAL_SOURCE):
        return ""
    if preferred_source(bbox, availability) != "3dep":
        return ""
    return i18n.tr(
        "Для этого охвата доступен USGS 3DEP (10 м, DTM) — "
        "он точнее выбранного источника."
    )


def mixed_datum_warning(key, bbox, availability, sonny_tiles=()):
    """Разные системы высот в мозаике рельефа.

    Наборы, из которых складывается мозаика, приведены к своим
    вертикальным системам: 3DEP — NAVD88, Copernicus — EGM2008,
    SRTM — EGM96, Sonny — национальные.  Смешивать их в одном проекте
    можно, но на границах тайлов высоты расходятся.
    """
    if bbox is None:
        return ""
    if key == AUTO_SOURCE:
        priority = AUTO_PRIORITY
    elif key == SONNY_SOURCE:
        priority = (SONNY_SOURCE, AUTO_BACKGROUND, "srtm")
    else:
        return ""
    plan = auto_plan(bbox, availability, sonny_tiles, priority=priority)
    used = set(plan.values())
    datums = []
    for source_key in priority:
        if source_key not in used:
            continue
        datum = i18n.tr(source(source_key).datum)
        if datum not in datums:
            datums.append(datum)
    if len(datums) < 2:
        return ""
    return i18n.trf(
        "В мозаике наборы с разными системами высот ({}): "
        "на границах тайлов возможны ступеньки.",
        ", ".join(datums),
    )


def describe(key, availability=None):
    """Краткое описание источника для интерфейса.

    Возвращает пару ``(текст, подсказка)``.  Текст — короткие строки:
    провайдер, система высот и короткое покрытие, затем доступность.
    Подробности — в подсказке: в узкой панели длинный текст обрезается.
    """
    item = source(key)
    provider = i18n.tr(item.provider)
    datum = i18n.tr(item.datum)
    note = i18n.tr(item.note)
    coverage = i18n.tr(item.coverage)
    coverage_short = i18n.tr(item.coverage_short or item.coverage)
    first = " · ".join(
        value for value in (provider, datum, coverage_short) if value
    )
    lines = [first]
    if availability is not None:
        lines.append(
            i18n.tr("Доступность: {}").replace(
                "{}", i18n.tr(availability.message)
            )
        )
    tooltip = [
        i18n.trf("Провайдер: {}", provider),
        i18n.trf("Разрешение: {}", i18n.tr(item.resolution)),
        i18n.trf("Высоты: {}", datum),
        i18n.trf("Покрытие: {}", coverage),
        note,
    ]
    if availability is not None:
        detail = availability.detail or availability.message
        tooltip.append(
            i18n.tr("Доступность: {}").replace("{}", i18n.tr(detail))
        )
    if item.attribution:
        tooltip.append(i18n.trf(
            "Атрибуция: {}", i18n.tr(item.attribution)
        ))
    return "\n".join(lines), "\n".join(tooltip)
