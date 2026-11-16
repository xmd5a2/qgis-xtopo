"""Файловые выгрузки OSM с других хостов и вырез по рамке.

Модуль дополняет Geofabrik: те же выгрузки по регионам, но с других
хостов (openstreetmap.fr, geo2day, movisda), плюс вырез по произвольной
рамке через SliceOSM.  Всё работает по файлам — без Overpass.

Границы регионов geo2day и openstreetmap.fr берутся из индекса geo2day:
там для каждого региона есть ссылка на GeoJSON с его контуром.  Остальные
провайдеры используют те же нарезки по границам OSM, поэтому их выгрузки
сопоставляются с границами по названию региона.  У movisda свои контуры
в общем индексе `index.geojson`.
"""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle

from . import geofabrik


USER_AGENT = "QGIS-xtopo/0.7 (+https://github.com/xmd5a2/qgis-xtopo)"
# Индексы провайдеров меняются раз в сутки.
INDEX_TTL = 24 * 60 * 60
HTTP_TIMEOUT = 30
HEAD_TIMEOUT = 15
# Таймаут ожидания данных: сервер может прислать первые килобайты и
# замолчать.  Тогда запрос повторяется с того места, где он остановился.
IDLE_TIMEOUT = 20
IDLE_TIMEOUT_SHORT = 5
READ_CHUNK = 64 * 1024
# Сколько байт запрашивать за раз у хостов, отдающих ответ частями.
RANGE_CHUNK = 64 * 1024
# Сколько регионов проверять на границы за один проход.
GEOMETRY_WORKERS = 8
# Сколько каталогов openstreetmap.fr запрашивать одновременно.
WALK_WORKERS = 8
# Сколько выгрузок проверять HEAD-запросом за раз.
MAX_HEAD_CHECKS = 24


class ExtractError(RuntimeError):
    pass


@dataclass
class ExtractMap:
    """Выгрузка провайдера: имя, адрес и, если нашли, границы."""

    id: str
    name: str
    parent: str
    url: str
    size: int = 0
    geometry: object = None
    # Провайдер-хост: входит в имя локального файла, чтобы выгрузки
    # разных хостов не путались.
    namespace: str = ""
    # Исходная геометрия GeoJSON: movisda отдаёт её в общем индексе,
    # а преобразуется она только для регионов, попавших в охват.
    boundary: object = None


@dataclass
class Catalogue:
    """Список выгрузок провайдера с границами для выбора по охвату."""

    maps: list = field(default_factory=list)
    fetched_at: float = 0.0
    last_modified: str = ""
    warning: str = ""


_urlopen = urllib.request.urlopen


# ---------------------------------------------------------------------------
# Сеть
# ---------------------------------------------------------------------------


def _open(url, headers=None, timeout=HTTP_TIMEOUT, method=None, data=None):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        method=method,
        data=data,
    )
    return _urlopen(request, timeout=timeout)


def fetch_bytes(url, headers=None, timeout=HTTP_TIMEOUT, cancel=None,
                progress=None, label=""):
    """Скачать ответ целиком."""
    try:
        with _open(url, headers=headers, timeout=timeout) as response:
            chunks = []
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Загрузка прервана")
                chunk = response.read(READ_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                if progress:
                    progress(len(chunk))
            return b"".join(chunks)
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        raise ExtractError(f"{url}: HTTP {exc.code}") from exc
    except Exception as exc:
        raise ExtractError(f"{url}: {exc}") from exc


def fetch_json(url, **kwargs):
    payload = fetch_bytes(url, **kwargs)
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ExtractError(f"{url}: ответ не является JSON") from exc


def head_size(url):
    """Размер файла и признак доступности (HEAD, при отказе — Range)."""
    try:
        with _open(url, method="HEAD", timeout=HEAD_TIMEOUT) as response:
            size = int(response.headers.get("Content-Length") or 0)
            return True, size, ""
    except Exception as exc:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
            )
            with _urlopen(request, timeout=HEAD_TIMEOUT) as response:
                content_range = response.headers.get("Content-Range") or ""
                size = 0
                if "/" in content_range:
                    try:
                        size = int(content_range.rsplit("/", 1)[-1])
                    except ValueError:
                        size = 0
                return True, size, ""
        except Exception:
            return False, 0, str(exc)


