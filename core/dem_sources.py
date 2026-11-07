"""Источники рельефа: описание, покрытие и проверка доступности.

Плагин умеет скачивать несколько наборов DEM.  Наборы различаются
разрешением, вертикальной системой и покрытием, поэтому для выбранного
охвата нужно заранее знать, есть ли в наборе данные.  Проверка идёт по
каталогу STAC (Planetary Computer) для растровых наборов и по известным
границам покрытия для SRTM: так интерфейс не предлагает скачать набор,
данных которого для этого охвата нет.

Модуль не скачивает сами тайлы — только описания и проверку доступности.
Загрузка лежит в :mod:`core.terrain`.
"""

import calendar
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import i18n
from .bbox import BBox

USER_AGENT = "qgis-xtopo/1.0"
STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SIGN_API = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

DEFAULT_SOURCE = "srtm"
MANUAL_SOURCE = "manual"

# Сколько объектов STAC запрашиваем за один раз и всего.  Для всей страны
# хватает одного ответа с limit=1000, но на всякий случай есть постраничный
# обход.
STAC_LIMIT = 1000
STAC_MAX_ITEMS = 5000

# Подпись SAS кэшируется: сервис отвечает 429, если подписывать подряд.
_SIGN_LOCK = threading.Lock()
_SIGN_CACHE = {}
_SIGN_LAST = [0.0]
_SIGN_INTERVAL = 0.3


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
    attribution: str = ""
    collection: str = ""
    lat_min: float = None
    lat_max: float = None
    native_resolution: float = 1 / 3600.0
    manual: bool = False

    @property
    def remote(self):
        return not self.manual


# Порядок — как в списке интерфейса: сначала обычный SRTM, затем более
# точные наборы, в конце ручной каталог.
SOURCES = (
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
        coverage="только США",
        note="DTM без растительности и построек — единственный такой набор. "
             "В Аляске разрешение грубее.",
        attribution="3D Elevation Program — courtesy of the U.S. Geological "
                    "Survey (public domain)",
        collection="3dep-seamless",
        native_resolution=1 / 10800.0,
    ),
    DemSource(
        key=MANUAL_SOURCE,
        title="Свой каталог DEM",
        short="Свой каталог",
        provider="пользовательские данные",
        resolution="любое",
        datum="зависит от данных",
        coverage="по вашему каталогу",
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
    """Атрибуция источника (пустая строка, если её не требуется)."""
    item = SOURCE_BY_KEY.get(key)
    return item.attribution if item is not None else ""


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
            with urllib.request.urlopen(request, timeout=timeout) as response:
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
               retries=4, delay=2.0):
    """Объекты STAC коллекции, пересекающие область."""
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
    message: str = ""
    missing: int = 0
    total: int = 0


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
            message=i18n.trf("Охват вне покрытия набора: {}", i18n.tr(item.coverage)),
            total=0,
        )
    clipped_bbox = BBox(*clipped)
    total = len(tiles_for_bbox(clipped_bbox))
    partial = clipped != _bbox_tuple(bbox)
    if partial:
        message = i18n.trf(
            "Данные есть не для всего охвата: часть за пределами {}",
            i18n.tr(item.coverage),
        )
    else:
        message = i18n.trf(
            "Данные есть для всего охвата: тайлов — {}", total
        )
    return Availability(
        available=True, checked=True, partial=partial,
        message=message, missing=0, total=total,
    )


def _stac_availability(item, bbox, timeout=30):
    clipped = _clip_to_coverage(bbox, item)
    if clipped is None:
        return Availability(
            available=False, checked=True,
            message=i18n.trf("Охват вне покрытия набора: {}", i18n.tr(item.coverage)),
        )
    clipped_bbox = BBox(*clipped)
    items = stac_items(
        item.collection, clipped, timeout=timeout, retries=2, delay=1.0
    )
    required = set(tiles_for_bbox(clipped_bbox))
    covered = required & _covered_tiles(items)
    outside = clipped != _bbox_tuple(bbox)
    if not covered:
        return Availability(
            available=False, checked=True, partial=False,
            message=i18n.trf(
                "Нет данных для выбранного охвата (покрытие: {})",
                i18n.tr(item.coverage),
            ),
            missing=len(required), total=len(required),
        )
    missing = required - covered
    partial = bool(missing) or outside
    if partial:
        message = i18n.trf(
            "Данные есть не для всех тайлов охвата: {} из {} "
            "(часть может быть морем или не опубликована)",
            len(covered), len(required),
        )
        if outside:
            message += i18n.tr("; часть охвата вне покрытия набора")
    else:
        message = i18n.trf(
            "Данные есть для всего охвата: тайлов — {}", len(required)
        )
    return Availability(
        available=True, checked=True, partial=partial,
        message=message, missing=len(missing), total=len(required),
    )


def _check_one(item, bbox, timeout):
    if item.manual:
        return Availability(
            available=True, checked=True,
            message=i18n.tr("Свой каталог: каталог выбирается вручную"),
        )
    if not item.collection:
        return _range_availability(item, bbox)
    try:
        return _stac_availability(item, bbox, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - проверка не должна ломать UI
        return Availability(
            available=True, checked=False,
            message=i18n.tr(
                "Не удалось проверить доступность: {}"
            ).replace("{}", str(exc)),
        )


def check_availability(bbox, cancel=None, timeout=30):
    """Проверяет все источники для охвата.

    Возвращает словарь ``ключ → Availability``.  Сетевые проверки идут
    параллельно; ошибка одного набора не мешает остальным.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for item in SOURCES:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("DEM availability check cancelled")
            futures[pool.submit(_check_one, item, bbox, timeout)] = item
        for future, item in futures.items():
            if cancel is not None and cancel.is_set():
                raise InterruptedError("DEM availability check cancelled")
            results[item.key] = future.result()
    return results


def describe(key, availability=None):
    """Краткое описание источника для интерфейса.

    Возвращает пару ``(текст, подсказка)``.  Текст — несколько строк:
    название, провайдер, система высот, покрытие и доступность.
    """
    item = source(key)
    title = i18n.tr(item.title)
    provider = i18n.tr(item.provider)
    datum = i18n.tr(item.datum)
    note = i18n.tr(item.note)
    coverage = i18n.tr(item.coverage)
    first = " · ".join(value for value in (title, provider, datum) if value)
    lines = [first, i18n.trf("{} Покрытие: {}", note, coverage)]
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
    if item.attribution:
        tooltip.append(i18n.trf(
            "Атрибуция: {}", i18n.tr(item.attribution)
        ))
    return "\n".join(lines), "\n".join(tooltip)