def download_file(url, destination, progress=None, cancel=None, expected_size=0,
                  label="", check_pbf=True):
    """Скачать файл по частям.

    Часть хостов отдаёт ответ не целиком: присылает несколько килобайт и
    замолкает.  Поэтому файл читается диапазонами, а оборванный ответ
    продолжается с места, где он остановился.  Обычные хосты отдают файл
    целиком с первого запроса.  ``check_pbf=False`` позволяет скачать так
    же индекс (JSON), а не только PBF.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    received = temporary.stat().st_size if temporary.exists() else 0
    total = expected_size
    if not total:
        available, size, _error = head_size(url)
        if available and size:
            total = size
    if total and received >= total:
        received = 0
        temporary.unlink(missing_ok=True)
    idle = IDLE_TIMEOUT
    stall = 0
    attempts = 0
    reported = received
    # Прогресс пишется примерно раз в 5 % файла, но не чаще, чем раз в
    # мегабайт: иначе на 100 МБ в журнал попадают сотни строк.
    progress_step = (
        max(1024 * 1024, total // 20) if total else 2 * 1024 * 1024
    )
    open_ended = False
    while True:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Загрузка прервана")
        attempts += 1
        if attempts > 5000:
            raise ExtractError(f"{url}: слишком много попыток загрузки")
        headers = {"User-Agent": USER_AGENT}
        if received:
            if total:
                end = min(total - 1, received + RANGE_CHUNK - 1)
            else:
                end = received + RANGE_CHUNK - 1
            headers["Range"] = (
                f"bytes={received}-" if open_ended else f"bytes={received}-{end}"
            )
        try:
            with _open(url, headers=headers, timeout=idle) as response:
                code = getattr(response, "status", 200)
                if code == 200 and received:
                    # Сервер проигнорировал Range — начинаем заново.
                    received = 0
                    reported = 0
                    temporary.unlink(missing_ok=True)
                mode = "ab" if received else "wb"
                with temporary.open(mode) as handle:
                    while True:
                        try:
                            chunk = response.read1(READ_CHUNK)
                        except (socket.timeout, TimeoutError):
                            chunk = b""
                        except AttributeError:
                            chunk = response.read(READ_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        if progress and received - reported >= progress_step:
                            reported = received
                            progress(
                                f"{label or url}: скачано "
                                f"{received / 1048576:.1f} МБ"
                            )
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and not open_ended:
                # Некоторые серверы ждут явную правую границу диапазона.
                open_ended = True
                continue
            raise ExtractError(f"{url}: HTTP {exc.code}") from exc
        except InterruptedError:
            raise
        except Exception as exc:
            if received == 0:
                raise ExtractError(f"{url}: {exc}") from exc
            # Ответ оборвался — продолжаем с того же места.
            stall += 1
            if stall > 20:
                raise ExtractError(f"{url}: загрузка прерывается ({exc})") from exc
            idle = IDLE_TIMEOUT_SHORT
            continue
        if total and received >= total:
            break
        if received == 0:
            stall += 1
            if stall > 5:
                raise ExtractError(f"{url}: сервер не отдаёт данные")
            idle = IDLE_TIMEOUT_SHORT
            continue
        # Данных меньше, чем просили: сервер отдаёт ответ частями.
        stall += 1
        idle = IDLE_TIMEOUT_SHORT
    if check_pbf and not _looks_like_pbf(temporary):
        temporary.unlink(missing_ok=True)
        raise ExtractError(f"{url}: полученный файл не похож на PBF")
    os.replace(temporary, destination)
    return destination


def _looks_like_pbf(path):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(4)
            if len(raw) < 4:
                return False
            size = int.from_bytes(raw, "big")
            if not 0 < size < 1000000:
                return False
            return b"OSMHeader" in handle.read(size)[:200]
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Каталоги и границы
# ---------------------------------------------------------------------------


def parse_listing(html):
    """Разбор листинга каталога: имена каталогов и файлов."""
    directories = []
    files = []
    for href in re.findall(r'href="([^"]+)"', html):
        if href in ("../", "/", "./") or "://" in href or href.startswith("?"):
            continue
        if href.endswith("/"):
            directories.append(href[:-1])
        else:
            files.append(href)
    return directories, files


def geometry_from_geojson(payload):
    """Геометрия из GeoJSON (строки, объект или FeatureCollection)."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "FeatureCollection":
        geometries = []
        for feature in payload.get("features") or []:
            part = geometry_from_geojson(feature)
            if part is None or part.isEmpty():
                continue
            geometries.append(part)
        if not geometries:
            return None
        return QgsGeometry.unaryUnion(geometries)
    if payload.get("type") == "Feature":
        return geometry_from_geojson(payload.get("geometry"))
    geometry = geofabrik.qgs_geometry(payload)
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    return geometry


def geometry_from_poly(text):
    """Геометрия из файла .poly (формат osmosis).

    Внешнее кольцо начинается строкой «1», внутреннее — «!1»; пустая
    строка отделяет следующий многоугольник.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    rings = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "polygon":
            if current:
                rings.append(current)
                current = None
            continue
        if line.startswith("!"):
            if current:
                rings.append(current)
                current = None
            continue
        if line == "1":
            if current:
                rings.append(current)
            current = []
            continue
        parts = line.split()
        if len(parts) >= 2 and current is not None:
            try:
                current.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if current:
        rings.append(current)
    polygons = []
    for ring in rings:
        if len(ring) < 3:
            continue
        polygons.append([ring])
    if not polygons:
        return None
    try:
        geometry = QgsGeometry.fromMultiPolygonXY(
            [
                [[QgsPointXY(x, y) for x, y in ring] for ring in polygon]
                for polygon in polygons
            ]
        )
    except Exception:
        return None
    if geometry is None or geometry.isNull() or geometry.isEmpty():
        return None
    return geometry


def normalize_name(value):
    """Ключ для сопоставления регионов разных провайдеров."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def humanize(slug):
    """Читаемое имя из идентификатора региона."""
    text = str(slug or "").replace("_", " ").replace("-", " ").strip()
    return text.title() if text else ""


def parent_slug(slug):
    parts = str(slug or "").split("/")
    return "/".join(parts[:-1])


# ---------------------------------------------------------------------------
# Границы регионов
# ---------------------------------------------------------------------------


class Boundaries:
    """Границы регионов по индексу geo2day.

    В индексе для каждого региона есть ссылка на GeoJSON с контуром.
    Сами контуры большие, поэтому качаются только для регионов, которые
    могут пересекаться с рабочим охватом, и сохраняются на диск.
    """

    INDEX_URL = "https://geo2day.com/regions.json"

    def __init__(self, config):
        self.config = config
        self._regions = {}
        self._loaded = False
        self._lock = None

    def _index(self, progress=None, cancel=None):
        if self._loaded:
            return self._regions
        cache = _cache_file(self.config, "boundaries", "regions.json")
        meta = _read_meta(cache)
        now = time.time()
        payload = None
        if cache.exists() and meta.get("fetched_at", 0) + INDEX_TTL > now:
            payload = _read_json(cache)
        else:
            try:
                payload = fetch_json(self.INDEX_URL, cancel=cancel)
                _write_json(cache, payload)
                _write_meta(cache, {"fetched_at": now, "warning": ""})
            except ExtractError:
                payload = _read_json(cache) if cache.exists() else {}
        self._regions = {}
        for region in payload.get("regions") or []:
            slug = str(region.get("slug") or "")
            if not slug:
                continue
            self._regions[slug] = region
        self._loaded = True
        return self._regions

    def region(self, name_key):
        """Регион по имени без учёта регистра и подчёркиваний."""
        regions = self._index()
        if not regions:
            return None
        for slug, region in regions.items():
            if normalize_name(slug.split("/")[-1]) == name_key:
                return region
        return None

    def geometry(self, name_key, progress=None, cancel=None):
        """Граница региона по имени (None, если региона нет)."""
        region = self.region(name_key)
        if region is None:
            return None
        url = str(region.get("geojson_url") or "")
        if not url:
            return None
        cache = _cache_file(
            self.config, "boundaries", _safe_name(str(region.get("slug"))) + ".json"
        )
        meta = _read_meta(cache)
        now = time.time()
        if cache.exists() and meta.get("fetched_at", 0) + INDEX_TTL > now:
            return geometry_from_geojson(_read_json(cache))
        try:
            payload = fetch_json(url, cancel=cancel)
        except ExtractError:
            return geometry_from_geojson(_read_json(cache)) if cache.exists() else None
        _write_json(cache, payload)
        _write_meta(cache, {"fetched_at": now, "warning": ""})
        return geometry_from_geojson(payload)


# ---------------------------------------------------------------------------
# Провайдеры
# ---------------------------------------------------------------------------


class Provider:
    """Провайдер файловых выгрузок OSM."""

    key = ""
    title = ""
    homepage = ""
    # У большинства провайдеров корень индекса — континенты, а страны
    # лежат на уровень ниже.  У movisda корень — уже страны.
    ROOTS_ARE_CONTINENTS = True

    def catalogue(self, config, progress=None, cancel=None):
        raise NotImplementedError

    def geometry(self, config, item, boundaries, progress=None, cancel=None):
        """Границы выгрузки (None, если их у провайдера нет)."""
        return None


class Geo2DayProvider(Provider):
    """geo2day: 1028 регионов, обновление раз в сутки."""

    key = "geo2day"
    title = "geo2day"
    homepage = "https://geo2day.com/"

    def catalogue(self, config, progress=None, cancel=None):
        cache = _cache_file(config, self.key, "regions.json")
        meta = _read_meta(cache)
        now = time.time()
        warning = ""
        payload = None
        if cache.exists() and meta.get("fetched_at", 0) + INDEX_TTL > now:
            payload = _read_json(cache)
        else:
            if progress:
                progress("Загрузка индекса geo2day…")
            try:
                payload = fetch_json(Boundaries.INDEX_URL, cancel=cancel)
                _write_json(cache, payload)
                _write_meta(cache, {"fetched_at": now, "warning": ""})
            except ExtractError as exc:
                warning = f"Индекс geo2day недоступен: {exc}"
                if not cache.exists():
                    raise ExtractError(warning) from exc
                payload = _read_json(cache)
        maps = []
        for region in payload.get("regions") or []:
            slug = str(region.get("slug") or "")
            url = str(region.get("pbf_url") or "")
            if not slug or not url:
                continue
            name = str(region.get("name_ru") or region.get("name_en") or slug)
            maps.append(
                ExtractMap(
                    id=slug,
                    name=name,
                    parent=parent_slug(slug),
                    url=url,
                    size=int(region.get("pbf_size_bytes") or 0),
                    namespace=self.key,
                )
            )
        return Catalogue(
            maps=maps,
            fetched_at=now,
            last_modified=str(payload.get("generated_at") or ""),
            warning=warning,
        )

    def geometry(self, config, item, boundaries, progress=None, cancel=None):
        key = normalize_name(item.id.split("/")[-1])
        return boundaries.geometry(key, progress=progress, cancel=cancel)


class OpenStreetMapFrProvider(Provider):
    """download.openstreetmap.fr: выгрузки по странам и регионам.

    Индекса с границами у сервера нет: каталоги обходятся по списку
    файлов, а границы берутся из индекса geo2day — нарезки там и здесь
    сделаны по одним и тем же границам OSM.
    """

    key = "osmfr"
    title = "openstreetmap.fr"
    homepage = "https://download.openstreetmap.fr/"

    BASE = "https://download.openstreetmap.fr/extracts/"
    MAX_DEPTH = 3

    def catalogue(self, config, progress=None, cancel=None):
        cache = _cache_file(config, self.key, "catalogue.json")
        meta = _read_meta(cache)
        now = time.time()
        entries = []
        if cache.exists() and meta.get("fetched_at", 0) + INDEX_TTL > now:
            entries = _read_json(cache).get("maps") or []
        else:
            if progress:
                progress("Обход каталогов openstreetmap.fr…")
            entries = self._walk(progress=progress, cancel=cancel)
            _write_json(cache, {"maps": entries})
            _write_meta(cache, {"fetched_at": now, "warning": ""})
        maps = []
        for entry in entries:
            maps.append(
                ExtractMap(
                    id=entry["id"],
                    name=entry.get("name") or humanize(entry["id"]),
                    parent=entry.get("parent", ""),
                    url=entry["url"],
                    size=int(entry.get("size") or 0),
                    namespace=self.key,
                )
            )
        return Catalogue(maps=maps, fetched_at=now)

    def _walk(self, progress=None, cancel=None):
        """Собрать адреса выгрузок: имя файла в каталоге задаёт регион.

        Листинги каталогов запрашиваются уровнями: сначала корень, затем
        все континенты, страны и области.  Последовательный обход занимал
        около минуты, потому что каждый запрос ждал ответа сервера.
        """
        found = {}
        level = [("", 0)]
        visited = 0

        def fetch(entry):
            path, _depth = entry
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Обход каталогов прерван")
            try:
                html = fetch_bytes(self.BASE + path, cancel=cancel).decode(
                    "utf-8", "replace"
                )
            except ExtractError:
                return entry, None
            return entry, parse_listing(html)

        while level:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Обход каталогов прерван")
            with ThreadPoolExecutor(max_workers=WALK_WORKERS) as pool:
                listings = list(pool.map(fetch, level))
            following = []
            for (path, depth), parsed in listings:
                visited += 1
                if parsed is None:
                    continue
                directories, files = parsed
                for name in files:
                    if not name.endswith("-latest.osm.pbf"):
                        continue
                    slug = name[: -len("-latest.osm.pbf")]
                    found[f"{path}{slug}"] = f"{self.BASE}{path}{name}"
                if depth >= self.MAX_DEPTH:
                    continue
                for name in directories:
                    if name == "merge":
                        continue
                    following.append((f"{path}{name}/", depth + 1))
            if progress:
                progress(
                    f"Обход каталогов openstreetmap.fr: {visited}…"
                )
            level = following
        entries = []
        for slug, url in sorted(found.items()):
            entries.append(
                {
                    "id": slug,
                    "name": humanize(slug.split("/")[-1]),
                    "parent": parent_slug(slug),
                    "url": url,
                }
            )
        return entries

    def geometry(self, config, item, boundaries, progress=None, cancel=None):
        key = normalize_name(item.id.split("/")[-1])
        return boundaries.geometry(key, progress=progress, cancel=cancel)


class MovisdaProvider(Provider):
    """movisda: выгрузки по точным границам административных областей.

    Индекс — GeoJSON со всеми границами (около 25 МБ): в нём есть и
    свойства выгрузок, и контуры регионов.  Он сохраняется на диск на
    сутки; контуры преобразуются в QgsGeometry только для регионов,
    которые могут пересекаться с охватом.
    """

    key = "movisda"
    title = "movisda"
    homepage = "https://osm.download.movisda.io/"

    INDEX_URL = "https://osm.download.movisda.io/index.geojson"
    BASE = "https://osm.download.movisda.io/"
    # В индексе корневые элементы — страны, области вложены в них.
    ROOTS_ARE_CONTINENTS = False

    def catalogue(self, config, progress=None, cancel=None):
        cache = _cache_file(config, self.key, "index.geojson")
        meta = _read_meta(cache)
        now = time.time()
        warning = ""
        payload = None
        if cache.exists() and meta.get("fetched_at", 0) + INDEX_TTL > now:
            payload = _read_json(cache)
        else:
            if progress:
                progress("Загрузка индекса movisda (25 МБ)…")
            try:
                download_file(
                    self.INDEX_URL, cache, cancel=cancel, check_pbf=False
                )
                payload = _read_json(cache)
                if not isinstance(payload, dict) or not payload.get("features"):
                    raise ExtractError(
                        f"{self.INDEX_URL}: индекс не содержит регионов"
                    )
                _write_meta(cache, {"fetched_at": now, "warning": ""})
            except ExtractError as exc:
                warning = f"Индекс movisda недоступен: {exc}"
                if not cache.exists():
                    raise ExtractError(warning) from exc
                payload = _read_json(cache)
        if not isinstance(payload, dict) or not payload.get("features"):
            raise ExtractError(f"{self.INDEX_URL}: индекс не содержит регионов")
        maps = []
        for feature in payload.get("features") or []:
            properties = feature.get("properties") or {}
            code = str(properties.get("id") or "")
            if not code:
                continue
            names = properties.get("names") or {}
            name = str(names.get("ru") or properties.get("name") or code)
            urls = properties.get("urls") or {}
            maps.append(
                ExtractMap(
                    id=code,
                    name=name,
                    parent=str(properties.get("parent") or ""),
                    url=str(urls.get("pbf") or self._url(code)),
                    size=int(properties.get("bytes") or 0),
                    boundary=feature.get("geometry"),
                    namespace=self.key,
                )
            )
        return Catalogue(maps=maps, fetched_at=now, warning=warning)

    def _url(self, code):
        """Запасной адрес выгрузки, если в индексе нет ссылки."""
        return f"{self.BASE}admin/{code}-latest.osm.pbf"

    def geometry(self, config, item, boundaries, progress=None, cancel=None):
        if item.geometry is not None:
            return item.geometry
        if item.boundary is None:
            return None
        item.geometry = geometry_from_geojson(item.boundary)
        return item.geometry


PROVIDERS = {
    provider.key: provider
    for provider in (
        Geo2DayProvider(),
        OpenStreetMapFrProvider(),
        MovisdaProvider(),
    )
}

GEO2DAY = PROVIDERS["geo2day"]
OSMFR = PROVIDERS["osmfr"]
MOVISDA = PROVIDERS["movisda"]


def provider(key):
    """Провайдер по ключу; неизвестный ключ — geo2day."""
    return PROVIDERS.get(str(key or ""), GEO2DAY)


# ---------------------------------------------------------------------------
# Выбор выгрузок по охвату
# ---------------------------------------------------------------------------


def build_index(config, provider_key, bbox, progress=None, cancel=None):
    """Индекс выгрузок с границами.

    Границы дочитываются только для тех регионов, которые могут
    пересекаться с охватом: континент не задевает — значит, и его части
    не задевают.
    """
    source = provider(provider_key)
    catalogue = source.catalogue(config, progress=progress, cancel=cancel)
    maps = catalogue.maps

    boundaries = Boundaries(config)
    children = {}
    for item in maps:
        children.setdefault(item.parent, []).append(item)
    roots = [item for item in maps if not item.parent]
    rectangle = QgsRectangle(bbox.lon_min, bbox.lat_min, bbox.lon_max, bbox.lat_max)

    def resolve(items):
        """Границы регионов; None, если границ нет."""
        pending = [item for item in items if item.geometry is None]
        if not pending:
            return
        with ThreadPoolExecutor(max_workers=GEOMETRY_WORKERS) as pool:
            futures = {
                pool.submit(
                    source.geometry, config, item, boundaries, None, cancel
                ): item
                for item in pending
            }
            for future, item in futures.items():
                try:
                    item.geometry = future.result()
                except InterruptedError:
                    raise
                except Exception:
                    item.geometry = None

    # Страны (дети континентов) — самая мелкая нарезка, что есть у всех
    # провайдеров; дальше спускаемся только внутрь подходящих стран.
    # У movisda наоборот: корень — страны, а области вложены в них,
    # поэтому сначала проверяются только корневые элементы.
    level = []
    if source.ROOTS_ARE_CONTINENTS:
        for root in roots:
            if root.geometry is not None:
                # Границы уже в индексе.
                level.append(root)
            level.extend(children.get(root.id, []))
    else:
        level.extend(roots)
    entries = []
    while level:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Определение выгрузок прервано")
        resolve(level)
        following = []
        for item in level:
            geometry = item.geometry
            if geometry is None:
                continue
            if not geometry.boundingBox().intersects(rectangle):
                continue
            entries.append((item, geometry))
            following.extend(children.get(item.id, []))
        level = following
    return geofabrik.build_index(entries), catalogue


def build_plan(config, provider_key, bbox, progress=None, cancel=None):
    """План выгрузок для охвата: нужные карты и их состояние."""
    index, catalogue = build_index(
        config, provider_key, bbox, progress=progress, cancel=cancel
    )
    maps = index.required(bbox)
    chosen = {item.id for item in maps}
    extra = [item for item in index.candidates(bbox) if item.id not in chosen]
    statuses = _statuses(config, maps + extra, catalogue)
    by_id = {status.map.id: status for status in statuses}
    names = {item.id: item.name for item in catalogue.maps}
    parents = {
        item.id: names.get(item.parent, "")
        for item in catalogue.maps
        if item.parent
    }
    state = geofabrik.IndexState(
        index=index,
        fetched_at=catalogue.fetched_at,
        last_modified=catalogue.last_modified,
        warning=catalogue.warning,
    )
    return geofabrik.GeofabrikPlan(
        state=state,
        maps=maps,
        statuses=[by_id[item.id] for item in maps],
        extra=extra,
        extra_statuses=[by_id[item.id] for item in extra],
        parent_names=parents,
    )


def _statuses(config, maps, catalogue):
    """Состояние выгрузок: размер известен из индекса или проверяется HEAD."""
    sizes = {item.id: item.size for item in catalogue.maps if item.size}
    unknown = [item for item in maps if item.id not in sizes]
    checked = {}
    if unknown:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(head_size, item.url): item for item in unknown[:MAX_HEAD_CHECKS]
            }
            for future, item in futures.items():
                try:
                    checked[item.id] = future.result()
                except Exception:
                    checked[item.id] = (False, 0, "")
    statuses = []
    for item in maps:
        available, size, error = checked.get(item.id, (True, sizes.get(item.id, 0), ""))
        statuses.append(
            geofabrik.MapStatus(
                map=item,
                available=available,
                size=size,
                error=error,
                local=geofabrik.local_ready(config, item),
            )
        )
    return statuses


# ---------------------------------------------------------------------------
# Кэш индексов
# ---------------------------------------------------------------------------


def _cache_file(config, name, filename):
    directory = config.extracts_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "x"))[:120]


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _read_meta(path):
    meta_path = Path(str(path) + ".meta")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_meta(path, meta):
    meta_path = Path(str(path) + ".meta")
    temporary = meta_path.with_suffix(meta_path.suffix + ".part")
    temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, meta_path)


def invalidate_cache(config, provider_key):
    """Помечает кэш индекса провайдера устаревшим.

    Обновление кнопкой должно перекачать индекс при следующем запросе.
    Метаданные лежат рядом с каждым файлом кэша, поэтому сбрасываются
    для всех файлов провайдера: у movisda это index.geojson, у
    openstreetmap.fr — catalogue.json.
    """
    directory = config.extracts_dir / str(provider_key or "")
    if not directory.exists():
        return
    for path in directory.glob("*"):
        if path.name.endswith((".meta", ".part")):
            continue
        meta_path = path.with_name(path.name + ".meta")
        if not meta_path.exists():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data["fetched_at"] = 0
        _write_meta(path, data)


# ---------------------------------------------------------------------------
# Вырез по рамке (SliceOSM)
# ---------------------------------------------------------------------------


class SliceOsm:
    """Вырез OSM по произвольной рамке через сервис SliceOSM.

    Данные свежее минуты, но это не Overpass: сервис собирает файл PBF
    и отдаёт его для скачивания.
    """

    API = "https://slice.openstreetmap.us/api/"
    FILES = "https://slice.openstreetmap.us/files/"

    def submit(self, bbox, cancel=None):
        """Создать вырез; возвращает идентификатор задания."""
        payload = json.dumps(
            {
                "Name": "qgis-xtopo",
                "RegionType": "bbox",
                # Сервис ждёт min_lat, min_lon, max_lat, max_lon.
                "RegionData": [
                    bbox.lat_min, bbox.lon_min, bbox.lat_max, bbox.lon_max
                ],
            }
        ).encode("utf-8")
        try:
            with _open(
                self.API,
                headers={"Content-Type": "application/json"},
                method="POST",
                data=payload,
                timeout=HTTP_TIMEOUT,
            ) as response:
                body = response.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            raise ExtractError(f"SliceOSM: HTTP {exc.code}") from exc
        except Exception as exc:
            raise ExtractError(f"SliceOSM: {exc}") from exc
        uuid = body.strip().strip('"')
        if not uuid or " " in uuid:
            raise ExtractError(f"SliceOSM: неожиданный ответ {body[:80]}")
        return uuid

    def status(self, uuid, cancel=None):
        try:
            payload = fetch_json(f"{self.API}{uuid}", cancel=cancel)
        except ExtractError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def is_complete(self, payload):
        value = payload.get("Complete")
        if isinstance(value, bool):
            return value
        return str(value).lower() == "true"

    def wait(self, uuid, cancel=None, progress=None, timeout=600):
        """Дождаться готовности выреза."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Вырез прерван")
            payload = self.status(uuid, cancel=cancel)
            if self.is_complete(payload):
                return payload
            if progress:
                elapsed = str(payload.get("Elapsed") or "")
                progress(f"SliceOSM: сборка выреза… {elapsed}")
            time.sleep(3)
        raise ExtractError("SliceOSM: вырез не готов за отведённое время")

    def download(self, uuid, destination, progress=None, cancel=None):
        url = f"{self.FILES}{uuid}.osm.pbf"
        return download_file(
            url, destination, progress=progress, cancel=cancel, label="SliceOSM",
        )
